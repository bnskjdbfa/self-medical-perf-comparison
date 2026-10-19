import json
import unittest
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


if __name__ == '__main__':
    unittest.main()
