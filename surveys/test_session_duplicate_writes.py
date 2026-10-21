"""Generation-safe duplicate saves and bounded ordinary-family contention."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading
import time
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.sessions.models import Session
from django.db import connection, connections, transaction, DatabaseError
from django.http import HttpResponse
from django.test import TestCase, TransactionTestCase, RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from config.session_backend import SessionStore, load_context, SCOPE_PREFIX, TOKEN_PREFIX, SessionConflict
from config.session_middleware import ConcurrentSessionMiddleware
from surveys.models import LoginSessionFamily, SurveySubmission
from surveys import tests as flow_tests


def make_store():
    store = SessionStore()
    store['fixture'] = 'original'
    store.save(finalizing=True)
    return store


def cookies(store):
    return {SCOPE_PREFIX + store.context.family.id.hex: store.context.secret,
            TOKEN_PREFIX + store.context.credential.id.hex: store.session_key}


def reload_store(jar):
    ctx, key = load_context(jar)
    return SessionStore(key, context=ctx)


class DuplicateSessionWriteTest(TestCase):
    def test_explicit_unchanged_save_does_not_write_or_renew_expiry(self):
        original = make_store()
        store = reload_store(cookies(original))
        expiry = Session.objects.get(session_key=store.session_key).expire_date
        store['fixture'] = 'original'
        with self.assertNumQueries(0):
            store.save()
        self.assertEqual(Session.objects.get(session_key=store.session_key).expire_date, expiry)

    def test_explicit_save_preserves_nested_edits_without_modified_flag(self):
        original = make_store()
        original['nested'] = {'value': 'before'}
        original.save()
        store = reload_store(cookies(original))
        store['nested']['value'] = 'after'
        self.assertFalse(store.modified)
        store.save()
        self.assertEqual(reload_store(cookies(original))['nested']['value'], 'after')

    def test_identical_concurrent_change_does_not_rewrite_rows(self):
        original = make_store()
        first, second = (reload_store(cookies(original)) for _ in range(2))
        for store in (first, second):
            store['fixture'] = 'same-completed-value'
        first.save()
        with CaptureQueriesContext(connection) as queries:
            second.save()
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('UPDATE ', 'INSERT ', 'DELETE '))
                             for q in queries))
        self.assertFalse(second.has_changes())
        self.assertEqual(reload_store(cookies(original))['fixture'], 'same-completed-value')

    def test_identical_pending_change_cannot_write_through_retired_generation(self):
        original = make_store()
        delayed = reload_store(cookies(original))
        delayed['fixture'] = 'new-auth'
        rotated = reload_store(cookies(original))
        rotated.cycle_key()
        rotated['fixture'] = 'new-auth'
        rotated.save()
        with self.assertRaisesRegex(SessionConflict, 'retired_session'):
            delayed.save()
        self.assertIsNone(load_context(cookies(original))[1])
        self.assertEqual(reload_store(cookies(rotated))['fixture'], 'new-auth')


class FinalSurveyDraftTest(TestCase):
    setUp = flow_tests.SurveyFlowTest.setUp
    _start_session = flow_tests.SurveyFlowTest._start_session

    def test_final_submission_does_not_create_partial_draft(self):
        self._start_session()
        # Adopt the fixture legacy session before observing explicit writes.
        self.client.get(reverse('surveys:day-survey', kwargs={'day_no': 1}))
        with patch.object(SessionStore, 'save', autospec=True, side_effect=AssertionError('unexpected session write')):
            response = self.client.post(reverse('surveys:day-survey', kwargs={'day_no': 1}),
                {f'q_{self.question.id}': '예', 'submit': '1'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SurveySubmission.objects.count(), 1)
        self.assertNotIn('survey_answers_draft', self.client.session)

    def test_duplicate_insert_race_clears_existing_draft(self):
        from django.db import IntegrityError
        self._start_session()
        store = self.client.session
        store['survey_answers_draft'] = {'fixture-old': 'fixture-answer'}
        store.save()
        with patch('surveys.views.SurveySubmission.objects.create', side_effect=IntegrityError('fixture-duplicate')):
            response = self.client.post(reverse('surveys:day-survey', kwargs={'day_no': 1}),
                {f'q_{self.question.id}': '예', 'submit': '1'})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('survey_answers_draft', self.client.session)


@skipUnless(connection.vendor == 'postgresql', 'Real PostgreSQL row locks required')
@override_settings(SESSION_LOCK_TIMEOUT_MS=200, SESSION_STATEMENT_TIMEOUT_MS=1500)
class OrdinarySessionLockTest(TransactionTestCase):
    @contextmanager
    def hold_family(self, family_id, release_after=None):
        ready, release = threading.Event(), threading.Event()
        def holder():
            try:
                with transaction.atomic():
                    LoginSessionFamily.objects.select_for_update().get(pk=family_id)
                    ready.set()
                    if release_after is None:
                        if not release.wait(5):
                            raise AssertionError('test lock release timed out')
                    else:
                        time.sleep(release_after)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(holder)
            try:
                self.assertTrue(ready.wait(5))
                yield
            finally:
                release.set()
                future.result(timeout=6)

    def test_short_contention_waits_and_preserves_both_changes(self):
        original = make_store()
        store = reload_store(cookies(original))
        store['new-field'] = 'new-value'
        with self.hold_family(original.context.family.id, release_after=.07):
            store.save()
        self.assertEqual(reload_store(cookies(original))['new-field'], 'new-value')

    def test_timeout_changes_no_cookies_or_existing_state_and_recovers(self):
        original = make_store()
        jar = cookies(original)
        request = RequestFactory().post('/fixture/')
        request.COOKIES.update(jar)
        def view(request):
            request.session['new-field'] = 'must-not-persist'
            return HttpResponse('fixture')
        with self.hold_family(original.context.family.id):
            started = time.monotonic()
            response = ConcurrentSessionMiddleware(view)(request)
            elapsed = time.monotonic() - started
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.cookies)
        self.assertGreater(elapsed, .15)
        self.assertLess(elapsed, 1.5)
        restored = reload_store(jar)
        self.assertNotIn('new-field', restored)
        restored['new-field'] = 'retry-success'
        restored.save()
        self.assertEqual(reload_store(jar)['new-field'], 'retry-success')

    def test_rotation_keeps_nowait_and_does_not_change_auth(self):
        original = make_store()
        store = reload_store(cookies(original))
        store.cycle_key()
        with self.hold_family(original.context.family.id):
            started = time.monotonic()
            with self.assertRaises(DatabaseError) as error:
                store.save()
            self.assertLess(time.monotonic() - started, .15)
            self.assertEqual(error.exception.__cause__.sqlstate, '55P03')
        self.assertEqual(load_context(cookies(original))[1], original.session_key)
