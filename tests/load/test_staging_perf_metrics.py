"""Measurement helpers only; no database or application behavior changes."""
import unittest
from unittest.mock import patch
from staging_perf_metrics import Measurement, sql_kind, route_name, instrument_method, active


class StagingMetricsTest(unittest.TestCase):
    def test_sql_and_routes_never_echo_unknown_input(self):
        self.assertEqual(sql_kind('SELECT secret FROM unknown_table'), 'select')
        self.assertEqual(sql_kind('SAVEPOINT "arbitrary-name"'), 'savepoint')
        self.assertEqual(sql_kind('SELECT set_config(%s,%s,true)'), 'transaction_settings')
        self.assertIsNone(route_name('/private/person-value/'))
        self.assertEqual(route_name('/surveys/day/17/result/'), 'result')

    def test_nested_session_spans_are_not_double_counted(self):
        measurement = Measurement()
        with patch('staging_perf_metrics.time.perf_counter', side_effect=[0, 1, 3]):
            with measurement.session_span():
                with measurement.session_span():
                    pass
        self.assertEqual(measurement.values['session_ms'], 3000)

    def test_failed_query_is_counted_without_retaining_sql_or_params(self):
        measurement = Measurement()
        def fail(*args):
            raise ValueError('fixture')
        with self.assertRaises(ValueError):
            measurement.execute(fail, 'SELECT %s FROM unknown_table', ['not-for-output'], False, {})
        self.assertEqual(measurement.values['sql_count'], 1)
        self.assertEqual(measurement.sequence[0]['kind'], 'select')
        self.assertNotIn('not-for-output', repr(measurement.__dict__))

    def test_native_health_check_is_measured_separately_and_unprofiled_is_unchanged(self):
        class Driver:
            def is_usable(self):
                return True
        instrument_method(Driver, 'is_usable', 'healthcheck_ms', 'health_checks')
        self.assertTrue(Driver().is_usable())
        measurement = Measurement()
        token = active.set(measurement)
        try:
            with patch('staging_perf_metrics.time.perf_counter', side_effect=[1, 1.005]):
                self.assertTrue(Driver().is_usable())
        finally:
            active.reset(token)
        self.assertAlmostEqual(measurement.values['healthcheck_ms'], 5)
        self.assertEqual(measurement.values['health_checks'], 1)
        self.assertEqual(measurement.values['sql_count'], 0)


if __name__ == '__main__':
    unittest.main()
