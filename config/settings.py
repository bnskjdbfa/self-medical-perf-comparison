import os
import sys
from pathlib import Path

from config.cache_settings import build_caches
from config.db_safety import (
    classify_database,
    parse_app_env,
    parse_markers,
    parse_strict_bool,
    validate_database_target,
)

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return None

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

APP_ENV = parse_app_env(os.getenv('APP_ENV'))


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.lower() in {'1', 'true', 'yes', 'on'}


def env_list(name: str, default: str = '') -> list[str]:
    return [value.strip() for value in os.getenv(name, default).split(',') if value.strip()]


SECRET_KEY = os.getenv(
    'DJANGO_SECRET_KEY',
    'local-development-fallback-secret-key-change-before-production-2026-x9Kq7vLm2P',
)
DEBUG = env_bool('DJANGO_DEBUG', True)
ALLOWED_HOSTS = env_list('DJANGO_ALLOWED_HOSTS', 'self-medical.onrender.com,127.0.0.1,localhost')
CSRF_TRUSTED_ORIGINS = env_list('DJANGO_CSRF_TRUSTED_ORIGINS')
ENABLE_PERF_LOG = env_bool('ENABLE_PERF_LOG', False)
PERF_SLOW_THRESHOLD_MS = env_int('PERF_SLOW_THRESHOLD_MS', 300)
SURVEY_FORM_CACHE_TTL = env_int('SURVEY_FORM_CACHE_TTL', 60)
SURVEY_CONTEXT_CACHE_TTL = env_int('SURVEY_CONTEXT_CACHE_TTL', 60)
SURVEY_SUBMISSION_CACHE_TTL = env_int('SURVEY_SUBMISSION_CACHE_TTL', 30)
# Shared cache across gunicorn workers when DJANGO_CACHE_URL (or Render's REDIS_URL)
# points at redis://; otherwise a per-process in-memory cache. See config/cache_settings.py.
CACHES = build_caches(
    cache_url=os.getenv('DJANGO_CACHE_URL') or os.getenv('REDIS_URL'),
    app_env=APP_ENV,
    running_tests='test' in sys.argv,
)
REFLECTION_EXPORT_RETENTION_HOURS = env_int('REFLECTION_EXPORT_RETENTION_HOURS', 72)
ADMIN_2FA_REQUIRED = env_bool('ADMIN_2FA_REQUIRED', not DEBUG)
OTP_TOTP_ISSUER = os.getenv('ADMIN_2FA_ISSUER', '탐방 통합 플랫폼')
SESSION_COOKIE_SECURE = env_bool('DJANGO_SESSION_COOKIE_SECURE', not DEBUG)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
# Legacy compatibility mirror; generation-bound credentials are authoritative.
LOGIN_AUTH_COOKIE_NAME = 'authenticated_sessionid'
SESSION_ENGINE = 'django.contrib.sessions.backends.db'
SESSION_LOCK_TIMEOUT_MS = max(1, min(env_int('SESSION_LOCK_TIMEOUT_MS', 200), 1000))
SESSION_STATEMENT_TIMEOUT_MS = max(SESSION_LOCK_TIMEOUT_MS + 1, min(env_int('SESSION_STATEMENT_TIMEOUT_MS', 1500), 5000))
SESSION_MAX_GENERATIONS = 64
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'login_console': {'class': 'logging.StreamHandler'}},
    'loggers': {
        'login.session': {
            'handlers': ['login_console'], 'level': 'INFO', 'propagate': False,
        },
    },
}
CSRF_COOKIE_SECURE = env_bool('DJANGO_CSRF_COOKIE_SECURE', not DEBUG)
SECURE_SSL_REDIRECT = env_bool('DJANGO_SECURE_SSL_REDIRECT', not DEBUG)
SECURE_HSTS_SECONDS = env_int('DJANGO_SECURE_HSTS_SECONDS', 31536000 if not DEBUG else 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool('DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS', not DEBUG)
SECURE_HSTS_PRELOAD = env_bool('DJANGO_SECURE_HSTS_PRELOAD', not DEBUG)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "rest_framework",
    "teams",
    "surveys",
    "admins",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "config.session_middleware.ConcurrentSessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "admins.middleware.AdminTwoFactorMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

if ENABLE_PERF_LOG:
    MIDDLEWARE.append("config.perf.PerformanceLogMiddleware")

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DB_ENGINE = os.getenv('DB_ENGINE', 'django.db.backends.postgresql')
DB_IS_PRODUCTION = parse_strict_bool('DB_IS_PRODUCTION', os.getenv('DB_IS_PRODUCTION'), default=None)
ALLOW_DATABASE_ENV_MISMATCH = parse_strict_bool(
    'ALLOW_DATABASE_ENV_MISMATCH',
    os.getenv('ALLOW_DATABASE_ENV_MISMATCH'),
    default=False,
)

if DB_ENGINE == 'django.db.backends.sqlite3':
    sqlite_name = os.getenv('DB_NAME', 'db.sqlite3')
    DATABASES = {
        'default': {
            'ENGINE': DB_ENGINE,
            'NAME': sqlite_name if sqlite_name == ':memory:' else BASE_DIR / sqlite_name,
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': DB_ENGINE,
            'NAME': os.environ.get('DB_NAME', 'postgres'),
            'USER': os.environ.get('DB_USER', ''),
            'PASSWORD': os.environ.get('DB_PASSWORD', ''),
            'HOST': os.environ.get('DB_HOST', ''),
            'PORT': os.environ.get('DB_PORT', '5432'),
            'CONN_MAX_AGE': env_int('DB_CONN_MAX_AGE', 60),
            'CONN_HEALTH_CHECKS': env_bool('DB_CONN_HEALTH_CHECKS', True),
            'OPTIONS': {
                'sslmode': os.getenv('DB_SSLMODE', 'require'),
                'connect_timeout': env_int('DB_CONNECT_TIMEOUT', 5),
            },
        }
    }

DB_CLASSIFICATION = classify_database(
    engine=DATABASES['default']['ENGINE'],
    host=str(DATABASES['default'].get('HOST', '')),
    name=str(DATABASES['default'].get('NAME', '')),
    explicit_is_production=DB_IS_PRODUCTION,
    production_host_markers=parse_markers(os.getenv('PRODUCTION_DB_HOST_MARKERS')),
    production_name_markers=parse_markers(os.getenv('PRODUCTION_DB_NAME_MARKERS')),
    non_production_host_markers=parse_markers(os.getenv('NON_PRODUCTION_DB_HOST_MARKERS')),
    non_production_name_markers=parse_markers(os.getenv('NON_PRODUCTION_DB_NAME_MARKERS')),
)
DB_SAFETY = validate_database_target(
    app_env=APP_ENV,
    classification=DB_CLASSIFICATION,
    override_enabled=ALLOW_DATABASE_ENV_MISMATCH,
    engine=DATABASES['default']['ENGINE'],
    host=str(DATABASES['default'].get('HOST', '')),
    name=str(DATABASES['default'].get('NAME', '')),
)

LANGUAGE_CODE = os.getenv("DJANGO_LANGUAGE_CODE", "ko-kr")
TIME_ZONE = os.getenv("DJANGO_TIME_ZONE", "Asia/Seoul")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_URL = os.getenv('DJANGO_MEDIA_URL', '/media/')
MEDIA_ROOT = Path(os.getenv('DJANGO_MEDIA_ROOT', BASE_DIR / 'media'))
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'reflection_exports': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
        'OPTIONS': {
            'location': MEDIA_ROOT / 'private' / 'reflection-exports',
            'base_url': None,
        },
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}

