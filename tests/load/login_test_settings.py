"""Isolated PostgreSQL test settings, not a deployment settings module."""
import os

if os.environ.get('LOGIN_RACE_TEST_SERVER') != '1' or os.environ.get('APP_ENV') != 'test':
    raise RuntimeError('Only the isolated login test harness may use these settings.')

from config.settings import *  # noqa: F403

# An initdb --no-locale server can have an SQL_ASCII template1. Django creates
# a NEW test database from template1, not from the already UTF-8 fixture DB.
# Explicit UTF8/template0 avoids false varchar length errors for Korean text.
if DATABASES['default']['ENGINE'] == 'django.db.backends.postgresql':  # noqa: F405
    DATABASES['default']['TEST'] = {'CHARSET': 'UTF8', 'TEMPLATE': 'template0'}  # noqa: F405
