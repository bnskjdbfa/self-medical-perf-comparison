from datetime import date
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from surveys.models import SurveyForm, SurveyQuestion, SurveySection, SurveySubmission
from surveys.services import (
    RESULT_SCORING_RULE,
    build_result_from_answers,
    build_result_payload,
    build_survey_form_snapshot,
    result_summary_from_result_json,
    summarize_section_results,
)
from teams.models import Respondent, Team


FIVE_POINT = [
    {'label': '매우 좋음', 'score': 5},
    {'label': '좋음', 'score': 4},
    {'label': '보통', 'score': 3},
    {'label': '나쁨', 'score': 2},
    {'label': '매우 나쁨', 'score': 1},
]


def section(key, label, average, surveys=1):
    return {'key': key, 'status_label': label, 'average_score': average, 'surveys': [{'score': average}] * surveys}


class SummarizeSectionResultsTests(TestCase):
    def test_risk_follows_worst_section_and_summary_lists_each_section(self):
        cases = [
            ([section('body', '최상', 95), section('emotion', '정상', 70)], 'low', '신체 최상 · 정신 정상', 82),
            ([section('body', '주의', 45), section('emotion', '정상', 65)], 'medium', '신체 주의 · 정신 정상', 55),
            ([section('body', '정상', 60), section('emotion', '경고', 25)], 'high', '신체 정상 · 정신 경고', 42),
            ([section('body', '위험', 0), section('emotion', '위험', 10)], 'high', '신체 위험 · 정신 위험', 5),
        ]
        for sections, risk, summary, score in cases:
            with self.subTest(summary=summary):
                overview = summarize_section_results(sections)
                self.assertEqual((overview['risk_level'], overview['summary'], overview['score']), (risk, summary, score))

    def test_sections_without_scored_questions_are_ignored(self):
        overview = summarize_section_results([section('body', '주의', 50), section('emotion', '설문 전', 0, surveys=0)])
        self.assertEqual((overview['risk_level'], overview['summary'], overview['score']), ('medium', '신체 주의', 50))

        empty = summarize_section_results([section('body', '설문 전', 0, surveys=0)])
        self.assertEqual(empty['risk_level'], 'unknown')

    def test_english_status_labels_are_understood(self):
        overview = summarize_section_results([section('body', 'Warning', 25), section('emotion', 'Excellent', 90)])
        self.assertEqual(overview['risk_level'], 'high')


class BuildResultFromAnswersTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(
            name='채점 팀', organization_name='기관', organization_name_en='Org',
            code='SCORE', start_date=date.today(), max_day_no=3,
        )
        self.form = SurveyForm.objects.create(team=self.team, day_no=1, title='18문항', version=1, is_active=True)
        body = SurveySection.objects.create(form=self.form, title='신체', section_type='physical', display_order=1)
        mind = SurveySection.objects.create(form=self.form, title='정신', section_type='mental', display_order=2)
        for index in range(9):
            SurveyQuestion.objects.create(section=body, key=f'b{index}', label=f'신체 {index}', question_type='single_choice',
                                          options_json=FIVE_POINT, is_required=True, display_order=index + 1)
            SurveyQuestion.objects.create(section=mind, key=f'm{index}', label=f'정신 {index}', question_type='single_choice',
                                          options_json=FIVE_POINT, is_required=True, display_order=index + 1)

    def _answers(self, body_pick, mind_pick):
        return {f'b{i}': body_pick for i in range(9)} | {f'm{i}': mind_pick for i in range(9)}

    def test_all_healthy_answers_are_low_risk_even_with_many_questions(self):
        result = build_result_from_answers(survey_form=self.form, answers_json=self._answers('매우 좋음', '매우 좋음'))

        self.assertEqual(result['risk_level'], 'low')
        self.assertEqual(result['summary'], '신체 최상 · 정신 최상')
        self.assertEqual(result['score'], 100)
        self.assertEqual(result['scoring_rule'], RESULT_SCORING_RULE)
        self.assertEqual(result['sections']['body']['status_label'], '최상')

    def test_all_worst_answers_are_high_risk(self):
        result = build_result_from_answers(survey_form=self.form, answers_json=self._answers('매우 나쁨', '매우 나쁨'))

        self.assertEqual((result['risk_level'], result['summary'], result['score']), ('high', '신체 위험 · 정신 위험', 0))

    def test_mixed_answers_follow_worst_section(self):
        result = build_result_from_answers(survey_form=self.form, answers_json=self._answers('보통', '매우 좋음'))

        self.assertEqual(result['sections']['body']['average_score'], 50)
        self.assertEqual((result['risk_level'], result['summary']), ('medium', '신체 주의 · 정신 최상'))

    def test_payload_keeps_explicit_submitted_at(self):
        submitted_at = timezone.now()
        payload = build_result_payload(sections=[section('body', '정상', 60)], submitted_at=submitted_at)
        self.assertEqual(payload['submitted_at'], submitted_at.isoformat())


