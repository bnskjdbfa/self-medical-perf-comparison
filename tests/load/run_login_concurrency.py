"""Isolated HTTP + optional Chrome regression with a named Gunicorn profile.

Usage: .venv/bin/python tests/load/run_login_concurrency.py [--browser]
Browser dependency: Playwright available via NODE_PATH; uses installed Chrome.
Never points at Render or any configured project database.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
from http.cookies import SimpleCookie
from io import StringIO
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from server_profile import arguments as server_arguments, profile
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[2]
CODE = '/surveys/preview/v2/code/'
IDENTITY = '/surveys/preview/v2/identity/'
HOME = '/surveys/preview/v2/home/'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


@contextmanager
def isolated_database(temp, postgres_port):
    if postgres_port is None:
        yield {'DB_ENGINE': 'django.db.backends.sqlite3',
               'DB_NAME': str(Path(temp) / 'test.sqlite3')}
        return
    # No arbitrary host/DSN is accepted. This tool only creates its own database
    # on an explicitly requested local PostgreSQL instance, never .env's DB.
    import psycopg
    from psycopg import sql
    db_name = 'login_predeploy_' + uuid.uuid4().hex
    admin = psycopg.connect(host='127.0.0.1', port=postgres_port, dbname='postgres',
                           user='postgres', password='', sslmode='disable',
                           passfile=str(Path(temp) / 'unused.pgpass'), connect_timeout=3, autocommit=True)
    try:
        admin.execute(sql.SQL("CREATE DATABASE {} ENCODING 'UTF8' TEMPLATE template0").format(sql.Identifier(db_name)))
        try:
            yield {'DB_ENGINE': 'django.db.backends.postgresql', 'DB_NAME': db_name,
                   'DB_HOST': '127.0.0.1', 'DB_PORT': str(postgres_port),
                   'DB_USER': 'postgres', 'DB_PASSWORD': '', 'DB_SSLMODE': 'disable'}
        finally:
            from django.db import connections
            connections.close_all()
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(db_name)))
    finally:
        admin.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', action='store_true')
    parser.add_argument('--postgres-port', type=int,
                        help='Disposable database on local isolated PostgreSQL; never reads a DSN.')
    parser.add_argument('--predeploy', action='store_true', help='Contention, fault and cookie-order probes.')
    parser.add_argument('--django-tests', action='store_true', help='Run login/consent/policy tests on the isolated backend.')
    parser.add_argument('--users42', action='store_true', help='42 distinct synthetic participants, three submissions each.')
    parser.add_argument('--report', type=Path, help='Write sanitized JSON results (no cookies or input).')
    parser.add_argument('--server-profile', choices=['sync3', 'production-gthread', 'local-gthread-2x4', 'local-gthread-2x4-no-keepalive', 'local-gthread-2x6-no-keepalive', 'local-gthread-2x8-no-keepalive'], default='sync3')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='login-race-') as temp, isolated_database(temp, args.postgres_port) as db_env:
        os.environ.update({
            'PYTHON_DOTENV_DISABLED': '1',
            'APP_ENV': 'test', 'DB_IS_PRODUCTION': 'false',
            'ALLOW_DATABASE_ENV_MISMATCH': 'false',
            'DJANGO_SETTINGS_MODULE': 'config.settings', 'DJANGO_DEBUG': 'true',
            'DJANGO_SECRET_KEY': 'isolated-login-race-test-secret-not-used-in-deployment',
            'DJANGO_ALLOWED_HOSTS': '127.0.0.1,localhost',
            'DJANGO_SESSION_COOKIE_SECURE': 'false', 'DJANGO_CSRF_COOKIE_SECURE': 'false',
            'DJANGO_SECURE_SSL_REDIRECT': 'false', 'ADMIN_2FA_REQUIRED': 'false',
            'ENABLE_PERF_LOG': 'false', 'LOGIN_RACE_TEST_SERVER': '1',
            **db_env,
        })
        sys.path.insert(0, str(ROOT))
        import django
        django.setup()
        from django.core.management import call_command
        from django.utils import timezone
        from teams.models import Participant, Team
        from surveys.models import TeamFeatureSetting

        target = StringIO()
        call_command('show_db_target', stdout=target)
        assert 'STATUS                : SAFE' in target.getvalue()
        assert 'MISMATCH_OVERRIDE     : false' in target.getvalue()
        call_command('migrate', verbosity=0, stdout=StringIO())
        team = Team.objects.create(
            name='Race fixture', code='LOGIN-RACE', start_date=timezone.localdate(), max_day_no=3,
            organization_name='Fixture', organization_name_en='Fixture',
        )
        TeamFeatureSetting.objects.create(team=team)
        Participant.objects.create(
            team=team, name='Race Fixture', normalized_name='RaceFixture',
            birth_date=date(1990, 1, 1), gender='M', is_active=True,
        )
        from surveys.services import normalize_name
        Participant.objects.filter(team=team).update(normalized_name=normalize_name('Race Fixture'))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        workers = set()
        worker_threads = set()

        def request(path, cookies=None, payload=None, delay=0, timeout=15, headers_extra=None):
            headers = {'Cookie': '; '.join(f'{k}={v}' for k, v in (cookies or {}).items()),
                       'X-Test-Delay': str(delay), 'Referer': base + CODE}
            if payload is not None:
                headers['Content-Type'] = 'application/x-www-form-urlencoded'
                headers['X-CSRFToken'] = (cookies or {}).get('csrftoken', '')
            headers.update(headers_extra or {})
            req = Request(base + path, headers=headers,
                          data=urlencode(payload).encode() if payload is not None else None)
            try:
                response = build_opener(NoRedirect).open(req, timeout=timeout)
            except HTTPError as exc:
                response = exc
            with response:
                received = {}
                for value in response.headers.get_all('Set-Cookie', []):
                    parsed = SimpleCookie()
                    parsed.load(value)
                    for name, morsel in parsed.items():
                        received[name] = morsel.value
                worker = response.headers.get('X-Test-Worker')
                if worker:
                    workers.add(worker)
                    if response.headers.get('X-Test-Thread'):
                        worker_threads.add((worker, response.headers['X-Test-Thread']))
                return response.status, dict(response.headers), received, response.read()

        def retry_request(path, cookies=None, payload=None, delay=0):
            for attempt in range(10):
                result = request(path, cookies, payload, delay)
                if result[0] != 503:
                    return result
                time.sleep(min(0.01 * (attempt + 1), 0.1))
            return result

        log_path = Path(temp) / 'server.log'
        with log_path.open('w') as log:
            server = subprocess.Popen([
                sys.executable, '-m', 'gunicorn', *server_arguments(args.server_profile), '--bind', f'127.0.0.1:{port}',
                '--pythonpath', str(ROOT / 'tests/load'),
                'login_wsgi:application',
            ], cwd=ROOT, stdout=log, stderr=log,
                env={**os.environ, 'PGAPPNAME': 'login_predeploy_workers'})
            try:
                for _ in range(100):
                    try:
                        if request(CODE)[0] == 200:
                            break
                    except OSError:
                        pass
                    if server.poll() is not None:
                        raise RuntimeError('Local test server did not start; inspect test environment.')
                    time.sleep(0.1)
                else:
                    raise RuntimeError('Local test server readiness timeout.')

                expected = profile(args.server_profile)
                burst = max(8, expected['workers']*expected['threads'])
                if expected['threads'] > 4:
                    # Observe all lazily created pool threads before asserting
                    # capacity; static probes do not open DB connections.
                    probe_count=max(42,burst*4)
                    with ThreadPoolExecutor(max_workers=probe_count) as pool:
                        probes=list(pool.map(lambda _:request('/static/surveys/public_v2.css',delay=.15),range(probe_count)))
                    assert all(r[0]==200 for r in probes), 'thread_capacity_probe_failed'
                for _ in range(5):
                    cookies = request(CODE)[2]
                    with ThreadPoolExecutor(max_workers=burst) as pool:
                        code_results = list(pool.map(
                            lambda _: retry_request(CODE, cookies, {'team_code': 'LOGIN-RACE'}), range(burst)))
                    assert all(result[0] == 302 for result in code_results), [r[0] for r in code_results]
                    for result in code_results:
                        cookies.update(result[2])
                    prelogin = dict(cookies)
                    payload = {'name': 'Race Fixture', 'birth_date': '900101', 'gender': 'M'}
                    with ThreadPoolExecutor(max_workers=burst) as pool:
                        identity_results = list(pool.map(
                            lambda _: retry_request(IDENTITY, prelogin, payload), range(burst)))
                    assert sorted(result[0] for result in identity_results) == [302] + [409] * (burst-1)
                    success = next(result for result in identity_results if result[0] == 302)
                    cookies.update(success[2])
                    assert cookies.get('authenticated_sessionid')
                    with ThreadPoolExecutor(max_workers=3) as pool:
                        delayed = pool.submit(request, CODE, prelogin, {'team_code': 'LOGIN-RACE'}, 0.4)
                        assert request(HOME, cookies)[0] == 200
                        late = delayed.result()
                    assert 'authenticated_sessionid' not in late[2]
                    cookies.update(late[2])
                    assert request(HOME, cookies)[0] == 200
                    with ThreadPoolExecutor(max_workers=8) as pool:
                        updates = list(pool.map(
                            lambda _: retry_request('/_test/session-update/', cookies, {}), range(12)))
                    assert all(result[0] == 200 for result in updates)
                    assert sorted(json.loads(result[3])['count'] for result in updates) == list(range(1, 13))
                expected = profile(args.server_profile)
                assert len(workers) == expected['workers']
                observed_threads = sorted(sum(worker == owner for owner, thread in worker_threads) for worker in workers)
                if expected['worker_class'] == 'gthread':
                    assert observed_threads == [expected['threads']] * expected['workers'], observed_threads
                print('PASS HTTP: ' + args.server_profile + f'; 5 rounds; {5*burst} code + {5*burst} identity submissions; 60 concurrent session updates; late responses preserve login')
                report = {'backend': db_env['DB_ENGINE'], 'multiworker': 'pass', 'workers': len(workers),
                          'server_profile': expected, 'observed_threads_per_worker': observed_threads,
                          'code_posts': 5*burst, 'identity_posts': 5*burst, 'concurrent_updates': 60}
                if args.predeploy:
                    from login_predeploy_checks import run_checks
                    report.update(run_checks(request, db_env, capacity=expected['workers'] * expected['threads']))
                if args.users42:
                    from login_load_42 import run
                    report['load42'] = run(request)
                if args.browser:
                    browser = subprocess.run(['node', str(ROOT / 'tests/load/login_browser.cjs')],
                                             env={**os.environ, 'LOGIN_TEST_BASE_URL': base})
                    report['browser'] = 'pass' if browser.returncode == 0 else 'fail'
                if args.django_tests:
                    checks = subprocess.run([
                        sys.executable, 'manage.py', 'test', 'surveys.test_login_concurrency',
                        'surveys.test_login_deployment', 'surveys.tests.RequiredDocumentFlowTest',
                        'surveys.test_session_rotation_writes', 'surveys.test_identity_round_trips',
                        'surveys.test_session_duplicate_writes',
                        '--verbosity', '0',
                    ], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                        env={**os.environ, 'DJANGO_SETTINGS_MODULE': 'login_test_settings',
                             'PYTHONPATH': str(ROOT / 'tests/load') + os.pathsep + str(ROOT)})
                    count = re.search(r'Ran (\d+) tests', checks.stdout)
                    failures = re.findall(r'^(?:FAIL|ERROR): (\w+) \(([\w.]+)\)',
                                          checks.stdout, flags=re.MULTILINE)
                    types = sorted(set(re.findall(r'^([A-Za-z_.]*Error):', checks.stdout,
                                                  flags=re.MULTILINE)))
                    report['django_regression'] = {
                        'status': 'pass' if checks.returncode == 0 else 'fail',
                        'count': int(count.group(1)) if count else None,
                        'failed_tests': [owner for name, owner in failures],
                        'exception_types': types,
                        'test_database_charset': 'UTF8' if args.postgres_port else 'sqlite',
                    }
                    print('ISOLATED DJANGO ' + json.dumps(report['django_regression']))
                if args.report:
                    args.report.write_text(json.dumps(report, indent=2) + '\n')
                def failed(value):
                    if isinstance(value, dict):
                        return any(failed(item) for item in value.values())
                    return value == 'fail'
                if failed(report):
                    raise SystemExit(2 if args.predeploy else 1)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


if __name__ == '__main__':
    main()
