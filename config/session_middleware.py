"""Generation credentials; no request-wide transaction or legacy fallback."""
import logging
import time
import uuid
from contextlib import nullcontext

from django.conf import settings
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.utils.cache import patch_cache_control, patch_vary_headers
from django.utils.http import http_date

from config.db_limits import bounded_atomic
from config.session_backend import SessionStore, SessionConflict, load_context, SCOPE_PREFIX, TOKEN_PREFIX

logger = logging.getLogger('login.session')
LOGIN_PATHS = {'/surveys/preview/v2/code/', '/surveys/preview/v2/identity/'}


def login_event(request, reason):
    if not hasattr(request, 'login_request_id'):
        request.login_request_id = uuid.uuid4().hex
    logger.info('login_session request_id=%s reason=%s', request.login_request_id, reason)


class ConcurrentSessionMiddleware(SessionMiddleware):
    sync_capable = True
    async_capable = False

    def __init__(self, get_response):
        super().__init__(get_response)
        if settings.SESSION_ENGINE != 'django.contrib.sessions.backends.db':
            raise ImproperlyConfigured('Generation sessions require DB storage.')

    def __call__(self, request):
        request.login_request_id = uuid.uuid4().hex
        try:
            with bounded_atomic() if connection.vendor == 'postgresql' else nullcontext():
                context, key = load_context(request.COOKIES)
            request.session = SessionStore(key, context=context)
            request.login_session_missing = key is None
            if key is None:
                login_event(request, 'retired_session' if context.stale else 'cookie_absent')
            if (key is None and context.stale and request.method == 'POST'
                    and request.path == '/surveys/preview/v2/identity/'):
                return self.failure(request, 409, 'retired_identity_request')
            # Python/external I/O in the view holds NO session lock/transaction.
            response = self.get_response(request)
            if request.session.failure_reason:
                login_event(request, request.session.failure_reason)
            if response.status_code >= 500 or getattr(request, 'session_commit_failed', False):
                return response
            session = request.session
            changed = session.has_changes()
            recovery_page = context.stale and request.method == 'GET' and request.path == '/surveys/preview/v2/code/'
            if not recovery_page and (changed or session.flushed or (context.legacy and not session.is_empty())):
                session.save(finalizing=True)
            if session.accessed:
                patch_vary_headers(response, ('Cookie',))
            age = session.get_expiry_age()
            if context.emit and context.credential:
                self.cookie(response, TOKEN_PREFIX + context.credential.id.hex,
                            session.session_key, age)
                from django.utils import timezone
                self.cookie(response, SCOPE_PREFIX + context.family.id.hex, context.secret,
                            max(1, int((context.family.expires_at - timezone.now()).total_seconds())))
            # Compatibility mirrors are NOT authoritative. A registered key
            # cannot authenticate via a mirror, even if its scope is removed.
            if session.session_key and (changed or context.emit):
                self.cookie(response, settings.SESSION_COOKIE_NAME, session.session_key, age)
            authenticated = bool(session.get('_auth_user_id') or (
                session.get('survey_respondent_id') and session.get('v2_login_expires_at')))
            if authenticated and (context.emit or not request.COOKIES.get(settings.LOGIN_AUTH_COOKIE_NAME)):
                self.cookie(response, settings.LOGIN_AUTH_COOKIE_NAME, session.session_key, age)
            for name in context.obsolete:
                response.delete_cookie(name, path=settings.SESSION_COOKIE_PATH,
                                       domain=settings.SESSION_COOKIE_DOMAIN,
                                       samesite=settings.SESSION_COOKIE_SAMESITE)
            if request.path in LOGIN_PATHS:
                if request.headers.get('X-Login-Request') == '1' and response.status_code == 302:
                    converted = JsonResponse({'redirect_url': response['Location']})
                    converted.cookies = response.cookies
                    response = converted
                patch_cache_control(response, no_store=True, private=True)
                response['X-Request-ID'] = request.login_request_id
            return response
        except SessionConflict as exc:
            return self.failure(request, 409, str(exc))
        except DatabaseError:
            return self.failure(request, 503, 'session_database_busy')

    def process_exception(self, request, exception):
        if isinstance(exception, (SessionConflict, DatabaseError)):
            request.session_commit_failed = True
            return self.failure(request, 409 if isinstance(exception, SessionConflict) else 503,
                                'session_write_conflict' if isinstance(exception, SessionConflict) else 'database_busy')

    @staticmethod
    def cookie(response, name, value, age):
        response.set_cookie(name, value, max_age=age, expires=http_date(time.time() + age),
                            path=settings.SESSION_COOKIE_PATH, domain=settings.SESSION_COOKIE_DOMAIN,
                            secure=settings.SESSION_COOKIE_SECURE or None,
                            httponly=settings.SESSION_COOKIE_HTTPONLY or None,
                            samesite=settings.SESSION_COOKIE_SAMESITE)

    @staticmethod
    def failure(request, status, reason):
        login_event(request, reason)
        response = JsonResponse({'error': 'login_retry_required' if status == 409 else 'temporarily_unavailable'}, status=status)
        response['Retry-After'] = '1'
        response['X-Request-ID'] = request.login_request_id
        patch_cache_control(response, no_store=True)
        return response
