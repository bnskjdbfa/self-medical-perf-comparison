from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from surveys.models import SurveySubmission
from teams.models import Respondent, Team


class SurveyAnswerMigrationAuditCommandTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(
            name='감사 테스트 팀',
            organization_name='감사 기관',
            organization_name_en='Audit Organization',
            code='AUDIT1',
            start_date=timezone.localdate(),
            max_day_no=1,
            is_active=True,
        )
        self.respondent = Respondent.objects.create(
            team=self.team,
            name='감사대상',
            birth_date='1990-01-01',
            phone='01012345678',
            normalized_name='감사대상',
            normalized_phone='01012345678',
        )

    def test_snapshot_only_submission_is_ready(self):
        SurveySubmission.objects.create(
            team=self.team,
            respondent=self.respondent,
            day_no=1,
            answers_json={'q1': '예'},
            result_json={},
            form_version=1,
            form_snapshot_json={
                'sections': [
                    {
                        'title': '신체',
                        'questions': [{'key': 'q1', 'label': '질문', 'question_type': 'single'}],
                    },
                ],
            },
            is_locked=True,
            submitted_at=timezone.now(),
        )
        stdout = StringIO()

        call_command('audit_survey_answer_migration', '--fail-on-issues', stdout=stdout)

        self.assertIn('SurveyAnswer 행 없음: 1', stdout.getvalue())
        self.assertIn('읽기 전용 정합성 검사가 완료되었습니다.', stdout.getvalue())

    def test_missing_snapshot_fails_strict_audit(self):
        SurveySubmission.objects.create(
            team=self.team,
            respondent=self.respondent,
            day_no=1,
            answers_json={'q1': '예'},
            result_json={},
            form_version=1,
            form_snapshot_json={},
            is_locked=True,
            submitted_at=timezone.now(),
        )

        with self.assertRaises(CommandError):
            call_command(
                'audit_survey_answer_migration',
                '--fail-on-issues',
                stdout=StringIO(),
                stderr=StringIO(),
            )
