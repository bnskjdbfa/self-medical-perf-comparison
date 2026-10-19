"""Ordered request/response regressions. All identities are synthetic fixtures."""
from copy import deepcopy
from unittest.mock import patch

from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.db import DatabaseError
from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from config.session_middleware import ConcurrentSessionMiddleware
from surveys.models import DocumentSubmission
from surveys import tests as flow_tests
from surveys.views import SESSION_PRE_IDENTITY_PRIVACY_CONSENT, SESSION_RESPONDENT_ID


class LoginConcurrencyTest(TestCase):
    setUp = flow_tests.RequiredDocumentFlowTest.setUp
    begin_pre_identity_consent = flow_tests.RequiredDocumentFlowTest.begin_pre_identity_consent

    def authenticate(self):
        self.begin_pre_identity_consent()
        self.prelogin_cookies = deepcopy(self.client.cookies)
        response = self.client.post(reverse('surveys:preview-v2-identity'), {
            'name': self.participant.name, 'birth_date': '900101', 'gender': 'M',
        })
        self.assertEqual(response.status_code, 302)
        self.auth_key = self.client.cookies[settings.LOGIN_AUTH_COOKIE_NAME].value
        return response

    def assert_still_authenticated(self):
        response = self.client.get(reverse('surveys:preview-v2-home'))
        self.assertRedirects(response, reverse('surveys:preview-v2-documents-next'),
                             fetch_redirect_response=False)
        from config.session_backend import load_context
        _, key = load_context({name: cookie.value for name, cookie in self.client.cookies.items()})
        self.assertIsNotNone(key)
        store = SessionStore(key)
        self.assertEqual(store[SESSION_RESPONDENT_ID], self.respondent.pk)

    def test_repeated_code_preserves_pre_identity_consent(self):
        self.begin_pre_identity_consent()
        before = self.client.session[SESSION_PRE_IDENTITY_PRIVACY_CONSENT]
        self.client.post(reverse('surveys:preview-v2-code'), {'team_code': self.team.code})
        self.assertEqual(self.client.session[SESSION_PRE_IDENTITY_PRIVACY_CONSENT], before)

    def test_authenticated_code_replay_cannot_switch_or_clear_identity(self):
        self.authenticate()
        self.client.post(reverse('surveys:preview-v2-code'), {'team_code': 'different-code'})
        self.assert_still_authenticated()
        self.assertEqual(self.client.cookies[settings.LOGIN_AUTH_COOKIE_NAME].value, self.auth_key)

    def test_authenticated_identity_replay_is_idempotent(self):
        self.authenticate()
        count = DocumentSubmission.objects.count()
        response = self.client.post(reverse('surveys:preview-v2-identity'), {
            'name': self.participant.name, 'birth_date': '900101', 'gender': 'M',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(DocumentSubmission.objects.count(), count)
        self.assertEqual(self.client.cookies[settings.LOGIN_AUTH_COOKIE_NAME].value, self.auth_key)
        self.assert_still_authenticated()

    def test_retired_identity_post_has_no_cookie_side_effects(self):
        self.authenticate()
        stale = Client()
        stale.cookies = self.prelogin_cookies
        response = stale.post(reverse('surveys:preview-v2-identity'), {
            'name': self.participant.name, 'birth_date': '900101', 'gender': 'M',
        }, HTTP_X_LOGIN_REQUEST='1')
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.cookies)
        self.client.cookies.update(response.cookies)
        self.assert_still_authenticated()

    def test_late_code_response_cannot_replace_authenticated_cookie(self):
        self.authenticate()
        stale = Client()
        stale.cookies = self.prelogin_cookies
        response = stale.post(reverse('surveys:preview-v2-code'), {'team_code': self.team.code})
        self.assertNotIn(settings.LOGIN_AUTH_COOKIE_NAME, response.cookies)
        # Browser receives this response AFTER the authentication response.
        self.client.cookies.update(response.cookies)
        self.assert_still_authenticated()

    def test_late_cookie_deletion_cannot_log_out_browser(self):
        self.authenticate()
        stale = Client()
        stale.cookies = self.prelogin_cookies
        response = stale.get(reverse('surveys:preview-v2-home'))
        self.assertNotIn(settings.LOGIN_AUTH_COOKIE_NAME, response.cookies)
        self.client.cookies.update(response.cookies)
        self.assert_still_authenticated()

    def test_response_order_before_authentication_is_also_safe(self):
        self.begin_pre_identity_consent()
        stale = Client()
        stale.cookies = deepcopy(self.client.cookies)
        duplicate = stale.post(reverse('surveys:preview-v2-code'), {'team_code': self.team.code})
        self.client.cookies.update(duplicate.cookies)
        response = self.client.post(reverse('surveys:preview-v2-identity'), {
            'name': self.participant.name, 'birth_date': '900101', 'gender': 'M',
        })
        self.assertEqual(response.status_code, 302)
        self.assert_still_authenticated()

    def test_old_key_cannot_authenticate_even_after_rotation(self):
        self.authenticate()
        old_key = self.prelogin_cookies[settings.SESSION_COOKIE_NAME].value
        self.assertFalse(Session.objects.filter(session_key=old_key).exists())
        stale = Client()
        stale.cookies = self.prelogin_cookies
        response = stale.get(reverse('surveys:preview-v2-home'))
        self.assertRedirects(response, reverse('surveys:preview-v2-code'),
                             fetch_redirect_response=False)

    def test_auth_cookie_has_same_security_and_expiry_as_django_cookie(self):
        response = self.authenticate()
        normal = response.cookies[settings.SESSION_COOKIE_NAME]
        auth = response.cookies[settings.LOGIN_AUTH_COOKIE_NAME]
        self.assertEqual(dict(auth), dict(normal))
        self.assertTrue(auth['httponly'])
        self.assertEqual(auth['samesite'], 'Lax')

    def test_reset_revokes_old_key_without_deleting_a_later_login_cookie(self):
        self.authenticate()
        response = self.client.post(reverse('surveys:preview-v2-reset-login'))
        self.assertNotIn(settings.LOGIN_AUTH_COOKIE_NAME, response.cookies)
        self.assertFalse(Session.objects.filter(session_key=self.auth_key).exists())
        response = self.client.get(reverse('surveys:preview-v2-home'))
        self.assertRedirects(response, reverse('surveys:preview-v2-code'),
                             fetch_redirect_response=False)

    def test_authenticated_update_does_not_refresh_protected_cookie(self):
        self.authenticate()
        response = self.client.get(reverse('surveys:preview-v2-code') + '?lang=en')
        self.assertIn(settings.SESSION_COOKIE_NAME, response.cookies)
        self.assertNotIn(settings.LOGIN_AUTH_COOKIE_NAME, response.cookies)
        self.assert_still_authenticated()

    def test_existing_login_gets_protected_cookie_on_first_read(self):
        self.authenticate()
        del self.client.cookies[settings.LOGIN_AUTH_COOKIE_NAME]
        response = self.client.get(reverse('surveys:preview-v2-home'))
        self.assertEqual(response.cookies[settings.LOGIN_AUTH_COOKIE_NAME].value, self.auth_key)
        self.assert_still_authenticated()

    def test_late_reset_response_cannot_clear_a_new_login(self):
        self.authenticate()
        reset = self.client.post(reverse('surveys:preview-v2-reset-login'))
        self.authenticate()
        self.client.cookies.update(reset.cookies)
        self.assert_still_authenticated()

    def test_ajax_redirect_is_json_and_validation_error_can_retry(self):
        response = self.client.post(reverse('surveys:preview-v2-code'),
                                    {'team_code': 'invalid'}, HTTP_X_LOGIN_REQUEST='1')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-login-form')
        response = self.client.post(reverse('surveys:preview-v2-code'),
                                    {'team_code': self.team.code}, HTTP_X_LOGIN_REQUEST='1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['redirect_url'], reverse('surveys:preview-v2-privacy-consent'))
        self.assertIn('no-store', response['Cache-Control'])

    def test_diagnostics_do_not_contain_identity_or_cookie_values(self):
        with self.assertLogs('login.session', level='INFO') as logs:
            self.authenticate()
            self.client.post(reverse('surveys:preview-v2-identity'), {})
        rendered = ' '.join(logs.output)
        for secret in (self.participant.name, self.team.code, '900101', self.auth_key):
            self.assertNotIn(secret, rendered)
        self.assertIn('reason=identity_authenticated', rendered)
        self.assertIn('reason=authenticated_identity_replay', rendered)

    def test_database_failure_has_retryable_response_without_cookie_changes(self):
        with patch('config.session_middleware.load_context', side_effect=DatabaseError):
            response = self.client.post(reverse('surveys:preview-v2-code'), {})
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.cookies)

    def test_failure_after_rotation_rolls_back_deletion_and_sends_no_cookie(self):
        store = SessionStore()
        store['synthetic'] = True
        store.save()
        old_key = store.session_key
        request = RequestFactory().get('/')
        request.COOKIES[settings.SESSION_COOKIE_NAME] = old_key

        def fail_after_rotation(request):
            request.session.cycle_key()
            return HttpResponse(status=500)

        response = ConcurrentSessionMiddleware(fail_after_rotation)(request)
        self.assertFalse(response.cookies)
        self.assertTrue(Session.objects.filter(session_key=old_key).exists())

    def test_parallel_branch_late_mirror_cannot_resurrect_after_reset(self):
        self.authenticate()
        late = self.client.get(reverse('surveys:preview-v2-code') + '?lang=en')
        self.client = Client()
        self.authenticate()
        self.client.post(reverse('surveys:preview-v2-reset-login'))
        self.client.cookies.update(late.cookies)
        response = self.client.get(reverse('surveys:preview-v2-home'))
        self.assertRedirects(response, reverse('surveys:preview-v2-code'), fetch_redirect_response=False)

    def test_delayed_genuine_legacy_promotion_does_not_replace_new_generation(self):
        self.authenticate()
        legacy_store = SessionStore()
        legacy_store.update(SessionStore(self.auth_key).load())
        legacy_store.save()
        legacy = Client()
        legacy.cookies[settings.SESSION_COOKIE_NAME] = legacy_store.session_key
        before = deepcopy(legacy.cookies)
        late = legacy.get(reverse('surveys:preview-v2-code') + '?lang=en')
        legacy.cookies = before
        legacy.post(reverse('surveys:preview-v2-reset-login'))
        self.client = Client()
        self.authenticate()
        self.client.cookies.update(late.cookies)
        self.assert_still_authenticated()

    def test_reset_with_retired_key_revokes_inflight_rotation_without_restoring_auth(self):
        from django.contrib.auth import get_user_model
        self.authenticate()
        before = deepcopy(self.client.cookies)
        user = get_user_model().objects.create_user('rotation-fixture', password='Fixture-only-pass-2026', is_staff=True)
        late = self.client.post(reverse('admin:login'), {
            'username': user.username, 'password': 'Fixture-only-pass-2026',
            'next': reverse('surveys:preview-v2-home'),
        })
        self.assertEqual(late.status_code, 302)
        self.client.cookies = before
        self.client.post(reverse('surveys:preview-v2-reset-login'))
        self.client.cookies.update(late.cookies)
        response = self.client.get(reverse('surveys:preview-v2-home'))
        self.assertRedirects(response, reverse('surveys:preview-v2-code'), fetch_redirect_response=False)

    def test_registered_old_key_without_scope_is_never_promoted(self):
        self.authenticate()
        stripped = Client()
        stripped.cookies[settings.SESSION_COOKIE_NAME] = self.auth_key
        response = stripped.get(reverse('surveys:preview-v2-home'))
        self.assertRedirects(response, reverse('surveys:preview-v2-code'), fetch_redirect_response=False)

    def test_logout_with_retired_key_revokes_inflight_admin_login(self):
        from django.contrib.auth import get_user_model
        self.authenticate()
        before = deepcopy(self.client.cookies)
        user = get_user_model().objects.create_user('logout-fixture', password='Fixture-only-pass-2026', is_staff=True)
        late = self.client.post(reverse('admin:login'), {'username': user.username, 'password': 'Fixture-only-pass-2026'})
        self.assertEqual(late.status_code, 302)
        self.client.cookies = before
        self.assertEqual(self.client.post(reverse('admins:logout')).status_code, 302)
        self.client.cookies.update(late.cookies)
        response = self.client.get(reverse('surveys:preview-v2-home'))
        self.assertRedirects(response, reverse('surveys:preview-v2-code'), fetch_redirect_response=False)

    def test_disjoint_concurrent_draft_changes_are_merged(self):
        from config.session_backend import load_context, SessionStore as GuardedStore
        self.authenticate()
        cookies = {name: value.value for name, value in self.client.cookies.items()}
        first_context, key = load_context(cookies)
        second_context, _ = load_context(cookies)
        first, second = GuardedStore(key, context=first_context), GuardedStore(key, context=second_context)
        first['draft_left'] = 'left'
        second['draft_right'] = 'right'
        first.save()
        second.save()
        result = SessionStore(key)
        self.assertEqual(result['draft_left'], 'left')
        self.assertEqual(result['draft_right'], 'right')

    def test_conflicting_changes_fail_without_overwriting_saved_state(self):
        from config.session_backend import load_context, SessionStore as GuardedStore, SessionConflict
        self.authenticate()
        cookies = {name: value.value for name, value in self.client.cookies.items()}
        first_context, key = load_context(cookies)
        second_context, _ = load_context(cookies)
        first, second = GuardedStore(key, context=first_context), GuardedStore(key, context=second_context)
        first['draft_same'] = 'left'
        second['draft_same'] = 'right'
        first.save()
        with self.assertRaises(SessionConflict):
            second.save()
        self.assertEqual(SessionStore(key)['draft_same'], 'left')

    def test_expired_generation_has_csrf_protected_reset_and_can_restart(self):
        from datetime import timedelta
        from django.utils import timezone
        self.authenticate()
        Session.objects.filter(session_key=self.auth_key).update(expire_date=timezone.now() - timedelta(seconds=1))
        page = self.client.get(reverse('surveys:preview-v2-code'))
        self.assertContains(page, reverse('surveys:preview-v2-reset-login'))
        self.assertContains(page, 'csrfmiddlewaretoken')
        self.assertEqual(self.client.post(reverse('surveys:preview-v2-reset-login')).status_code, 302)
        page = self.client.get(reverse('surveys:preview-v2-code'))
        self.assertContains(page, 'data-login-form')

    def test_legacy_draft_save_before_500_does_not_promote_or_retire_key(self):
        from config.session_backend import digest
        from surveys.models import LoginSessionCredential
        legacy = SessionStore()
        legacy['fixture'] = 'kept'
        legacy.save()
        request = RequestFactory().get('/')
        request.COOKIES[settings.SESSION_COOKIE_NAME] = legacy.session_key

        def view(request):
            request.session['draft'] = 'saved'
            request.session.save()
            return HttpResponse(status=500)

        response = ConcurrentSessionMiddleware(view)(request)
        self.assertEqual(response.status_code, 500)
        self.assertFalse(response.cookies)
        self.assertEqual(SessionStore(legacy.session_key)['draft'], 'saved')
        self.assertFalse(LoginSessionCredential.objects.filter(key_hash=digest(legacy.session_key)).exists())

    def test_expired_family_cleanup_cannot_restore_a_leftover_legacy_row(self):
        from datetime import timedelta
        from io import StringIO
        from django.core.management import call_command
        from django.utils import timezone
        from surveys.models import LoginSessionFamily, LoginSessionCredential
        self.authenticate()
        credential = LoginSessionCredential.objects.get(session_key=self.auth_key)
        LoginSessionFamily.objects.filter(pk=credential.family_id).update(expires_at=timezone.now() - timedelta(seconds=1))
        call_command('purge_login_session_families', stdout=StringIO())
        self.assertFalse(Session.objects.filter(session_key=self.auth_key).exists())
        stripped = Client()
        stripped.cookies[settings.SESSION_COOKIE_NAME] = self.auth_key
        response = stripped.get(reverse('surveys:preview-v2-home'))
        self.assertRedirects(response, reverse('surveys:preview-v2-code'), fetch_redirect_response=False)

    def test_first_legacy_rotation_is_revoked_by_previously_loaded_reset(self):
        from config.session_backend import load_context, SessionStore as GuardedStore
        legacy = SessionStore()
        legacy['fixture'] = 'authenticated-state'
        legacy.save()
        cookies = {settings.SESSION_COOKIE_NAME: legacy.session_key}
        reset_context, key = load_context(cookies)
        reset = GuardedStore(key, context=reset_context)
        rotation_context, _ = load_context(cookies)
        rotation = GuardedStore(key, context=rotation_context)
        rotation.cycle_key()
        rotation.save(finalizing=True)
        replacement = rotation.session_key
        reset.flush()
        reset.save(finalizing=True)
        self.assertFalse(Session.objects.filter(session_key=replacement).exists())
        rotation_context.family.refresh_from_db()
        self.assertIsNone(rotation_context.family.active_generation)

    def test_unadopted_legacy_logout_deletes_original_session(self):
        legacy = SessionStore()
        legacy['fixture'] = 'kept-until-logout'
        legacy.save()
        client = Client()
        client.cookies[settings.SESSION_COOKIE_NAME] = legacy.session_key
        self.assertEqual(client.post(reverse('admins:logout')).status_code, 302)
        self.assertFalse(Session.objects.filter(session_key=legacy.session_key).exists())

    def test_equivalent_session_assignment_emits_no_write_or_cookie(self):
        from config.session_backend import SessionStore as GuardedStore
        self.authenticate()
        request = RequestFactory().get('/')
        request.COOKIES = {name: value.value for name, value in self.client.cookies.items()
                           if value.value and str(value['max-age']) != '0'}
        def view(request):
            request.session[SESSION_RESPONDENT_ID] = request.session[SESSION_RESPONDENT_ID]
            return HttpResponse('ok')
        with patch.object(GuardedStore, 'save', side_effect=AssertionError('unexpected session write')):
            response = ConcurrentSessionMiddleware(view)(request)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.cookies)

    def test_expiry_rotation_and_revocation_are_always_dirty(self):
        from config.session_backend import load_context, SessionStore as GuardedStore
        self.authenticate()
        cookies = {name: value.value for name, value in self.client.cookies.items()}
        context, key = load_context(cookies)
        store = GuardedStore(key, context=context)
        self.assertFalse(store.has_changes())
        store.set_expiry(1234)
        self.assertTrue(store.has_changes())
        store.save()
        self.assertFalse(store.has_changes())
        store.cycle_key()
        self.assertTrue(store.has_changes())
        store.flush()
        self.assertTrue(store.has_changes())

    def test_home_reads_document_state_once_and_next_request_checks_fresh_policy(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.authenticate()
        self.features.enable_privacy_consent = False
        self.features.enable_third_party_consent = False
        self.features.enable_safety_pledge = False
        self.features.save()
        home = reverse('surveys:preview-v2-home')
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(home + '?lang=ko')
        self.assertEqual(response.status_code, 200)
        for table in ('team_feature_settings', 'document_submissions'):
            reads = [row for row in captured if row['sql'].lstrip().upper().startswith('SELECT')
                     and f'"{table}"' in row['sql']]
            self.assertEqual(len(reads), 1, table)
        self.features.enable_third_party_consent = True
        self.features.save()
        response = self.client.get(home)
        self.assertRedirects(response, reverse('surveys:preview-v2-documents-next'),
                             fetch_redirect_response=False)
