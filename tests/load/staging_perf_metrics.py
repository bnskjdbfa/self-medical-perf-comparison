"""Secret-free, stage-only measurement helpers. No SQL or bind values retained."""
from collections import Counter
from contextlib import contextmanager
import contextvars
import re
import time

active = contextvars.ContextVar('staging_perf', default=None)


def sql_kind(sql):
    operation = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else 'OTHER'
    if operation in {'SAVEPOINT', 'RELEASE', 'ROLLBACK'}:
        return operation.lower()
    if 'set_config' in sql.lower():
        return 'transaction_settings'
    # Only an explicit allowlist can enter logs. Never return a fragment of SQL.
    for table in ('login_session_credentials', 'login_session_families', 'django_session'):
        if f'"{table}"' in sql:
            return operation.lower() + '_' + table
    return operation.lower() if operation in {'SELECT', 'INSERT', 'UPDATE', 'DELETE'} else 'other'


class Measurement:
    def __init__(self):
        self.values = Counter()
        self.kinds = Counter()
        self.sequence = []
        self.session_depth = 0

    def execute(self, run, sql, params, many, context):
        started = time.perf_counter()
        try:
            return run(sql, params, many, context)
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            kind = sql_kind(sql)
            self.values['query_ms'] += elapsed
            self.values['sql_count'] += 1
            self.kinds[kind] += 1
            if len(self.sequence) < 80:
                self.sequence.append({'kind': kind, 'ms': round(elapsed, 3)})

    @contextmanager
    def session_span(self):
        started = time.perf_counter()
        self.session_depth += 1
        try:
            yield
        finally:
            self.session_depth -= 1
            if self.session_depth == 0:
                self.values['session_ms'] += (time.perf_counter() - started) * 1000


def instrument_method(cls, method, metric, count=None, session=False):
    original = getattr(cls, method)
    def wrapped(*args, **kwargs):
        measurement = active.get()
        if measurement is None:
            return original(*args, **kwargs)
        started = time.perf_counter()
        if count:
            measurement.values[count] += 1
        try:
            if session:
                with measurement.session_span():
                    return original(*args, **kwargs)
            return original(*args, **kwargs)
        finally:
            if not session:
                measurement.values[metric] += (time.perf_counter() - started) * 1000
    setattr(cls, method, wrapped)


def install():
    from django.db.backends.postgresql.base import DatabaseWrapper
    from config import session_backend, session_middleware
    instrument_method(DatabaseWrapper, 'connect', 'db_open_ms', 'db_opens')
    instrument_method(DatabaseWrapper, 'get_new_connection', 'driver_connect_ms')
    # PostgreSQL health checks use a native cursor, outside execute_wrapper.
    instrument_method(DatabaseWrapper, 'is_usable', 'healthcheck_ms', 'health_checks')
    instrument_method(DatabaseWrapper, 'commit', 'commit_ms')
    instrument_method(DatabaseWrapper, 'rollback', 'rollback_ms')
    instrument_method(session_backend.SessionStore, 'load', 'session_ms', session=True)
    instrument_method(session_backend.SessionStore, 'save', 'session_ms', 'session_saves', session=True)
    original_atomic = session_middleware.bounded_atomic
    @contextmanager
    def measured_atomic(*args, **kwargs):
        measurement = active.get()
        if measurement is None:
            with original_atomic(*args, **kwargs):
                yield
        else:
            with measurement.session_span():
                with original_atomic(*args, **kwargs):
                    yield
    session_middleware.bounded_atomic = measured_atomic


def route_name(path):
    prefix = '/surveys/preview/v2/'
    names = {'code/': 'code', 'identity/': 'identity', 'privacy-consent/': 'privacy',
             'documents/next/': 'documents_next', 'home/': 'home', 'health/start/': 'survey_start'}
    if path.startswith(prefix):
        rest = path[len(prefix):]
        if rest in names:
            return names[rest]
        if rest in {'documents/third_party_consent/', 'documents/safety_pledge/'}:
            return 'documents'
        if rest.startswith('completed/'):
            return 'completed'
    if re.fullmatch(r'/surveys/day/\d+/', path):
        return 'survey'
    if re.fullmatch(r'/surveys/day/\d+/result/(detail/)?', path):
        return 'result'
    return None