class ResultSummaryDisplayTests(TestCase):
    def test_admin_summary_uses_stored_sections_over_legacy_summary(self):
        legacy = {
            'status': 'submitted', 'score': 90, 'risk_level': 'high',
            'summary': '임시 규칙 기준으로 고위험 신호가 감지되었습니다.',
            'scoring_rule': 'temporary_v1_single_pick_or_multi_sum',
            'sections': {
                'body': {'average_score': 100, 'status_label': '최상', 'surveys': [{'question_key': 'b0', 'score': 100}]},
                'emotion': {'average_score': 65, 'status_label': '정상', 'surveys': [{'question_key': 'm0', 'score': 65}]},
            },
        }
        self.assertEqual(result_summary_from_result_json(legacy), '신체 최상 · 정신 정상')

    def test_admin_summary_falls_back_when_no_sections_are_stored(self):
        self.assertEqual(result_summary_from_result_json({'summary': '옛 요약'}), '옛 요약')
        self.assertEqual(result_summary_from_result_json({'status': 'submitted'}), 'submitted')
        self.assertEqual(result_summary_from_result_json({}), '-')
        self.assertEqual(result_summary_from_result_json(None), '-')


class RecomputeSurveyResultsCommandTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(
            name='재계산 팀', organization_name='기관', organization_name_en='Org',
            code='RECALC', start_date=date.today(), max_day_no=3,
        )
        self.form = SurveyForm.objects.create(team=self.team, day_no=1, title='폼', version=1, is_active=True)
        body = SurveySection.objects.create(form=self.form, title='신체', section_type='physical', display_order=1)
        mind = SurveySection.objects.create(form=self.form, title='정신', section_type='mental', display_order=2)
        SurveyQuestion.objects.create(section=body, key='b0', label='신체', question_type='single_choice',
                                      options_json=FIVE_POINT, is_required=True, display_order=1)
        SurveyQuestion.objects.create(section=mind, key='m0', label='정신', question_type='single_choice',
                                      options_json=FIVE_POINT, is_required=True, display_order=1)
        self.respondent = Respondent.objects.create(
            team=self.team, name='홍길동', birth_date=date(1990, 1, 1), phone='01012345678',
            normalized_name='홍길동', normalized_phone='01012345678',
        )
        self.submitted_at = timezone.now()
        self.legacy_result = {
            'status': 'submitted', 'score': 10, 'risk_level': 'high',
            'summary': '임시 규칙 기준으로 고위험 신호가 감지되었습니다.',
            'scoring_rule': 'temporary_v1_single_pick_or_multi_sum',
            'submitted_at': self.submitted_at.isoformat(),
            'sections': {
                'body': {'average_score': 100, 'status_label': '최상', 'surveys': [{'question_key': 'b0', 'score': 100}]},
                'emotion': {'average_score': 100, 'status_label': '최상', 'surveys': [{'question_key': 'm0', 'score': 100}]},
            },
        }
        self.submission = SurveySubmission.objects.create(
            team=self.team, respondent=self.respondent, day_no=1,
            answers_json={'b0': '매우 좋음', 'm0': '매우 좋음'},
            result_json=dict(self.legacy_result),
            form_version=1,
            form_snapshot_json=build_survey_form_snapshot(survey_form=self.form),
            is_locked=True,
            submitted_at=self.submitted_at,
        )

    def _run(self, *args):
        out, err = StringIO(), StringIO()
        call_command('recompute_survey_results', *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_dry_run_reports_without_saving(self):
        out, _ = self._run('--dry-run')

        self.assertIn('dry-run', out)
        self.assertIn('갱신 1건', out)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.result_json['risk_level'], 'high')

    def test_recompute_replaces_legacy_rule_and_keeps_submitted_at(self):
        out, _ = self._run()

        self.assertIn('갱신 1건', out)
        self.submission.refresh_from_db()
        result = self.submission.result_json
        self.assertEqual((result['risk_level'], result['summary'], result['score']), ('low', '신체 최상 · 정신 최상', 100))
        self.assertEqual(result['scoring_rule'], RESULT_SCORING_RULE)
        self.assertEqual(result['submitted_at'], self.submitted_at.isoformat())
        self.assertEqual(result['sections']['body']['average_score'], 100)

        second, _ = self._run()
        self.assertIn('대상 0건', second)

    def test_all_flag_and_team_filter(self):
        self._run()
        out, _ = self._run('--all', '--team-id', str(self.team.pk))
        self.assertIn('변경 없음 1건', out)
        other, _ = self._run('--all', '--team-id', str(self.team.pk + 999))
        self.assertIn('대상 0건', other)

    def test_submission_without_snapshot_uses_active_form_or_is_skipped(self):
        self.submission.form_snapshot_json = {}
        self.submission.save(update_fields=['form_snapshot_json', 'updated_at'])

        out, _ = self._run()
        self.assertIn('갱신 1건', out)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.result_json['summary'], '신체 최상 · 정신 최상')

        self.form.is_active = False
        self.form.save(update_fields=['is_active', 'updated_at'])
        self.submission.result_json = dict(self.legacy_result)
        self.submission.save(update_fields=['result_json', 'updated_at'])
        out, err = self._run()
        self.assertIn('건너뜀 1건', out)
        self.assertIn('스냅샷과 활성 폼이 없어', err)
