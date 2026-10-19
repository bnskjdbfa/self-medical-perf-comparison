"""Optional diagnostics only: never stringify exceptions, SQL, params or requests."""
import json
import os
import re
import sys
import time


def describe_error(exc):
    chain = []
    seen = set()
    while exc is not None and id(exc) not in seen and len(chain) < 6:
        seen.add(id(exc))
        cls = type(exc)
        known = cls.__module__ in {'config.db_limits', 'django.db.utils', 'psycopg.errors', 'psycopg'}
        state = getattr(exc, 'sqlstate', None)
        state = state if isinstance(state, str) and re.fullmatch(r'[A-Z0-9]{5}', state) else None
        item = {'exception': cls.__module__ + '.' + cls.__name__ if known else 'other',
                'sqlstate': state, 'source': 'application' if cls.__module__ == 'config.db_limits'
                and cls.__name__ == 'DatabaseBudgetExceeded' else 'database_or_other'}
        # PostgreSQL primary messages are matched to constants, never emitted.
        primary = getattr(getattr(exc, 'diag', None), 'message_primary', None)
        item['server_reason'] = {
            'canceling statement due to statement timeout': 'statement_timeout',
            'canceling statement due to lock timeout': 'lock_timeout',
            'canceling statement due to user request': 'external_cancel',
        }.get(primary, 'not_classified') if isinstance(primary, str) else 'not_available'
        points = []
        tb = exc.__traceback__
        while tb is not None:
            frame = tb.tb_frame
            module = frame.f_globals.get('__name__', '')
            function = frame.f_code.co_name
            if module == 'config.db_limits' and function == 'budget':
                started = frame.f_locals.get('started')
                points.append({'module': module, 'function': function, 'line': tb.tb_lineno,
                    'phase': 'after_execute' if 'result' in frame.f_locals else 'before_execute',
                    # Observed after rollback/unwinding: NOT the precise throw time.
                    'elapsed_ms_at_observation': round((time.monotonic() - started) * 1000, 3)
                    if isinstance(started, (float, int)) else None})
            elif module in {'surveys.required_documents', 'surveys.views', 'teams.services'}:
                points.append({'module': module, 'function': function, 'line': tb.tb_lineno})
            tb = tb.tb_next
        item['points'] = points[-8:]
        chain.append(item)
        exc = exc.__cause__
    return chain


def install():
    from django.conf import settings
    from config import session_middleware
    from surveys import views
    from staging_perf_metrics import active
    if settings.APP_ENV != 'staging' or settings.DB_IS_PRODUCTION is not False:
        raise RuntimeError('Identity diagnostics require non-production staging.')
    original = views.login_event
    if getattr(original, '_identity_diagnostics', False):
        return

    def diagnostic_event(request, reason):
        original(request, reason)
        measurement = active.get()
        exc = sys.exc_info()[1]
        if measurement is None or reason != 'identity_database_busy' or exc is None:
            return
        # Reporting must never affect exception handling or authentication.
        try:
            print(json.dumps({'event': 'staging_identity_database_error',
                'worker': os.getpid(),
                'build': os.environ.get('RENDER_GIT_COMMIT', '')
                if re.fullmatch(r'[a-fA-F0-9]{40,64}', os.environ.get('RENDER_GIT_COMMIT', '')) else 'unknown',
                'login_request_id': request.login_request_id,
                'reason': 'identity_database_busy',
                'lock_limit_ms': settings.SESSION_LOCK_TIMEOUT_MS,
                'statement_and_wall_budget_ms': settings.SESSION_STATEMENT_TIMEOUT_MS,
                'sql_count_at_observation': measurement.values['sql_count'],
                'query_ms_at_observation': round(measurement.values['query_ms'], 3),
                'exception_chain': describe_error(exc)}), flush=True)
        except Exception:
            print('{"event":"staging_identity_diagnostic_failed"}', flush=True)

    diagnostic_event._identity_diagnostics = True
    views.login_event = diagnostic_event
    # Preserve the same event implementation for subsequent imports/callers.
    session_middleware.login_event = diagnostic_event
