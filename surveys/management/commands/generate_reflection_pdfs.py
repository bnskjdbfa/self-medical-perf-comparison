from django.core.management.base import BaseCommand

from surveys.models import ReflectionSubmission
from surveys.reflection_exports import ReflectionExportError, generate_reflection_pdf


class Command(BaseCommand):
    help = '생성 대기 또는 실패 상태인 소감문 PDF를 Private Storage에 생성합니다.'

    def add_arguments(self, parser):
        parser.add_argument('--team-id', type=int)
        parser.add_argument('--limit', type=int, default=100)
        parser.add_argument('--retry-failed', action='store_true')

    def handle(self, *args, **options):
        statuses = [ReflectionSubmission.PdfStatus.PENDING]
        if options['retry_failed']:
            statuses.append(ReflectionSubmission.PdfStatus.FAILED)
        queryset = ReflectionSubmission.objects.filter(
            pdf_status__in=statuses,
            purged_at__isnull=True,
        ).order_by('submitted_at', 'pk')
        if options['team_id']:
            queryset = queryset.filter(team_id=options['team_id'])
        submission_ids = list(queryset.values_list('pk', flat=True)[:options['limit']])

        generated = 0
        failed = 0
        for submission_id in submission_ids:
            try:
                generate_reflection_pdf(submission_id, force=True)
                generated += 1
            except ReflectionExportError as exc:
                failed += 1
                self.stderr.write(f'{submission_id}: {exc}')

        self.stdout.write(
            self.style.SUCCESS(
                f'소감문 PDF 처리 완료: 생성 {generated}건, 실패 {failed}건'
            )
        )
