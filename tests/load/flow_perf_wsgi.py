"""Isolated performance server. Never configure this entrypoint in deployment."""
import contextvars
from contextlib import contextmanager
import os
import re
import sys
import time
from collections import Counter

if os.environ.get('FLOW_PERF_TEST') != '1' or os.environ.get('APP_ENV') != 'test':
    raise RuntimeError('Isolated performance tests only')
sys.argv.append('test')
from django.core.wsgi import get_wsgi_application
from django.contrib.staticfiles.handlers import StaticFilesHandler
from django.db import connection

app = StaticFilesHandler(get_wsgi_application())
state = contextvars.ContextVar('perf_state', default=None)


def timed_method(cls, name):
    original = getattr(cls, name)
    def wrapped(*args, **kwargs):
        current = state.get()
        start = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            if current is not None:
                current['session_ms'] += (time.perf_counter() - start) * 1000
                if name == 'save':
                    current['session_saves'] += 1
    setattr(cls, name, wrapped)


try:
    from config import session_backend, session_middleware
except ImportError:
    from django.contrib.sessions.backends.db import SessionStore
    timed_method(SessionStore, 'load')
    timed_method(SessionStore, 'save')
else:
    timed_method(session_backend.SessionStore, 'save')
    timed_method(session_backend.SessionStore, 'load')
    original_atomic = session_middleware.bounded_atomic
    @contextmanager
    def timed_atomic(*args, **kwargs):
        start = time.perf_counter()
        try:
            with original_atomic(*args, **kwargs):
                yield
        finally:
            state.get()['session_ms'] += (time.perf_counter() - start) * 1000
    session_middleware.bounded_atomic = timed_atomic


def application(environ, start_response):
    stats = {'queries': 0, 'db_ms': 0, 'session_ms': 0, 'session_saves': 0}
    tables = Counter()
    token = state.set(stats)
    def execute(run, sql, params, many, context):
        start = time.perf_counter()
        try:
            return run(sql, params, many, context)
        finally:
            stats['queries'] += 1
            stats['db_ms'] += (time.perf_counter() - start) * 1000
            # Only table names/operations; never retain SQL or bind values.
            match = re.search(r'\b(?:FROM|UPDATE|INTO)\s+"([a-z_]+)"', sql, re.I)
            if match:
                tables[match.group(1)] += 1
    captured = []
    started = time.perf_counter()
    try:
        route = re.sub(r'/[0-9]+/', '/n/', environ.get('PATH_INFO', '/'))[:100]
        if not route.startswith('/static/'):
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('application_name', %s, false)", ['flow-perf:' + route])
        with connection.execute_wrapper(execute):
            response = app(environ, lambda status, headers, exc_info=None: captured.append((status, headers, exc_info)))
            body = list(response)
            if hasattr(response, 'close'):
                response.close()
        stats['server_ms'] = (time.perf_counter() - started) * 1000
        status, headers, exc_info = captured[0]
        headers += [('X-Perf-' + key.replace('_', '-'), str(round(value, 3))) for key, value in stats.items()]
        headers.append(('X-Perf-Tables', ','.join(f'{key}:{value}' for key, value in sorted(tables.items()))))
        start_response(status, headers, exc_info)
        return body
    finally:
        state.reset(token)
