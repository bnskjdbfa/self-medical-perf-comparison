"""Rotation must insert fresh keys and preserve atomic revocation on conflicts."""
from unittest.mock import patch

from django.contrib.sessions.models import Session
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from config.session_backend import (
    SCOPE_PREFIX, TOKEN_PREFIX, SessionConflict, SessionStore, load_context,
)
from surveys.models import LoginSessionCredential


class SessionRotationWriteTest(TestCase):
    def create_store(self, marker):
        store = SessionStore()
        store['fixture_marker'] = marker
        store.save(finalizing=True)
        return store

    def cookies(self, store):
        return {SCOPE_PREFIX + store.context.family.id.hex: store.context.secret,
                TOKEN_PREFIX + store.context.credential.id.hex: store.session_key}

    def reload(self, cookies):
        context, key = load_context(cookies)
        return SessionStore(key, context=context)

    def test_rotation_has_one_locked_session_read_and_no_inner_savepoints(self):
        initial = self.create_store('original')
        cookies = self.cookies(initial)
        store = self.reload(cookies)
        store.cycle_key()
        store['fixture_marker'] = 'rotated'
        with CaptureQueriesContext(connection) as queries:
            store.save(finalizing=True)
        # TestCase's surrounding transaction makes bounded_atomic create one
        # SAVEPOINT. The fresh session write adds no savepoints of its own.
        self.assertEqual(sum(q['sql'].startswith('SAVEPOINT ') for q in queries), 1)
        self.assertEqual(sum(q['sql'].startswith('SELECT ') and 'FROM "django_session"' in q['sql']
                             for q in queries), 1)
        self.assertNotEqual(initial.session_key, store.session_key)
        self.assertFalse(Session.objects.filter(session_key=initial.session_key).exists())
        self.assertEqual(Session.objects.get(session_key=store.session_key).get_decoded()['fixture_marker'], 'rotated')
        _, retired = load_context(cookies)
        self.assertIsNone(retired)

    def test_collision_never_overwrites_another_session_or_retires_old_key(self):
        initial = self.create_store('original')
        other = self.create_store('other')
        cookies = self.cookies(initial)
        count = LoginSessionCredential.objects.count()
        store = self.reload(cookies)
        with patch.object(store, '_get_new_session_key', return_value=other.session_key):
            store.cycle_key()
        store['fixture_marker'] = 'must-rollback'
        with self.assertRaisesRegex(SessionConflict, '^session_key_collision$'):
            store.save(finalizing=True)
        self.assertEqual(Session.objects.get(session_key=other.session_key).get_decoded()['fixture_marker'], 'other')
        self.assertEqual(Session.objects.get(session_key=initial.session_key).get_decoded()['fixture_marker'], 'original')
        self.assertEqual(LoginSessionCredential.objects.count(), count)
        self.assertEqual(self.reload(cookies)['fixture_marker'], 'original')

    def test_rotation_cannot_reuse_its_own_old_key(self):
        initial = self.create_store('original')
        cookies = self.cookies(initial)
        store = self.reload(cookies)
        with patch.object(store, '_get_new_session_key', return_value=initial.session_key):
            store.cycle_key()
        with self.assertRaisesRegex(SessionConflict, '^session_key_collision$'):
            store.save(finalizing=True)
        self.assertEqual(self.reload(cookies)['fixture_marker'], 'original')

    def test_collision_response_changes_no_cookies_and_preserves_existing_session(self):
        from django.http import HttpResponse
        from django.test import RequestFactory
        from config.session_middleware import ConcurrentSessionMiddleware
        initial = self.create_store('original')
        other = self.create_store('other')
        cookies = self.cookies(initial)
        request = RequestFactory().post('/surveys/preview/v2/identity/')
        request.COOKIES.update(cookies)
        def view(request):
            request.session.cycle_key()
            request.session['fixture_marker'] = 'must-rollback'
            return HttpResponse('fixture')
        with patch.object(SessionStore, '_get_new_session_key', return_value=other.session_key):
            response = ConcurrentSessionMiddleware(view)(request)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.cookies)
        self.assertEqual(self.reload(cookies)['fixture_marker'], 'original')

    def test_unrotated_legacy_adoption_updates_only_the_locked_original_row(self):
        from django.contrib.sessions.backends.db import SessionStore as LegacyStore
        from django.conf import settings
        legacy = LegacyStore()
        legacy['fixture_marker'] = 'legacy'
        legacy.save()
        store = self.reload({settings.SESSION_COOKIE_NAME: legacy.session_key})
        store['fixture_extra'] = 'adopted'
        store.save(finalizing=True)
        self.assertEqual(store.session_key, legacy.session_key)
        self.assertEqual(Session.objects.get(session_key=legacy.session_key).get_decoded()['fixture_extra'], 'adopted')
        self.assertIsNotNone(store.context.credential)

    def test_failed_generation_insert_rolls_back_new_row_and_preserves_old_auth(self):
        from django.db import IntegrityError
        initial = self.create_store('original')
        cookies = self.cookies(initial)
        store = self.reload(cookies)
        store.cycle_key()
        candidate = store.session_key
        with patch.object(LoginSessionCredential.objects, 'create', side_effect=IntegrityError('fixture-only')):
            with self.assertRaisesRegex(SessionConflict, '^duplicate_session_adoption$'):
                store.save(finalizing=True)
        self.assertFalse(Session.objects.filter(session_key=candidate).exists())
        self.assertEqual(self.reload(cookies)['fixture_marker'], 'original')

    def test_candidate_generation_does_not_query_database(self):
        store = self.create_store('original')
        with self.assertNumQueries(0):
            store.cycle_key()
        self.assertEqual(len(store.session_key), 32)
        self.assertRegex(store.session_key, r'^[a-z0-9]{32}$')
        self.assertNotEqual(store.session_key, store.original_key)

    def test_anonymous_candidate_collision_preserves_other_family(self):
        other = self.create_store('existing')
        fresh = SessionStore()
        fresh['fixture_marker'] = 'must-rollback'
        from surveys.models import LoginSessionFamily
        count = LoginSessionFamily.objects.count()
        with patch.object(fresh, '_get_new_session_key', return_value=other.session_key):
            with self.assertRaisesRegex(SessionConflict, '^session_key_collision$'):
                fresh.save(finalizing=True)
        self.assertEqual(LoginSessionFamily.objects.count(), count)
        self.assertEqual(self.reload(self.cookies(other))['fixture_marker'], 'existing')

    def test_cookie_free_request_skips_session_read_transaction(self):
        from django.http import HttpResponse
        from django.test import RequestFactory
        from config.session_middleware import ConcurrentSessionMiddleware
        for cookies in ({}, {'csrftoken': 'fixture-csrf'}):
            request = RequestFactory().get('/fixture/')
            request.COOKIES.update(cookies)
            with patch('config.session_middleware.bounded_atomic', side_effect=AssertionError('unexpected transaction')):
                with self.assertNumQueries(0):
                    response = ConcurrentSessionMiddleware(lambda req: HttpResponse('ok'))(request)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.cookies)

    def test_session_cookie_inputs_keep_bounded_read(self):
        from django.conf import settings
        from django.http import HttpResponse
        from django.test import RequestFactory
        from contextlib import nullcontext
        from config.session_middleware import ConcurrentSessionMiddleware
        for name in (settings.SESSION_COOKIE_NAME, settings.LOGIN_AUTH_COOKIE_NAME,
                     SCOPE_PREFIX + 'malformed', TOKEN_PREFIX + 'malformed'):
            request = RequestFactory().get('/fixture/')
            request.COOKIES[name] = 'fixture'
            with patch('config.session_middleware.connection') as conn, patch('config.session_middleware.bounded_atomic', return_value=nullcontext()) as bounded:
                conn.vendor = 'postgresql'
                response = ConcurrentSessionMiddleware(lambda req: HttpResponse('ok'))(request)
            bounded.assert_called_once_with()
            self.assertEqual(response.status_code, 200)
