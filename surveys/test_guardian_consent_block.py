import json
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from surveys.models import ExitSurveySubmission, ReflectionSubmission, SurveyForm, SurveyQuestion, SurveySection, SurveySubmission
from surveys.required_documents import respondent_lacks_guardian_consent
from teams.guardian_consent import revoke_guardian_consent, verify_guardian_consent
from teams.models import GuardianConsentEvent, Participant, Respondent, Team


class GuardianConsentSubmissionBlockTests(TestCase):
    """동의 취소는 다음 로그인뿐 아니라 이미 로그인한 세션의 새 제출도 막아야 한다."""

    def setUp(self):
        today = timezone.localdate()
        self.team = Team.objects.create(
            name='동의 팀', organization_name='기관', organization_name_en='Org',
            code='GC-BLOCK', start_date=today, max_day_no=3,
        )
        self.birth_date = date(today.year - 13, 1, 15)
        self.admin_user = get_user_model().objects.create_superuser('root', 'root@example.com', 'pass')
        self.participant = Participant.objects.create(
            team=self.team, name='미성년', normalized_name='미성년', birth_date=self.birth_date,
            phone='01012340000', normalized_phone='01012340000', gender='M', is_active=True,
        )
        verify_guardian_consent(participant=self.participant, actor=self.admin_user, source=GuardianConsentEvent.Source.ADMIN_FORM)
        self.respondent = Respondent.objects.create(
            team=self.team, name='미성년', normalized_name='미성년', birth_date=self.birth_date,
            phone='01012340000', normalized_phone='01012340000', gender='M',
        )
        self.form, self.question = self._make_form(SurveyForm.FormType.DAILY_HEALTH, 'headache', section_type='physical')
        self.exit_form, self.exit_question = self._make_form(SurveyForm.FormType.EXIT_SURVEY, 'overall', section_type='survey', version=2)
        session = self.client.session
        session['survey_team_id'] = self.team.id
        session['survey_team_code'] = self.team.code
        session['survey_respondent_id'] = self.respondent.id
        session['survey_day_no'] = 1
        session.save()

    def _make_form(self, form_type, key, *, section_type, version=1):
        form = SurveyForm.objects.create(
            team=self.team, day_no=1, title=key, version=version, is_active=True, form_type=form_type,
        )
        section = SurveySection.objects.create(form=form, title='섹션', section_type=section_type, display_order=1)
        question = SurveyQuestion.objects.create(
            section=section, key=key, label='문항', question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=[{'label': '예', 'score': 5}, {'label': '아니오', 'score': 1}], is_required=True, display_order=1,
        )
        return form, question

    def _revoke(self):
        revoke_guardian_consent(participant=self.participant, actor=self.admin_user, reason='테스트 취소')

    def test_helper_reflects_current_consent_state(self):
        self.assertFalse(respondent_lacks_guardian_consent(team=self.team, respondent=self.respondent))
        self._revoke()
        self.assertTrue(respondent_lacks_guardian_consent(team=self.team, respondent=self.respondent))

    def _day_survey_payload(self, signature):
        return {
            'survey_form_id': str(self.form.id),
            'survey_form_version': str(self.form.version),
            'survey_form_signature': signature,
            'answers_payload': json.dumps({str(self.question.id): '예'}),
            'submit': '1',
        }

    def test_day_survey_submit_is_blocked_after_revocation(self):
        url = reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?source=v2'
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        signature = page.context['survey_form_signature']
        self._revoke()

        with self.assertLogs('surveys.views', level='WARNING') as logs:
            response = self.client.post(url, self._day_survey_payload(signature), HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertEqual(response.status_code, 400)
        self.assertIn('법정대리인 동의 확인이 취소되어', response.json()['error'])
        self.assertIn('guardian consent not verified', logs.output[0])
        self.assertFalse(SurveySubmission.objects.exists())

        plain = self.client.post(url, self._day_survey_payload(signature), follow=True)
        self.assertContains(plain, '법정대리인 동의 확인이 취소되어')
        self.assertFalse(SurveySubmission.objects.exists())

    def test_day_survey_submit_succeeds_while_consent_is_verified(self):
        url = reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?source=v2'
        signature = self.client.get(url).context['survey_form_signature']

        response = self.client.post(url, self._day_survey_payload(signature), HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(SurveySubmission.objects.count(), 1)

    def test_exit_survey_submit_is_blocked_after_revocation(self):
        url = reverse('surveys:exit-survey')
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        signature = page.context['survey_form_signature']
        self._revoke()

        response = self.client.post(
            url,
            {
                'survey_form_id': str(self.exit_form.id),
                'survey_form_version': str(self.exit_form.version),
                'survey_form_signature': signature,
                'answers_payload': json.dumps({str(self.exit_question.id): '예'}),
                'submit': '1',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('법정대리인 동의 확인이 취소되어', response.json()['error'])
        self.assertFalse(ExitSurveySubmission.objects.exists())

    def test_reflection_submit_is_blocked_after_revocation(self):
        url = reverse('surveys:preview-v2-reflection')
        self.assertEqual(self.client.get(url).status_code, 200)
        self._revoke()

        response = self.client.post(
            url,
            {'affiliation': '학교', 'country': '대한민국', 'title': '제목', 'content': '열 글자 이상의 소감문 내용입니다.'},
            follow=True,
        )

        self.assertRedirects(response, reverse('surveys:preview-v2-home'))
        self.assertContains(response, '법정대리인 동의 확인이 취소되어')
        self.assertFalse(ReflectionSubmission.objects.exists())
