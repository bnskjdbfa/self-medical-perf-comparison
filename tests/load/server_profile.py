"""Named local test server configurations; never changes deployed settings."""
ACCESS_FORMAT = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'


def profile(name):
    if name == 'production-gthread':
        return {'worker_class': 'gthread', 'workers': 2, 'threads': 2, 'timeout': 60,
                'keep_alive': 5, 'max_requests': 2000, 'max_requests_jitter': 500}
    if name == 'local-gthread-2x4':
        return {**profile('production-gthread'), 'threads': 4}
    if name == 'local-gthread-2x4-no-keepalive':
        return {**profile('local-gthread-2x4'), 'keep_alive': 0}
    if name in {'local-gthread-2x6-no-keepalive', 'local-gthread-2x8-no-keepalive'}:
        return {**profile('local-gthread-2x4-no-keepalive'), 'threads': 6 if '2x6' in name else 8}
    if name == 'sync3':
        return {'worker_class': 'sync', 'workers': 3, 'threads': 1, 'timeout': 30,
                'keep_alive': 2, 'max_requests': 0, 'max_requests_jitter': 0}
    raise ValueError('Unknown isolated test server profile')


def arguments(name):
    value = profile(name)
    return [part for key, item in value.items() for part in ('--' + key.replace('_', '-'), str(item))] + [
        '--access-logfile', '-', '--error-logfile', '-', '--access-logformat', ACCESS_FORMAT]
