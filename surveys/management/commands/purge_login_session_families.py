from django.core.management.base import BaseCommand
from django.contrib.sessions.models import Session
from django.utils import timezone

from config.db_limits import bounded_atomic
from surveys.models import LoginSessionFamily, LoginSessionCredential


class Command(BaseCommand):
    help = 'Delete a bounded batch of expired login families and their obsolete sessions.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=100)

    def handle(self, *args, **options):
        with bounded_atomic():
            ids = list(LoginSessionFamily.objects.select_for_update(nowait=True).filter(
                expires_at__lte=timezone.now()).order_by('expires_at').values_list('id', flat=True)[:max(1, min(options['limit'], 200))])
            # Never leave an old legacy session row after deleting revocation
            # metadata, even if its expiry was modified outside the backend.
            keys = LoginSessionCredential.objects.filter(family_id__in=ids).values_list('session_key', flat=True)
            Session.objects.filter(session_key__in=keys).delete()
            LoginSessionFamily.objects.filter(id__in=ids).delete()
        self.stdout.write(f'Expired login families removed: {len(ids)}')
