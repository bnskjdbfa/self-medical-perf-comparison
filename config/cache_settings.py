"""Cache backend selection for ``config.settings``.

Without a shared backend every gunicorn worker keeps its own in-memory cache, so an
admin action that invalidates a key only reaches the worker that handled that request.
Point ``DJANGO_CACHE_URL`` (or Render's ``REDIS_URL``) at a ``redis://`` / ``rediss://``
server to make every worker share one cache. Tests always use the in-memory backend so a
developer's ``.env`` can never make the suite touch a real cache server.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

LOCMEM_BACKEND = 'django.core.cache.backends.locmem.LocMemCache'
REDIS_BACKEND = 'django.core.cache.backends.redis.RedisCache'
SUPPORTED_SCHEMES = ('redis', 'rediss')
DEFAULT_KEY_PREFIX = 'self-medical'


def build_caches(
    *,
    cache_url: str | None,
    app_env: str,
    running_tests: bool = False,
    key_prefix: str = DEFAULT_KEY_PREFIX,
) -> dict:
    """Return the Django ``CACHES`` setting for the given environment.

    - No URL (or tests): per-process ``LocMemCache``. Same behavior as before.
    - ``redis://`` / ``rediss://`` URL: shared ``RedisCache`` for all workers.
    - Any other scheme: refuse to start, like the database safety checks.
    """
    cache_url = (cache_url or '').strip()
    if running_tests or app_env == 'test' or not cache_url:
        return {
            'default': {
                'BACKEND': LOCMEM_BACKEND,
                'LOCATION': 'self-medical-per-process',
            }
        }

    scheme = urlsplit(cache_url).scheme.lower()
    if scheme not in SUPPORTED_SCHEMES:
        raise ImproperlyConfigured(
            'DJANGO_CACHE_URL must be a redis:// or rediss:// URL '
            f'(got scheme {scheme or "<none>"!r}).'
        )
    return {
        'default': {
            'BACKEND': REDIS_BACKEND,
            'LOCATION': cache_url,
            'KEY_PREFIX': key_prefix,
        }
    }


def describe_cache_backend(caches: dict) -> str:
    """Secret-free one-liner for operators (never includes the URL)."""
    backend = caches.get('default', {}).get('BACKEND', '')
    if backend == REDIS_BACKEND:
        return 'redis (shared across workers)'
    if backend == LOCMEM_BACKEND:
        return 'locmem (per-process, not shared across workers)'
    return backend or 'unknown'
