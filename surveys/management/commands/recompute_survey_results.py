from django.core.management.base import BaseCommand
from django.db import transaction

from surveys.models import SurveySubmission
from surveys.services import RESULT_SCORING_RULE, invalidate_submission_cache, rebuild_result_json


class Command(BaseCommand):
    help = (
        '저장된 자가검진 제출의 result_json(점수·위험도·요약)을 현재 규칙(섹션 평균 기준)으로 다시 계산합니다. '
        '기본은 이전 규칙으로 저장된 제출만 대상이며, --all 로 전체를 다시 계산할 수 있습니다.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--team-id', type=int, help='특정 팀만 처리')
        parser.add_argument('--limit', type=int, default=0, help='최대 처리 건수 (0 = 전체)')
        parser.add_argument('--all', action='store_true', help='이미 현재 규칙인 제출도 다시 계산')
        parser.add_argument('--dry-run', action='store_true', help='저장하지 않고 대상과 결과만 표시')

    def handle(self, *args, **options):
        queryset = (
            SurveySubmission.objects.select_related('team', 'respondent')
            .order_by('pk')
        )
        if options['team_id']:
            queryset = queryset.filter(team_id=options['team_id'])
        if not options['all']:
            queryset = queryset.exclude(result_json__scoring_rule=RESULT_SCORING_RULE)
        submission_ids = list(queryset.values_list('pk', flat=True))
        if options['limit']:
            submission_ids = submission_ids[: options['limit']]

        updated = skipped = unchanged = 0
        for submission_id in submission_ids:
            submission = SurveySubmission.objects.select_related('team', 'respondent').get(pk=submission_id)
            new_result = rebuild_result_json(submission=submission)
            if new_result is None:
                skipped += 1
                self.stderr.write(f'{submission.pk}: 스냅샷과 활성 폼이 없어 건너뜀')
                continue
            old = submission.result_json or {}
            changed = any(old.get(key) != new_result.get(key) for key in ('score', 'risk_level', 'summary', 'scoring_rule', 'sections'))
            if not changed:
                unchanged += 1
                continue
            self.stdout.write(
                f"{submission.pk}: {submission.team.name} / {submission.respondent.name} / {submission.day_no}일차  "
                f"{old.get('summary', '-')} → {new_result['summary']} ({new_result['risk_level']})"
            )
            if options['dry_run']:
                updated += 1
                continue
            with transaction.atomic():
                submission.result_json = new_result
                submission.save(update_fields=['result_json', 'updated_at'])
            invalidate_submission_cache(team=submission.team, respondent=submission.respondent, day_no=submission.day_no)
            updated += 1

        mode = '(dry-run, 저장 안 함) ' if options['dry_run'] else ''
        self.stdout.write(
            self.style.SUCCESS(
                f'{mode}대상 {len(submission_ids)}건: 갱신 {updated}건, 변경 없음 {unchanged}건, 건너뜀 {skipped}건'
            )
        )
