import contextlib
import io
import json
import unittest
from unittest.mock import patch, Mock
from staging_queue_worker import StagingQueueWorker, current


class QueueObserverTests(unittest.TestCase):
    def test_queue_clock_context_and_cleanup_on_exception(self):
        worker = object.__new__(StagingQueueWorker)
        seen = []
        def handle(conn):
            seen.append(dict(current.get()))
            current.get().update(route='health_static', status=200)
            raise RuntimeError('secret must not be logged')
        output = io.StringIO()
        with patch('staging_queue_worker.time.monotonic', side_effect=[12, 13]), patch('gunicorn.workers.gthread.ThreadWorker.handle', side_effect=handle), contextlib.redirect_stdout(output):
            with self.assertRaises(RuntimeError):
                worker.handle_observed(object(), 10)
        log = json.loads(output.getvalue())
        self.assertEqual(log['queue_ms'], 2000)
        self.assertEqual(log['worker_handle_ms'], 1000)
        self.assertNotIn('secret', output.getvalue())
        self.assertEqual(len(seen), 1)
        self.assertIsNone(current.get())

    def test_enqueue_preserves_gunicorn_future_tracking(self):
        worker = object.__new__(StagingQueueWorker)
        worker.tpool = Mock()
        worker._wrap_future = Mock()
        conn = Mock()
        with patch('staging_queue_worker.time.monotonic', return_value=5):
            worker.enqueue_req(conn)
        conn.init.assert_called_once_with()
        worker.tpool.submit.assert_called_once_with(worker.handle_observed, conn, 5)
        worker._wrap_future.assert_called_once_with(worker.tpool.submit.return_value, conn)

    def test_non_allowlisted_request_is_not_logged(self):
        worker = object.__new__(StagingQueueWorker)
        output = io.StringIO()
        with patch('gunicorn.workers.gthread.ThreadWorker.handle', return_value=(False, None)), contextlib.redirect_stdout(output):
            worker.handle_observed(None, 0)
        self.assertEqual(output.getvalue(), '')
        self.assertIsNone(current.get())

    def test_worker_refuses_non_staging(self):
        worker = object.__new__(StagingQueueWorker)
        with patch.dict('os.environ', {'APP_ENV': 'production', 'STAGING_PERF_ENABLED': '1'}):
            with self.assertRaises(RuntimeError):
                worker.init_process()

class QueueWsgiTests(unittest.TestCase):
    def test_health_headers_and_no_raw_path_or_cookie_in_state(self):
        import importlib.util
        import sys
        import types
        fake_app = types.ModuleType('staging_identity_diagnostic_wsgi')
        def app(environ, respond):
            respond('200 OK', [('Content-Type', 'text/css')])
            return [b'css']
        fake_app.application = app
        fake_metrics = types.ModuleType('staging_perf_metrics')
        fake_metrics.route_name = lambda path: None
        spec = importlib.util.spec_from_file_location('queue_wsgi_test_target', __file__.replace('test_staging_queue_worker.py', 'staging_queue_wsgi.py'))
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'staging_identity_diagnostic_wsgi': fake_app, 'staging_perf_metrics': fake_metrics}):
            spec.loader.exec_module(module)
        state = {'request_id': 'test-id', 'queue_ms': 6000}
        token = current.set(state)
        try:
            captured = []
            body = module.application({'PATH_INFO': '/static/surveys/public_v2.css', 'REQUEST_METHOD': 'GET', 'HTTP_COOKIE': 'private'}, lambda *args: captured.append(args))
            self.assertEqual(body, [b'css'])
            self.assertEqual(state['route'], 'health_static')
            self.assertEqual(state['status'], 200)
            self.assertIn(('X-Queue-Request-ID', 'test-id'), captured[0][1])
            self.assertNotIn('private', json.dumps(state))
        finally:
            current.reset(token)


if __name__ == '__main__':
    unittest.main()
