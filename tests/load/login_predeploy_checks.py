"""Sanitized probes for the isolated local PostgreSQL regression server only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import time


CODE = '/surveys/preview/v2/code/'
IDENTITY = '/surveys/preview/v2/identity/'
HOME = '/surveys/preview/v2/home/'
RESET = '/surveys/preview/v2/login/reset/'


def login(request):
    cookies = request(CODE)[2]
    code = request(CODE, cookies, {'team_code': 'LOGIN-RACE'})
    assert code[0] == 302
    cookies.update(code[2])
    result = request(IDENTITY, cookies, {
        'name': 'Race Fixture', 'birth_date': '900101', 'gender': 'M',
    })
    assert result[0] == 302
    cookies.update(result[2])
    assert cookies.get('sessionid') == cookies.get('authenticated_sessionid')
    return cookies


def cookie_matrix(request):
    results = {}
    for mode in ('reset', 'logout'):
        cookies = login(request)
        # For /ops/logout/, use an actual logged-in synthetic Django account.
        if mode == 'logout':
            from django.contrib.auth import get_user_model, HASH_SESSION_KEY, SESSION_KEY, BACKEND_SESSION_KEY
            from django.contrib.sessions.backends.db import SessionStore
            user, _ = get_user_model().objects.get_or_create(username='predeploy_fixture')
            session = SessionStore(cookies['authenticated_sessionid'])
            session[SESSION_KEY] = str(user.pk)
            session[BACKEND_SESSION_KEY] = 'django.contrib.auth.backends.ModelBackend'
            session[HASH_SESSION_KEY] = user.get_session_auth_hash()
            session.save()
        old = dict(cookies)
        cleared = request(RESET if mode == 'reset' else '/ops/logout/', cookies, {})
        assert cleared[0] == 302, f'{mode} status={cleared[0]}'
        cookies.update(cleared[2])
        revoked = request(HOME, old)[0] == 302
        results[f'{mode}_old_key_revoked'] = 'pass' if revoked else 'fail'
        fresh = login(request)
        fresh.update(cleared[2])
        results[f'late_{mode}_response_preserves_new_login'] = (
            'pass' if request(HOME, fresh)[0] == 200 else 'fail')

    # Two initial tabs can establish independent pre-auth rows before either
    # authenticates. A late ordinary response from branch A can arrive after
    # branch B was authenticated and reset. This MUST NOT resurrect branch A.
    branch_a = login(request)
    late_a = request(CODE + '?lang=en', branch_a)
    assert 'authenticated_sessionid' not in late_a[2]
    branch_b = login(request)
    reset_b = request(RESET, branch_b, {})
    branch_b.update(reset_b[2])
    branch_b.update(late_a[2])
    results['late_parallel_branch_cannot_resurrect_after_reset'] = (
        'pass' if request(HOME, branch_b)[0] == 302 else 'fail')

    # A delayed legacy-session promotion can write both old cookie values.
    # Legacy browsers know the original sessionid before the promotion response.
    branch_a = login(request)
    from django.contrib.sessions.backends.db import SessionStore
    legacy_store = SessionStore()
    legacy_store.update(SessionStore(branch_a['authenticated_sessionid']).load())
    legacy_store.save()
    legacy = {'sessionid': legacy_store.session_key, 'csrftoken': branch_a['csrftoken']}
    delayed_promotion = request(CODE + '?lang=en', legacy)
    assert 'authenticated_sessionid' in delayed_promotion[2]
    request(RESET, legacy, {})
    branch_b = login(request)
    branch_b.update(delayed_promotion[2])
    results['late_legacy_promotion_cannot_erase_new_login'] = (
        'pass' if request(HOME, branch_b)[0] == 200 else 'fail')

    # Real Django login rotates the existing participant session. A reset sent
    # with the old cookies while that login response is in flight must revoke
    # the in-flight login as well, rather than allowing its late resurrection.
    from django.contrib.auth import get_user_model
    user = get_user_model().objects.get(username='predeploy_fixture')
    user.is_staff = True
    user.set_password('Isolated-fixture-password-2026')
    user.save()
    current = login(request)
    delayed_rotation = request('/admin/login/', current, {
        'username': user.username, 'password': 'Isolated-fixture-password-2026', 'next': HOME,
    })
    assert delayed_rotation[0] == 302
    assert delayed_rotation[2].get('authenticated_sessionid')
    cleared = request(RESET, current, {})
    assert cleared[0] == 302
    current.update(cleared[2])
    current.update(delayed_rotation[2])
    results['reset_during_key_rotation_cannot_resurrect'] = (
        'pass' if request(HOME, current)[0] == 302 else 'fail')
    return results


def run_checks(request, db_env, capacity=3):
    report = {'cookie_matrix': cookie_matrix(request)}
    if db_env['DB_ENGINE'] != 'django.db.backends.postgresql':
        report['postgres_contention'] = 'not_run'
        return report
    import psycopg
    options = dict(host='127.0.0.1', port=int(db_env['DB_PORT']), dbname=db_env['DB_NAME'],
                   user='postgres', password='', sslmode='disable', connect_timeout=3,
                   passfile='/tmp/nonexistent-login-regression.pgpass')
    cookies = login(request)
    limits = request('/_test/effective-limits/', cookies)
    assert limits[0] == 200
    report['effective_transaction_limits'] = json.loads(limits[3])
    with closing(psycopg.connect(**options, autocommit=True)) as monitor:
        monitor.execute("SET client_encoding = 'UTF8'")
        report['postgres_version'] = monitor.execute('SHOW server_version').fetchone()[0]
        report['database_limits'] = {
            name: monitor.execute('SHOW ' + name).fetchone()[0]
            for name in ('lock_timeout', 'statement_timeout', 'idle_in_transaction_session_timeout',
                         'transaction_timeout', 'max_connections')
        }
        # External row holder is bounded by this test, not by the application.
        with closing(psycopg.connect(**options)) as holder:
            holder.execute('SELECT session_key FROM django_session WHERE session_key=%s FOR UPDATE',
                           (cookies['authenticated_sessionid'],))
            with ThreadPoolExecutor(max_workers=capacity + 1) as pool:
                started = time.monotonic()
                blocked = [pool.submit(request, '/_test/session-update/', cookies, {}) for _ in range(capacity)]
                unrelated_started = time.monotonic()
                unrelated = pool.submit(request, CODE)
                try:
                    statuses = [future.result(timeout=2)[0] for future in blocked]
                    assert unrelated.result(timeout=2)[0] == 200
                    unrelated_delay = time.monotonic() - unrelated_started
                finally:
                    holder.rollback()
                assert statuses == [503] * capacity
                assert unrelated_delay < 1
                report['lock_contention'] = {
                    'busy_statuses': statuses,
                    'observed_hold_seconds': round(time.monotonic() - started, 3),
                    'unrelated_delay_seconds': round(unrelated_delay, 3),
                    'deployment_gate': 'pass',
                }
        # Slow Python/external I/O must not hold a session row/transaction.
        # A SQL statement timeout alone would not stop this sleep.
        with ThreadPoolExecutor(max_workers=2) as pool:
            slow = pool.submit(request, '/_test/slow-session/', cookies)
            time.sleep(0.15)
            idle = monitor.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=%s "
                "AND application_name='login_predeploy_workers' AND state='idle in transaction'",
                (db_env['DB_NAME'],)).fetchone()[0]
            begin = time.monotonic()
            same_session = pool.submit(request, HOME, cookies)
            assert same_session.result()[0] == 200
            delay = time.monotonic() - begin
            assert not slow.done()
            assert slow.result()[0] == 200
            assert not idle
            report['slow_view'] = {'same_session_delay_seconds': round(delay, 3),
                                   'idle_in_transaction_observed': bool(idle), 'deployment_gate': 'pass'}

        start = time.monotonic()
        timeout = request('/_test/statement-timeout/', cookies)
        timeout_elapsed = time.monotonic() - start
        assert timeout[0] == 503 and not timeout[2]
        assert request(HOME, cookies)[0] == 200
        report['statement_timeout'] = {'status': timeout[0], 'cookie_changed': bool(timeout[2]),
                                       'elapsed_seconds': round(timeout_elapsed, 3), 'login_retained': True}

        # Existing connection failures must not destroy authentication.
        failed = request('/_test/database-error/', cookies)
        report['query_error'] = {'status': failed[0], 'cookie_changed': bool(failed[2]),
                                 'subsequent_login_valid': request(HOME, cookies)[0] == 200}
        assert failed[0] >= 500 and not failed[2]
        assert report['query_error']['subsequent_login_valid']

        # Deliberately exhaust ONLY this disposable 20-connection instance.
        hogs = []
        try:
            monitor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s "
                "AND application_name='login_predeploy_workers'", (db_env['DB_NAME'],))
            for _ in range(30):
                try:
                    hogs.append(psycopg.connect(**options, autocommit=True))
                except psycopg.OperationalError:
                    break
            else:
                raise AssertionError('Disposable test instance did not reach its configured limit.')
            start = time.monotonic()
            with ThreadPoolExecutor(max_workers=capacity) as pool:
                failures = list(pool.map(lambda _: request(HOME, cookies), range(capacity)))
            report['connection_exhaustion'] = {
                'statuses': [response[0] for response in failures],
                'cookie_changed': any(bool(response[2]) for response in failures),
                'elapsed_seconds': round(time.monotonic() - start, 3),
            }
            assert all(response[0] == 503 and not response[2] for response in failures)
        finally:
            for connection in hogs:
                connection.close()
        report['connection_exhaustion']['recovered_login_valid'] = request(HOME, cookies)[0] == 200
        assert report['connection_exhaustion']['recovered_login_valid']
        # Ensure no worker transaction/row lock remains after error recovery.
        report['remaining_worker_transactions'] = monitor.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname=%s "
            "AND application_name='login_predeploy_workers' AND xact_start IS NOT NULL",
            (db_env['DB_NAME'],)).fetchone()[0]
        assert report['remaining_worker_transactions'] == 0
    print('PREDEPLOY ' + json.dumps(report, sort_keys=True))
    return report