SUPABASE_S3_ENDPOINT_URL = os.getenv('SUPABASE_S3_ENDPOINT_URL', '').strip()
SUPABASE_S3_ACCESS_KEY_ID = os.getenv('SUPABASE_S3_ACCESS_KEY_ID', '').strip()
SUPABASE_S3_SECRET_ACCESS_KEY = os.getenv('SUPABASE_S3_SECRET_ACCESS_KEY', '').strip()
SUPABASE_S3_BUCKET_NAME = os.getenv('SUPABASE_S3_BUCKET_NAME', '').strip()

if all(
    (
        SUPABASE_S3_ENDPOINT_URL,
        SUPABASE_S3_ACCESS_KEY_ID,
        SUPABASE_S3_SECRET_ACCESS_KEY,
        SUPABASE_S3_BUCKET_NAME,
    )
):
    STORAGES['reflection_exports'] = {
        'BACKEND': 'storages.backends.s3.S3Storage',
        'OPTIONS': {
            'access_key': SUPABASE_S3_ACCESS_KEY_ID,
            'secret_key': SUPABASE_S3_SECRET_ACCESS_KEY,
            'bucket_name': SUPABASE_S3_BUCKET_NAME,
            'endpoint_url': SUPABASE_S3_ENDPOINT_URL,
            'region_name': os.getenv('SUPABASE_S3_REGION_NAME', 'ap-northeast-2'),
            'default_acl': None,
            'querystring_auth': True,
            'file_overwrite': True,
            'location': os.getenv('SUPABASE_REFLECTION_EXPORT_PREFIX', 'reflection-exports').strip('/'),
        },
    }

if 'test' in sys.argv:
    STORAGES['staticfiles'] = {
        'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
    }
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
