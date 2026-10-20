"""Uses the existing guarded diagnostic app; no new URL or auth bypass."""
from staging_identity_diagnostic_wsgi import application as app
from staging_perf_metrics import route_name
from staging_queue_worker import current, HEALTH_PATH


def application(environ, start_response):
    state = current.get()
    path = environ.get('PATH_INFO', '')
    route = 'health_static' if path == HEALTH_PATH else route_name(path)
    if state is None or route is None:
        return app(environ, start_response)
    state['route'] = route
    state['method'] = environ.get('REQUEST_METHOD') if environ.get('REQUEST_METHOD') in {'GET', 'POST', 'HEAD'} else 'other'

    def respond(status, headers, exc_info=None):
        state['status'] = int(status.split()[0])
        # All allowlisted requests get a secret-free join ID. No SQL/auth detail.
        return start_response(status, list(headers) + [
            ('X-Queue-Request-ID', state['request_id']),
            ('Server-Timing', 'worker_queue;dur=' + str(state['queue_ms']))], exc_info)

    return app(environ, respond)
