from django.conf import settings
from django.core.management.base import BaseCommand

from config.cache_settings import describe_cache_backend


class Command(BaseCommand):
    help = '현재 DB 대상을 연결 없이 마스킹하여 표시합니다.'

    def handle(self, *args, **options):
        safety = settings.DB_SAFETY
        explicit = settings.DB_IS_PRODUCTION
        explicit_label = 'unset' if explicit is None else str(explicit).lower()
        rows = (
            ('APP_ENV', settings.APP_ENV),
            ('DB_ENGINE', settings.DATABASES['default']['ENGINE']),
            ('DB_CLASSIFICATION', settings.DB_CLASSIFICATION),
            ('DB_IS_PRODUCTION', explicit_label),
            ('DB_HOST', safety.masked_host),
            ('DB_NAME', safety.masked_name),
            ('MISMATCH_OVERRIDE', str(settings.ALLOW_DATABASE_ENV_MISMATCH).lower()),
            ('STATUS', safety.status),
            ('CACHE_BACKEND', describe_cache_backend(settings.CACHES)),
        )
        for label, value in rows:
            self.stdout.write(f'{label:<22}: {value}')
