"""Opt-in staging gthread observer. Does not prioritize, reject or retry work."""
import contextvars
import json
import os
import time
import uuid
import gunicorn
from gunicorn.workers.gthread import ThreadWorker

current = contextvars.ContextVar('staging_queue_request', default=None)
HEALTH_PATH = '/static/surveys/public_v2.css'


class StagingQueueWorker(ThreadWorker):
    def init_process(self):
        if os.environ.get('APP_ENV') != 'staging' or os.environ.get('STAGING_PERF_ENABLED') != '1':
            raise RuntimeError('Queue observer requires explicitly enabled staging.')
        if gunicorn.__version__ != "23.0.0":
            raise RuntimeError("Queue observer is verified only with Gunicorn 23.0.0.")
        super().init_process()

    def enqueue_req(self, conn):
        # Same initialization/order as Gunicorn 23.0.0, with submit timestamp.
        conn.init()
        submitted = time.monotonic()
        fs = self.tpool.submit(self.handle_observed, conn, submitted)
        self._wrap_future(fs, conn)

    def handle_observed(self, conn, submitted):
        started = time.monotonic()
        state = {'event': 'staging_queue', 'request_id': uuid.uuid4().hex,
                 'worker': os.getpid(), 'build': os.environ.get('RENDER_GIT_COMMIT', 'unknown'),
                 'start_epoch_ms': round(time.time() * 1000, 3),
                 'queue_ms': round((started - submitted) * 1000, 3)}
        token = current.set(state)
        try:
            return super().handle(conn)
        finally:
            if 'route' in state:
                state['worker_handle_ms'] = round((time.monotonic() - started) * 1000, 3)
                print(json.dumps(state), flush=True)
            current.reset(token)
