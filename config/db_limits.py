"""Short, transaction-local budgets. Never hold these transactions around views."""
from contextlib import contextmanager
import time
from django.conf import settings
from django.db import connection, transaction, DatabaseError


class DatabaseBudgetExceeded(DatabaseError):
    sqlstate = '57014'


@contextmanager
def bounded_atomic():
    started = time.monotonic()
    nested = connection.in_atomic_block
    with transaction.atomic():
        previous = None
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                # SET LOCAL expires at the outer commit. Only a nested caller
                # needs to read/restore the enclosing transaction's settings.
                if nested:
                    cursor.execute("SELECT current_setting('lock_timeout'), current_setting('statement_timeout')")
                    previous = cursor.fetchone()
                cursor.execute("SELECT set_config('lock_timeout', %s, true), set_config('statement_timeout', %s, true)",
                               [f'{settings.SESSION_LOCK_TIMEOUT_MS}ms', f'{settings.SESSION_STATEMENT_TIMEOUT_MS}ms'])
        def budget(execute, sql, params, many, context):
            if (time.monotonic() - started) * 1000 > settings.SESSION_STATEMENT_TIMEOUT_MS:
                raise DatabaseBudgetExceeded('database_transaction_budget_exceeded')
            result = execute(sql, params, many, context)
            if (time.monotonic() - started) * 1000 > settings.SESSION_STATEMENT_TIMEOUT_MS:
                raise DatabaseBudgetExceeded('database_transaction_budget_exceeded')
            return result
        with connection.execute_wrapper(budget):
            yield
            if previous is not None:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('lock_timeout', %s, true), set_config('statement_timeout', %s, true)", previous)


def policy_row(model, **filters):
    """Compatible policy readers; block policy edits, not other participants."""
    row = model.objects.filter(**filters).first()
    if row is not None and connection.vendor == 'postgresql':
        table = connection.ops.quote_name(model._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f'SELECT id FROM {table} WHERE id = %s FOR SHARE NOWAIT', [row.pk])
        row = model.objects.filter(**filters).first()
    return row


def retryable_database_error(exc):
    while exc is not None:
        if getattr(exc, 'sqlstate', None) in {'55P03', '57014', '40P01', '40001'}:
            return True
        exc = exc.__cause__
    return False
