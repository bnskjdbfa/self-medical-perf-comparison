"""Opt-in staging entrypoint; regular authenticated app, no test URLs/CSRF bypass."""
import hmac
import json
import os
import re
import time
import uuid
import hashlib
from http.cookies import SimpleCookie, CookieError

from django.core.wsgi import get_wsgi_application
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
from django.conf import settings

if (settings.APP_ENV != 'staging' or settings.DB_IS_PRODUCTION is not False
        or settings.DATABASES['default']['ENGINE'] != 'django.db.backends.postgresql'
        or os.environ.get('STAGING_PERF_ENABLED') != '1'):
    raise RuntimeError('This measurement entrypoint requires a non-production PostgreSQL staging environment.')
secret = os.environ.get('STAGING_PERF_TOKEN', '')
build = os.environ.get('STAGING_PERF_BUILD_ID') or os.environ.get('RENDER_GIT_COMMIT', '')
if len(secret) < 32 or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', build):
    raise RuntimeError('Set a private measurement token and a safe immutable build identifier.')
sources = {name: hashlib.sha256((settings.BASE_DIR / name).read_bytes()).hexdigest()
           for name in ('config/db_limits.py', 'config/session_backend.py',
                        'config/session_middleware.py', 'surveys/views.py')}
code_digest = hashlib.sha256(json.dumps(sources, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
git_commit = os.environ.get('RENDER_GIT_COMMIT', '')
git_commit = git_commit if re.fullmatch(r'[a-fA-F0-9]{40,64}', git_commit) else 'unknown'

app = get_wsgi_application()
from django.db import connections
from contextlib import ExitStack
from staging_perf_metrics import active, Measurement, install, route_name
install()


def application(environ, start_response):
    route = route_name(environ.get('PATH_INFO', ''))
    supplied = environ.get('HTTP_X_STAGING_PERF_TOKEN', '')
    if not supplied:
        try:
            probe = SimpleCookie(environ.get('HTTP_COOKIE', '')).get('__Host-staging_perf')
            supplied = probe.value if probe else ''
        except CookieError:
            supplied = ''
    if route is None or not hmac.compare_digest(secret.encode(), supplied.encode()):
        return app(environ, start_response)
    measurement = Measurement()
    token = active.set(measurement)
    request_id = uuid.uuid4().hex
    started = time.perf_counter()
    captured = []
    try:
        with ExitStack() as stack:
            # Do NOT connect or execute an observer query here. The first
            # actual application cursor must establish the measured connection.
            for connection in connections.all():
                stack.enter_context(connection.execute_wrapper(measurement.execute))
            response = app(environ, lambda status, headers, exc_info=None: captured.append((status, headers, exc_info)))
            # Profile the finite HTML/JSON routes only, never downloads/streams.
            try:
                body = list(response)
            finally:
                if hasattr(response, 'close'):
                    response.close()
        measurement.values['app_ms'] = (time.perf_counter() - started) * 1000
        status, headers, exc_info = captured[0]
        values = {key: round(measurement.values[key], 3) for key in (
            'app_ms', 'db_open_ms', 'driver_connect_ms', 'query_ms', 'commit_ms',
            'rollback_ms', 'healthcheck_ms', 'health_checks', 'session_ms', 'sql_count', 'session_saves', 'db_opens')}
        timings = ', '.join(f'{key};dur={values[key]}' for key in (
            'app_ms', 'db_open_ms', 'driver_connect_ms', 'query_ms', 'healthcheck_ms', 'commit_ms', 'rollback_ms', 'session_ms'))
        headers += [('Server-Timing', timings), ('X-Perf-Sql-Count', str(values['sql_count'])),
                    ('X-Perf-Session-Saves', str(values['session_saves'])),
                    ('X-Perf-Db-Opens', str(values['db_opens'])), ('X-Perf-Build', build),
                    ('X-Perf-Worker', str(os.getpid())),
                    ('X-Perf-Code-Digest', code_digest),
                    ('X-Perf-Request-ID', request_id), ('Cache-Control', 'private, no-store')]
        print(json.dumps({'event': 'staging_perf', 'build': build, 'code_digest': code_digest,
            'git_commit': git_commit, 'request_id': request_id,
            'route': route, 'worker': os.getpid(), 'method': 'POST' if environ.get('REQUEST_METHOD') == 'POST' else 'GET',
            'status': int(status.split()[0]), **values,
            'sql_kinds': dict(measurement.kinds), 'sql_sequence': measurement.sequence}), flush=True)
        start_response(status, headers, exc_info)
        return body
    finally:
        active.reset(token)
