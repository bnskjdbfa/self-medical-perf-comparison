"""Fail-closed database target classification for Django settings.

This module deliberately has no Django settings dependency so it can be tested
and used while settings are being constructed.
"""

from __future__ import annotations

from dataclasses import dataclass
import warnings

from django.core.exceptions import ImproperlyConfigured


ALLOWED_APP_ENVS = frozenset({'local', 'development', 'staging', 'production', 'test'})
TRUE_VALUES = frozenset({'true', '1', 'yes', 'on'})
FALSE_VALUES = frozenset({'false', '0', 'no', 'off'})

SQLITE_TEST = 'sqlite_test'
NON_PRODUCTION = 'non_production'
PRODUCTION = 'production'
UNKNOWN = 'unknown'


@dataclass(frozen=True)
class DatabaseSafetyResult:
    app_env: str
    classification: str
    override_enabled: bool
    status: str
    masked_host: str
    masked_name: str
    reason: str


def parse_app_env(raw_value: str | None) -> str:
    value = (raw_value or 'local').strip().lower()
    if value not in ALLOWED_APP_ENVS:
        allowed = ', '.join(sorted(ALLOWED_APP_ENVS))
        raise ImproperlyConfigured(f'APP_ENV must be one of: {allowed}.')
    return value


def parse_strict_bool(name: str, raw_value: str | None, *, default=None):
    if raw_value is None or not raw_value.strip():
        return default
    value = raw_value.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ImproperlyConfigured(
        f'{name} must be one of true/false, 1/0, yes/no, or on/off.'
    )


def parse_markers(raw_value: str | None) -> tuple[str, ...]:
    if not raw_value:
        return ()
    return tuple(
        marker.strip().lower()
        for marker in raw_value.split(',')
        if marker.strip()
    )


def _matches_marker(value: str | None, markers: tuple[str, ...]) -> bool:
    normalized = (value or '').strip().lower()
    return bool(normalized) and any(marker in normalized for marker in markers)


def classify_database(
    *,
    engine: str,
    host: str | None,
    name: str | None,
    explicit_is_production: bool | None,
    production_host_markers: tuple[str, ...] = (),
    production_name_markers: tuple[str, ...] = (),
    non_production_host_markers: tuple[str, ...] = (),
    non_production_name_markers: tuple[str, ...] = (),
) -> str:
    if engine == 'django.db.backends.sqlite3':
        return SQLITE_TEST
    if explicit_is_production is not None:
        return PRODUCTION if explicit_is_production else NON_PRODUCTION
    if (
        _matches_marker(host, production_host_markers)
        or _matches_marker(name, production_name_markers)
    ):
        return PRODUCTION
    if (
        _matches_marker(host, non_production_host_markers)
        or _matches_marker(name, non_production_name_markers)
    ):
        return NON_PRODUCTION
    return UNKNOWN


def mask_host(value: str | None) -> str:
    value = (value or '').strip()
    if not value:
        return '(empty)'
    first, separator, remainder = value.partition('.')
    visible = first[:4] if len(first) >= 4 else first[:1]
    masked_first = f'{visible}****'
    return f'{masked_first}{separator}{remainder}' if separator else masked_first


def mask_name(value: str | None) -> str:
    value = str(value or '').strip()
    if not value:
        return '(empty)'
    visible = value[:4] if len(value) >= 4 else value[:1]
    return f'{visible}****'


def is_compatible(app_env: str, classification: str) -> bool:
    if classification == UNKNOWN:
        return False
    if app_env == PRODUCTION:
        return classification == PRODUCTION
    if classification == PRODUCTION:
        return False
    return classification in {NON_PRODUCTION, SQLITE_TEST}


def validate_database_target(
    *,
    app_env: str,
    classification: str,
    override_enabled: bool,
    engine: str,
    host: str | None,
    name: str | None,
) -> DatabaseSafetyResult:
    masked_host = mask_host(host)
    masked_name = mask_name(name)
    compatible = is_compatible(app_env, classification)
    reason = (
        f'APP_ENV={app_env}, DB_CLASSIFICATION={classification}, '
        f'DB_ENGINE={engine}, DB_HOST={masked_host}, DB_NAME={masked_name}'
    )
    if compatible:
        return DatabaseSafetyResult(
            app_env, classification, override_enabled, 'SAFE', masked_host, masked_name, reason
        )
    if override_enabled:
        warnings.warn(
            f'Database environment mismatch override is active: {reason}',
            RuntimeWarning,
            stacklevel=2,
        )
        return DatabaseSafetyResult(
            app_env, classification, override_enabled, 'OVERRIDE', masked_host, masked_name, reason
        )
    raise ImproperlyConfigured(
        'Unsafe database target blocked. '
        f'{reason}. Set the correct APP_ENV/DB target classification. '
        'ALLOW_DATABASE_ENV_MISMATCH is for audited emergencies only.'
    )
