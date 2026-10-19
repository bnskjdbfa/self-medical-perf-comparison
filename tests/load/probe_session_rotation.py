"""Count a full CSRF-enforced identity request on LOCAL disposable PostgreSQL.

No arbitrary database host/DSN is accepted. This is an in-process query probe,
not a Render latency or concurrent-user benchmark.
"""
import argparse
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--postgres-port', type=int, default=57482)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path.insert(0, str(root))
    if not args.child:
        from run_login_concurrency import isolated_database
        os.environ.update({'APP_ENV': 'test', 'DJANGO_SETTINGS_MODULE': 'config.settings',
                           'DB_IS_PRODUCTION': 'false', 'PYTHON_DOTENV_DISABLED': '1'})
        with tempfile.TemporaryDirectory(prefix='rotation-probe-') as temp, isolated_database(temp, args.postgres_port) as db:
            os.environ.update(db)
            env = {**os.environ, 'APP_ENV': 'staging', 'STAGING_PERF_ENABLED': '1',
                   'ALLOW_DATABASE_ENV_MISMATCH': 'false', 'DJANGO_DEBUG': 'true',
                   'DJANGO_ALLOWED_HOSTS': 'testserver', 'DJANGO_SECURE_SSL_REDIRECT': 'false',
                   'DJANGO_CACHE_URL': '', 'REDIS_URL': '', 'PYTHONPATH': str(root)}
            subprocess.run([sys.executable, str(Path(__file__).resolve()), '--child',
                            '--root', str(root), '--report', str(args.report.resolve())],
                           cwd=root, env=env, check=True)
        return

    import django
    django.setup()
    from django.conf import settings
    from django.core.management import call_command
    from django.db import connection
    from django.test import Client
    from staging_perf_metrics import Measurement, active, install
    if settings.APP_ENV != 'staging' or settings.DB_IS_PRODUCTION is not False or settings.DATABASES['default']['HOST'] != '127.0.0.1':
        raise RuntimeError('Local disposable staging database required.')
    call_command('migrate', verbosity=0, stdout=StringIO())
    subprocess.run([sys.executable, str(root / 'tests/load/prepare_staging_fixture.py'), '--init'],
                   check=True, capture_output=True, text=True, cwd=root)
    install()
    client = Client(enforce_csrf_checks=True)
    code = '/surveys/preview/v2/code/'
    privacy = '/surveys/preview/v2/privacy-consent/'
    identity = '/surveys/preview/v2/identity/'
    def post(path, data):
        return client.post(path, data, secure=True,
            HTTP_X_CSRFTOKEN=client.cookies[settings.CSRF_COOKIE_NAME].value,
            HTTP_ORIGIN='https://testserver', HTTP_X_LOGIN_REQUEST='1')
    assert client.get(code, secure=True).status_code == 200
    assert post(code, {'team_code': 'PERF-STAGING'}).status_code == 200
    assert client.get(privacy, secure=True).status_code == 200
    assert post(privacy, {'agreed': 'on'}).status_code == 302
    assert client.get(identity, secure=True).status_code == 200
    measurement = Measurement()
    token = active.set(measurement)
    try:
        with connection.execute_wrapper(measurement.execute):
            response = post(identity, {'name': 'Stage Fixture 41', 'birth_date': '900101', 'gender': 'M'})
    finally:
        active.reset(token)
    assert response.status_code == 200
    assert response.json()['redirect_url'].startswith('/surveys/preview/v2/')
    result = {'root': str(root), 'scope': 'local in-process full identity POST, enforced CSRF; no injected network latency',
              'status': response.status_code, 'sql_count': measurement.values['sql_count'],
              'sql_kinds': dict(measurement.kinds), 'session_saves': measurement.values['session_saves'],
              'raw_session_values_logged': False}
    args.report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
