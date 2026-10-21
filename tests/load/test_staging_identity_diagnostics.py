import json
import unittest
import contextvars
import sys
from collections import Counter
from unittest.mock import patch
from types import SimpleNamespace

from staging_identity_diagnostics import describe_error


class DiagnosticPrivacyTest(unittest.TestCase):
    def test_cause_sqlstate_and_safe_server_reason(self):
        underlying = RuntimeError('SECRET SQL AND PERSON VALUE')
        underlying.sqlstate = '55P03'
        underlying.diag = SimpleNamespace(message_primary='canceling statement due to lock timeout',
                                          context='SECRET SQL')
        outer = RuntimeError('SESSION RAW VALUE')
        outer.__cause__ = underlying
        result = describe_error(outer)
        self.assertEqual(result[1]['sqlstate'], '55P03')
        self.assertEqual(result[1]['server_reason'], 'lock_timeout')
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('SESSION', json.dumps(result))

    def test_unknown_message_and_sqlstate_are_not_printed(self):
        exc = RuntimeError('PRIVATE')
        exc.sqlstate = 'PRIVATE'
        exc.diag = SimpleNamespace(message_primary='PRIVATE')
        result = describe_error(exc)
        self.assertIsNone(result[0]['sqlstate'])
        self.assertEqual(result[0]['server_reason'], 'not_classified')
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_budget_origin_and_execute_phase(self):
        for after in (False, True):
            namespace = {'__name__': 'config.db_limits'}
            exec('import time\nclass DatabaseBudgetExceeded(Exception):\n sqlstate="57014"\n'
                 'def budget():\n started=time.monotonic()\n'
                 + (' result=None\n' if after else '')
                 + ' raise DatabaseBudgetExceeded("PRIVATE")\n', namespace)
            try:
                namespace['budget']()
            except Exception as exc:
                result = describe_error(exc)[0]
                self.assertEqual(result['source'], 'application')
                self.assertEqual(result['sqlstate'], '57014')
                self.assertEqual(result['points'][0]['phase'],
                                 'after_execute' if after else 'before_execute')
                self.assertNotIn('PRIVATE', json.dumps(result))

    def test_57014_external_cancel_is_not_statement_timeout(self):
        exc = RuntimeError('PRIVATE')
        exc.sqlstate = '57014'
        exc.diag = SimpleNamespace(message_primary='canceling statement due to user request')
        self.assertEqual(describe_error(exc)[0]['server_reason'], 'external_cancel')


    def test_session_diagnostics_join_and_failure_isolation(self):
        from staging_identity_diagnostics import install
        from contextlib import ExitStack
        for reason in ('session_database_busy', 'database_busy', 'identity_database_busy'):
            events = []
            views = SimpleNamespace(login_event=lambda request, reason: events.append(reason))
            middleware = SimpleNamespace(login_event=views.login_event)
            settings = SimpleNamespace(APP_ENV='staging', DB_IS_PRODUCTION=False,
                SESSION_LOCK_TIMEOUT_MS=200, SESSION_STATEMENT_TIMEOUT_MS=1500)
            active = contextvars.ContextVar('test_measurement', default=None)
            measurement = SimpleNamespace(request_id='a' * 32, values=Counter(sql_count=7, query_ms=600))
            request = SimpleNamespace(login_request_id='b' * 32)
            with ExitStack() as stack:
                stack.enter_context(patch.dict(sys.modules, {
                    'django.conf': SimpleNamespace(settings=settings),
                    'config.session_middleware': middleware,
                    'surveys.views': views,
                    'staging_perf_metrics': SimpleNamespace(active=active)}))
                stack.enter_context(patch('config.session_middleware', middleware, create=True))
                stack.enter_context(patch('surveys.views', views, create=True))
                install()
                install()  # Idempotent; do not stack logging wrappers.
                token = active.set(measurement)
                try:
                    try:
                        raise RuntimeError('PRIVATE SQL COOKIE PERSON')
                    except RuntimeError:
                        with patch('builtins.print') as output:
                            views.login_event(request, reason)
                        record = json.loads(output.call_args.args[0])
                        self.assertEqual(record['perf_request_id'], 'a' * 32)
                        self.assertEqual(record['reason'], reason)
                        self.assertEqual(record['event'], 'staging_identity_database_error'
                            if reason == 'identity_database_busy' else 'staging_session_database_error')
                        self.assertNotIn('PRIVATE', json.dumps(record))
                        with patch('builtins.print', side_effect=OSError()):
                            views.login_event(request, reason)
                    self.assertEqual(events, [reason, reason])
                finally:
                    active.reset(token)

    def test_session_trace_location_without_locals(self):
        namespace = {'__name__': 'config.session_backend'}
        exec('def save():\n secret="PRIVATE"\n raise RuntimeError(secret)\n', namespace)
        try:
            namespace['save']()
        except RuntimeError as exc:
            result = describe_error(exc)
        self.assertEqual(result[0]['points'][0]['function'], 'save')
        self.assertNotIn('PRIVATE', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
