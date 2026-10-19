"""Local application-policy checks; these do not claim Render staging coverage."""
from datetime import timedelta

from django.conf import settings
from django.contrib.sessions.models import Session
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings
from django.db import connection, transaction, DatabaseError
from unittest import skipUnless
from django.urls import reverse
from django.utils import timezone

from surveys import test_login_concurrency as concurrency_tests


class LoginDeploymentPolicyTest(TestCase):
    setUp = concurrency_tests.LoginConcurrencyTest.setUp
    begin_pre_identity_consent = concurrency_tests.LoginConcurrencyTest.begin_pre_identity_consent
    authenticate = concurrency_tests.LoginConcurrencyTest.authenticate

    @skipUnless(connection.vendor == 'postgresql', 'PostgreSQL transaction-local settings')
    def test_nested_database_limits_restore_outer_policy(self):
        from config.db_limits import bounded_atomic
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('lock_timeout', '777ms', true), set_config('statement_timeout', '4321ms', true)")
            with bounded_atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT current_setting('lock_timeout'), current_setting('statement_timeout')")
                    self.assertEqual(cursor.fetchone(), ('200ms', '1500ms'))
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('lock_timeout'), current_setting('statement_timeout')")
                self.assertEqual(cursor.fetchone(), ('777ms', '4321ms'))

    @override_settings(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                       SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_PATH='/',
                       SESSION_COOKIE_DOMAIN=None, SECURE_SSL_REDIRECT=False)
    def test_both_cookie_attributes_under_secure_application_policy(self):
        response = self.authenticate()
        names = [name for name in response.cookies if str(response.cookies[name]['max-age']) != '0' and (
            name in (settings.SESSION_COOKIE_NAME, settings.LOGIN_AUTH_COOKIE_NAME)
            or name.startswith(('exam_scope_', 'exam_session_')))]
        self.assertTrue(any(name.startswith('exam_scope_') for name in names))
        self.assertTrue(any(name.startswith('exam_session_') for name in names))
        for name in names:
            cookie = response.cookies[name]
            self.assertTrue(cookie['secure'])
            self.assertTrue(cookie['httponly'])
            self.assertEqual(cookie['samesite'], 'Lax')
            self.assertEqual(cookie['path'], '/')
            self.assertEqual(cookie['domain'], '')
            self.assertGreater(cookie['max-age'], 0)
            self.assertTrue(cookie['expires'])

    def test_expired_database_session_is_rejected_with_both_cookies_present(self):
        self.authenticate()
        Session.objects.filter(session_key=self.auth_key).update(
            expire_date=timezone.now() - timedelta(seconds=1))
        with self.assertLogs('login.session', level='INFO') as logs:
            response = self.client.get(reverse('surveys:preview-v2-home'))
        self.assertRedirects(response, reverse('surveys:preview-v2-code'),
                             fetch_redirect_response=False)
        self.assertIn('reason=session_record_expired', ' '.join(logs.output))

    @override_settings(SECURE_SSL_REDIRECT=False)
    def test_csrf_missing_and_foreign_origin_rejected_valid_origin_accepted(self):
        client = Client(enforce_csrf_checks=True)
        code = reverse('surveys:preview-v2-code')
        client.get(code, secure=True)
        token = client.cookies[settings.CSRF_COOKIE_NAME].value
        payload = {'team_code': self.team.code}
        response = client.post(code, payload, secure=True)
        self.assertEqual(response.status_code, 403)
        response = client.post(code, payload, secure=True,
                               HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN='https://foreign.invalid')
        self.assertEqual(response.status_code, 403)
        response = client.post(code, payload, secure=True,
                               HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN='https://testserver',
                               HTTP_X_LOGIN_REQUEST='1')
        self.assertEqual(response.status_code, 200)
        self.assertIn('redirect_url', response.json())

    @override_settings(SECURE_PROXY_SSL_HEADER=None)
    def test_forwarded_proto_alone_is_not_trusted_by_django(self):
        request = RequestFactory().get('/', HTTP_X_FORWARDED_PROTO='https')
        self.assertFalse(request.is_secure())
        # Gunicorn/proxy must supply the trusted WSGI scheme in this configuration.
        secure = RequestFactory().get('/', secure=True)
        self.assertTrue(secure.is_secure())


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL transaction-local settings')
class DatabaseLimitTransactionTest(TransactionTestCase):
    def test_outer_commit_and_timeout_restore_connection_settings(self):
        from config.db_limits import bounded_atomic
        def limits():
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('lock_timeout'), current_setting('statement_timeout')")
                return cursor.fetchone()
        before = limits()
        self.assertFalse(connection.in_atomic_block)
        with bounded_atomic():
            self.assertEqual(limits(), ('200ms', '1500ms'))
        self.assertEqual(limits(), before)
        with self.assertRaises(DatabaseError):
            with override_settings(SESSION_LOCK_TIMEOUT_MS=20, SESSION_STATEMENT_TIMEOUT_MS=50):
                with bounded_atomic():
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT pg_sleep(0.2)')
        self.assertEqual(limits(), before)
        self.assertFalse(connection.in_atomic_block)
