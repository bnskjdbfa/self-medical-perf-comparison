from django.core.management.base import BaseCommand, CommandError

from surveys.models import SurveySubmission


class Command(BaseCommand):
    help = (
        'SurveySubmission의 answers_json·문항 스냅샷과 기존 SurveyAnswer 행의 '
        '정합성을 읽기 전용으로 검사합니다.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--team-id', type=int, help='특정 팀만 검사합니다.')
        parser.add_argument(
            '--fail-on-issues',
            action='store_true',
            help='JSON 또는 스냅샷에 복구가 필요한 제출이 있으면 실패 코드로 종료합니다.',
        )

    def handle(self, *args, **options):
        submissions = SurveySubmission.objects.order_by('id')
        if options['team_id']:
            submissions = submissions.filter(team_id=options['team_id'])

        counters = {
            'total': 0,
            'with_rows': 0,
            'without_rows': 0,
            'missing_answers_json': 0,
            'missing_snapshot_questions': 0,
            'json_keys_missing_from_snapshot': 0,
            'legacy_row_keys_missing_from_json': 0,
        }

        for submission in submissions.iterator(chunk_size=200):
            counters['total'] += 1
            answers_json = submission.answers_json if isinstance(submission.answers_json, dict) else {}
            snapshot = (
                submission.form_snapshot_json
                if isinstance(submission.form_snapshot_json, dict)
                else {}
            )
            snapshot_keys = {
                str(question.get('key')).strip()
                for section in snapshot.get('sections', [])
                if isinstance(section, dict)
                for question in section.get('questions', [])
                if isinstance(question, dict) and str(question.get('key') or '').strip()
            }
            row_keys = set(
                submission.answers.values_list('question__key', flat=True)
            )

            if row_keys:
                counters['with_rows'] += 1
            else:
                counters['without_rows'] += 1
            if not answers_json:
                counters['missing_answers_json'] += 1
            if not snapshot_keys:
                counters['missing_snapshot_questions'] += 1
            counters['json_keys_missing_from_snapshot'] += len(set(answers_json) - snapshot_keys)
            counters['legacy_row_keys_missing_from_json'] += len(row_keys - set(answers_json))

        labels = (
            ('total', '전체 제출'),
            ('with_rows', '기존 SurveyAnswer 행 있음'),
            ('without_rows', 'SurveyAnswer 행 없음'),
            ('missing_answers_json', 'answers_json 없음'),
            ('missing_snapshot_questions', '문항 스냅샷 없음'),
            ('json_keys_missing_from_snapshot', '스냅샷에 없는 JSON 문항 키'),
            ('legacy_row_keys_missing_from_json', 'JSON에 없는 기존 행 문항 키'),
        )
        for key, label in labels:
            self.stdout.write(f'{label}: {counters[key]}')

        issue_count = (
            counters['missing_answers_json']
            + counters['missing_snapshot_questions']
            + counters['json_keys_missing_from_snapshot']
            + counters['legacy_row_keys_missing_from_json']
        )
        if options['fail_on_issues'] and issue_count:
            raise CommandError(f'복구 검토가 필요한 항목이 {issue_count}건 있습니다.')

        self.stdout.write(self.style.SUCCESS('읽기 전용 정합성 검사가 완료되었습니다.'))
