"""Local regression server only. Never use this WSGI entrypoint in deployment."""
import os
import sys
import time
import threading

if os.environ.get('LOGIN_RACE_TEST_SERVER') != '1' or os.environ.get('APP_ENV') != 'test':
    raise RuntimeError('This entrypoint is only for isolated login regression tests.')

# Match manage.py test's non-manifest static storage and isolated cache settings.
sys.argv.append('test')
from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django_application = get_wsgi_application()

from django.conf import settings
from django.http import JsonResponse
from django.urls import path
from django.contrib.staticfiles.handlers import StaticFilesHandler
from config import urls


def update_session(request):
    if not request.session.get('survey_respondent_id'):
        return JsonResponse({'error': 'unauthenticated'}, status=403)
    time.sleep(0.05)
    request.session.increment('race_test_counter')
    request.session.save()
    return JsonResponse({'count': request.session['race_test_counter']})


def slow_session(request):
    if not request.session.get('survey_respondent_id'):
        return JsonResponse({'error': 'unauthenticated'}, status=403)
    # Sleep inside the view, while ConcurrentSessionMiddleware holds its lock.
    time.sleep(1.2)
    return JsonResponse({'ok': True})


def database_error(request):
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1 / 0')
    return JsonResponse({'unexpected': True})


def session_proof(request):
    expected = request.headers.get('X-Test-Expected-Respondent')
    actual = request.session.get('survey_respondent_id')
    return JsonResponse({'matches': bool(actual and expected == str(actual))})


def statement_timeout(request):
    from django.db import connection
    from django.test.utils import override_settings
    from config.db_limits import bounded_atomic
    with override_settings(SESSION_LOCK_TIMEOUT_MS=20, SESSION_STATEMENT_TIMEOUT_MS=50):
        with bounded_atomic():
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_sleep(0.2)')
    return JsonResponse({'unexpected': True})


def effective_limits(request):
    from django.db import connection
    from config.db_limits import bounded_atomic
    with bounded_atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('lock_timeout'), current_setting('statement_timeout')")
            lock_ms, statement_ms = cursor.fetchone()
    return JsonResponse({'lock_timeout': lock_ms, 'statement_timeout': statement_ms})


urls.urlpatterns.append(path('_test/session-update/', update_session))
urls.urlpatterns.append(path('_test/slow-session/', slow_session))
urls.urlpatterns.append(path('_test/database-error/', database_error))
urls.urlpatterns.append(path('_test/session-proof/', session_proof))
urls.urlpatterns.append(path('_test/statement-timeout/', statement_timeout))
urls.urlpatterns.append(path('_test/effective-limits/', effective_limits))
django_application = StaticFilesHandler(django_application)


def application(environ, start_response):
    captured = []

    def capture(status, headers, exc_info=None):
        captured.append((status, headers, exc_info))

    result = django_application(environ, capture)
    # Delay TRANSPORT, after session transaction commit. This intentionally
    # reproduces out-of-order browser Set-Cookie delivery across workers.
    delay = min(float(environ.get('HTTP_X_TEST_DELAY', '0')), 2)
    time.sleep(delay)
    status, headers, exc_info = captured[0]
    start_response(status, [*headers, ('X-Test-Worker', str(os.getpid())),
                            ('X-Test-Thread', str(threading.get_ident()))], exc_info)
    return result
