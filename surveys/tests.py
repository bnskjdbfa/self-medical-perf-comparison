import json
import hashlib
import importlib
import re
from io import StringIO
from datetime import date, datetime, timedelta
import warnings
from unittest.mock import patch
from urllib.parse import urlencode

from django.core.cache import cache
from django.conf import settings
from django.contrib.staticfiles import finders
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.apps import apps as django_apps
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import Client, RequestFactory, SimpleTestCase
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from surveys.admin import (
    CommonQuestionAdmin,
    CommonQuestionForm,
    CommonQuestionInline,
    CommonQuestionSectionInline,
    CommonQuestionSetAdmin,
    CommonQuestionSetAdminForm,
    CommonQuestionSetCloneForm,
    DocumentSubmissionAdmin,
    RequiredDocumentConsentBundleAdmin,
    SurveyFormAdmin,
    SurveyQuestionAdmin,
    SurveyQuestionForm,
    SurveySectionAdmin,
)
from admins.models import AdminTeamPermission
from surveys.services import (
    normalize_name,
    get_active_form_day_numbers,
    get_active_survey_form,
)
from surveys.survey_deployment import (
    build_cloned_question_set_name,
    clone_common_question_set,
    clone_common_question_set_to_days,
    deploy_common_question_set_to_team,
    deploy_common_question_set_to_teams,
)
from teams.models import Participant, Respondent, Team
from .models import (
    CommonQuestion,
    CommonQuestionSection,
    AnonymousSurveySubmission,
    CommonQuestionSet,
    CommonQuestionSetDeployment,
    DocumentSubmission,
    DocumentTemplate,
    ExitSurveySubmission,
    RequiredDocumentConsentBundle,
    ReflectionSubmission,
    TeamFeatureSetting,
    SurveyForm,
    SurveyQuestion,
    SurveySection,
    SurveyAnswer,
    SurveySubmission,
    TeamCommonQuestionExclusion,
)
from .views import (
    SESSION_DAY_NO,
    SESSION_LOGIN_EXPIRES_AT,
    SESSION_PRE_IDENTITY_PRIVACY_CONSENT,
    SESSION_PUBLIC_LANGUAGE,
    SESSION_RESPONDENT_ID,
    SESSION_REQUIRED_DOCUMENT_CONSENTS,
    SESSION_TEAM_CODE,
    SESSION_TEAM_ID,
)
from .required_documents import (
    RequiredDocumentAlreadySubmitted,
    RequiredDocumentEvidenceChanged,
    RequiredDocumentTemplateInvalid,
    STATIC_REQUIRED_DOCUMENT_VERSION,
    create_required_document_submission,
    create_required_document_submissions,
    create_required_document_submissions_from_consents,
    build_canonical_document_evidence,
    build_enabled_document_types_snapshot,
    build_participant_snapshot,
    build_team_snapshot,
    get_document_content_hash,
    get_active_document_template,
    get_enabled_required_document_types,
    get_next_pending_required_document,
    get_required_document_progress,
    render_canonical_document,
)
from config.cache_settings import LOCMEM_BACKEND, REDIS_BACKEND, build_caches
from config.db_safety import (
    NON_PRODUCTION,
    PRODUCTION,
    SQLITE_TEST,
    UNKNOWN,
    classify_database,
    mask_host,
    mask_name,
    parse_app_env,
    parse_markers,
    parse_strict_bool,
    validate_database_target,
)


class SurveyFlowTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(
            name='고양교육지원청',
            code='1234',
            manager_entry_code='goyang-admin',
            is_active=True,
            start_date=date.today(),
            max_day_no=3,
        )
        self.form = SurveyForm.objects.create(
            team=self.team,
            day_no=1,
            title='1일차',
            version=1,
            schema_json={},
            is_active=True,
        )
        self.section = SurveySection.objects.create(
            form=self.form,
            title='신체',
            section_type=SurveySection.SectionType.PHYSICAL,
            display_order=1,
        )
        self.question = SurveyQuestion.objects.create(
            section=self.section,
            key='PT_day1',
            label='두통이 있습니까?',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=['예', '아니오'],
            is_required=True,
            display_order=1,
        )
        Participant.objects.create(
            team=self.team,
            name='홍길동',
            gender='M',
            birth_date='1985-03-12',
            phone='01012345678',
            normalized_name='홍길동',
            normalized_phone='01012345678',
            is_active=True,
        )

    def _start_session(self, *, name='홍길동', birth_date='1985-03-12', phone='010-1234-5678', gender='M', client=None):
        """v2 본인확인을 마친 상태와 같은 세션을 만든다 (v1 로그인 화면은 폐기됨)."""
        client = client or self.client
        respondent, _ = Respondent.objects.get_or_create(
            team=self.team,
            normalized_name=normalize_name(name),
            birth_date=birth_date,
            gender=gender,
            defaults={'name': name},
        )
        session = client.session
        session['survey_team_id'] = self.team.id
        session['survey_team_code'] = self.team.code
        session['survey_respondent_id'] = respondent.id
        session['survey_day_no'] = 1
        session.save()
        return respondent

    def test_health_survey_renders_deployed_english_question_and_option(self):
        self.question.label_en = 'Do you have a headache?'
        self.question.options_json = [
            {'label': '예', 'score': 1, 'label_en': 'Yes|1'},
            {'label': '아니오', 'score': 0, 'label_en': 'No|0'},
        ]
        self.question.save(update_fields=['label_en', 'options_json', 'updated_at'])
        self._start_session()

        response = self.client.get(
            reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?q=1&lang=en'
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Do you have a headache?')
        self.assertContains(response, '>Yes</span>', html=False)
        self.assertNotContains(response, '>Yes|1</span>', html=False)

    def test_user_result_uses_current_korean_session_after_english_submission(self):
        self.section.title_en = 'Body'
        self.section.save(update_fields=['title_en', 'updated_at'])
        self.question.label_en = 'Do you have a headache?'
        self.question.graph_label = '두통'
        self.question.graph_label_en = 'Headache'
        self.question.options_json = [
            {'label': '예', 'score': 1, 'label_en': 'Yes'},
            {'label': '아니오', 'score': 0, 'label_en': 'No'},
        ]
        self.question.save(
            update_fields=[
                'label_en',
                'graph_label',
                'graph_label_en',
                'options_json',
                'updated_at',
            ]
        )
        self._start_session()
        submit_response = self.client.post(
            reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?q=1&lang=en',
            {f'q_{self.question.id}': '예', 'submit': '1'},
        )
        self.assertEqual(submit_response.status_code, 302)
        submission = SurveySubmission.objects.get(team=self.team, day_no=1)
        self.assertEqual(
            submission.submission_language,
            SurveySubmission.SubmissionLanguage.ENGLISH,
        )
        snapshot_question = submission.form_snapshot_json['sections'][0]['questions'][0]
        self.assertEqual(snapshot_question['label'], '두통이 있습니까?')
        self.assertEqual(snapshot_question['label_en'], 'Do you have a headache?')
        self.assertEqual(snapshot_question['graph_label_en'], 'Headache')

        session = self.client.session
        session[SESSION_PUBLIC_LANGUAGE] = 'ko'
        session.save()
        response = self.client.get(
            reverse('surveys:result-detail', kwargs={'day_no': 1}),
            {'source': 'user', 'return_to': reverse('surveys:preview-v2-home')},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_language'], 'ko')
        self.assertContains(response, '자가검진')
        self.assertContains(response, '두통이 있습니까?')
        self.assertContains(response, '&gt; 예', html=False)
        self.assertNotContains(response, 'Do you have a headache?')

    def test_user_result_uses_current_english_session_after_korean_submission(self):
        self.question.label_en = 'Do you have a headache?'
        self.question.options_json = [
            {'label': '예', 'score': 1, 'label_en': 'Yes'},
            {'label': '아니오', 'score': 0, 'label_en': 'No'},
        ]
        self.question.save(
            update_fields=['label_en', 'options_json', 'updated_at']
        )
        self._start_session()
        self.client.post(
            reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?q=1&lang=ko',
            {f'q_{self.question.id}': '예', 'submit': '1'},
        )
        session = self.client.session
        session[SESSION_PUBLIC_LANGUAGE] = 'en'
        session.save()

        response = self.client.get(
            reverse('surveys:result-detail', kwargs={'day_no': 1}),
            {'source': 'user', 'return_to': reverse('surveys:preview-v2-home')},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_language'], 'en')
        self.assertContains(response, 'Health Check')
        self.assertContains(response, 'Do you have a headache?')
        self.assertContains(response, '&gt; Yes', html=False)
        self.assertNotContains(response, '두통이 있습니까?')

    def test_manager_result_is_korean_for_english_submission(self):
        self.question.label_en = 'Do you have a headache?'
        self.question.options_json = [
            {'label': '예', 'score': 1, 'label_en': 'Yes'},
            {'label': '아니오', 'score': 0, 'label_en': 'No'},
        ]
        self.question.save(
            update_fields=['label_en', 'options_json', 'updated_at']
        )
        self._start_session()
        self.client.post(
            reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?q=1&lang=en',
            {f'q_{self.question.id}': '예', 'submit': '1'},
        )
        submission = SurveySubmission.objects.get(team=self.team, day_no=1)
        manager = get_user_model().objects.create_superuser(
            username='result-manager',
            email='result-manager@example.com',
            password='pass',
        )
        self.client.post(reverse('admin:login'), {'username': manager.username, 'password': 'pass'})
        session = self.client.session
        session['ops_login_team_id'] = self.team.id
        session['ops_login_verified_team_id'] = self.team.id
        session[SESSION_PUBLIC_LANGUAGE] = 'en'
        session.save()

        response = self.client.get(
            reverse('surveys:result-detail', kwargs={'day_no': 1}),
            {
                'source': 'manager',
                'submission_id': submission.id,
                'return_to': reverse('admins:prototype-manager'),
                'lang': 'en',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_language'], 'ko')
        self.assertContains(response, '자가검진')
        self.assertContains(response, '두통이 있습니까?')
        self.assertContains(response, '&gt; 예', html=False)
        self.assertNotContains(response, 'Do you have a headache?')

    def test_submit_success_redirects_to_v2_completion(self):
        self._start_session()

        response = self.client.post(reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?q=1', {f'q_{self.question.id}': '예', 'submit': '1'})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            reverse('surveys:preview-v2-completion', kwargs={'completion_type': 'health'}),
        )
        self.assertEqual(SurveySubmission.objects.count(), 1)

    def test_v2_health_submit_redirects_to_v2_completion(self):
        self._start_session()
        response = self.client.post(
            reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?q=1&source=v2',
            {f'q_{self.question.id}': '예', 'submit': '1'},
        )

        self.assertRedirects(
            response,
            reverse(
                'surveys:preview-v2-completion',
                kwargs={'completion_type': 'health'},
            ),
            fetch_redirect_response=False,
        )
        self.assertEqual(SurveySubmission.objects.count(), 1)

    def _submission_payload_snapshot(self):
        submission = SurveySubmission.objects.get()
        result_json = dict(submission.result_json)
        result_json.pop('submitted_at', None)
        answer_rows = {
            row.question_id: row.answer_json
            for row in SurveyAnswer.objects.filter(submission=submission).order_by('question_id')
        }
        return submission.answers_json, result_json, answer_rows

    def _add_participant(self, *, name, birth_date, phone, gender='M'):
        return Participant.objects.create(
            team=self.team,
            name=name,
            gender=gender,
            birth_date=birth_date,
            phone=phone,
            normalized_name=name,
            normalized_phone=''.join(ch for ch in phone if ch.isdigit()),
            is_active=True,
        )

    def _identified_client(self, *, name='홍길동', birth_date='1985-03-12', phone='010-1234-5678', gender='M'):
        client = Client()
        respondent = self._start_session(name=name, birth_date=birth_date, phone=phone, gender=gender, client=client)
        return client, respondent

    def _form_signature_for_client(self, client):
        response = client.get(reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?q=1')
        self.assertEqual(response.status_code, 200)
        return response.context['survey_form_signature'], response.content.decode()

    def _submit_json_answer(self, client, *, answer):
        signature, _ = self._form_signature_for_client(client)
        response = client.post(
            reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?q=1',
            {
                'survey_form_id': str(self.form.id),
                'survey_form_version': str(self.form.version),
                'survey_form_signature': signature,
                'answers_payload': json.dumps({str(self.question.id): answer}),
                'submit': '1',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        return response

    def test_submit_uses_daily_health_form_when_exit_survey_form_has_higher_version_same_day(self):
        exit_form = SurveyForm.objects.create(
            team=self.team,
            day_no=1,
            title='탐방 종료 설문',
            version=2,
            schema_json={},
            form_type=SurveyForm.FormType.EXIT_SURVEY,
            is_active=True,
        )
        exit_section = SurveySection.objects.create(
            form=exit_form,
            title='설문',
            section_type=SurveySection.SectionType.SURVEY,
            display_order=1,
        )
        SurveyQuestion.objects.create(
            section=exit_section,
            key='overall_satisfaction',
            label='전체 일정에 만족하셨습니까?',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=['만족', '불만족'],
            is_required=True,
            display_order=1,
        )
        self._start_session()

        _, html = self._form_signature_for_client(self.client)
        self.assertIn('두통이 있습니까?', html)
        self.assertNotIn('전체 일정에 만족하셨습니까?', html)

        response = self._submit_json_answer(self.client, answer='예')

        self.assertIn('redirect_url', response.json())
        submission = SurveySubmission.objects.get(team=self.team, day_no=1)
        self.assertEqual(submission.form_version, self.form.version)
        self.assertEqual(submission.answers_json, {'PT_day1': '예'})
        self.assertNotIn('overall_satisfaction', submission.answers_json)

    def _answer_rows_for_submission(self, submission):
        return list(
            SurveyAnswer.objects
            .filter(submission=submission)
            .select_related('question')
            .order_by('question_id')
            .values_list('question_id', 'question__key', 'answer_json')
        )

    def test_json_payload_submission_matches_legacy_question_id_post(self):
        multi_question = SurveyQuestion.objects.create(
            section=self.section,
            key='PT_day1_multi',
            label='동반 증상은 무엇인가요?',
            question_type=SurveyQuestion.QuestionType.MULTI,
            options_json=[{'label': '피로', 'score': 2}, {'label': '어지러움', 'score': 3}],
            is_required=True,
            display_order=2,
        )
        text_question = SurveyQuestion.objects.create(
            section=self.section,
            key='PT_day1_text',
            label='기타 증상을 입력하세요.',
            question_type=SurveyQuestion.QuestionType.TEXT,
            options_json=[],
            is_required=False,
            display_order=3,
        )
        url = reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?q=1'

        self._start_session()
        legacy_response = self.client.post(
            url,
            {
                f'q_{self.question.id}': '예',
                f'q_{multi_question.id}': ['피로', '어지러움'],
                f'q_{text_question.id}': '목이 조금 아픕니다.',
                'submit': '1',
            },
        )
        self.assertEqual(legacy_response.status_code, 302)
        legacy_snapshot = self._submission_payload_snapshot()

        SurveySubmission.objects.all().delete()
        session = self.client.session
        session.pop('survey_answers_draft', None)
        session.save()

        form_response = self.client.get(url)
        self.assertEqual(form_response.status_code, 200)
        json_response = self.client.post(
            url,
            {
                'survey_form_id': str(self.form.id),
                'survey_form_version': str(self.form.version),
                'survey_form_signature': form_response.context['survey_form_signature'],
                'answers_payload': json.dumps(
                    {
                        str(text_question.id): '목이 조금 아픕니다.',
                        str(multi_question.id): ['피로', '어지러움'],
                        str(self.question.id): '예',
                    }
                ),
                'submit': '1',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(json_response.status_code, 200)
        self.assertEqual(
            json_response.json()['redirect_url'],
            reverse('surveys:preview-v2-completion', kwargs={'completion_type': 'health'}),
        )
        self.assertEqual(self._submission_payload_snapshot(), legacy_snapshot)

    def test_low_score_answers_are_not_replaced_with_first_or_highest_options(self):
        self.question.options_json = [
            {'label': '최고점 보기', 'score': 5},
            {'label': '중간점 보기', 'score': 3},
            {'label': '최저점 보기', 'score': 1},
        ]
        self.question.save(update_fields=['options_json', 'updated_at'])
        mental = SurveySection.objects.create(
            form=self.form,
            title='정신',
            section_type=SurveySection.SectionType.MENTAL,
            display_order=2,
        )
        emotion_question = SurveyQuestion.objects.create(
            section=mental,
            key='EQ_low',
            label='기분은 어떤가요?',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=[
                {'label': '감정 최고점', 'score': 5},
                {'label': '감정 중간점', 'score': 3},
                {'label': '감정 최저점', 'score': 1},
            ],
            is_required=True,
            display_order=1,
        )

        self._start_session()
        url = reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?q=1'
        form_response = self.client.get(url)
        response = self.client.post(
            url,
            {
                'survey_form_id': str(self.form.id),
                'survey_form_version': str(self.form.version),
                'survey_form_signature': form_response.context['survey_form_signature'],
                'answers_payload': json.dumps(
                    {
                        str(self.question.id): '최저점 보기',
                        str(emotion_question.id): '감정 최저점',
                    }
                ),
                'submit': '1',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        submission = SurveySubmission.objects.get()
        self.assertEqual(submission.answers_json, {self.question.key: '최저점 보기', emotion_question.key: '감정 최저점'})
        self.assertEqual(
            self._answer_rows_for_submission(submission),
            [],
        )
        self.assertEqual(submission.result_json['sections']['body']['average_score'], 0)
        self.assertEqual(submission.result_json['sections']['emotion']['average_score'], 0)

    def test_mid_and_low_answers_are_saved_as_selected_labels(self):
        self.question.options_json = [
            {'label': '최고점 보기', 'score': 5},
            {'label': '중간점 보기', 'score': 3},
            {'label': '최저점 보기', 'score': 1},
        ]
        self.question.save(update_fields=['options_json', 'updated_at'])
        second_question = SurveyQuestion.objects.create(
            section=self.section,
            key='PT_day1_second',
            label='몸 상태는 어떤가요?',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=[
                {'label': '몸 최고점', 'score': 5},
                {'label': '몸 중간점', 'score': 3},
                {'label': '몸 최저점', 'score': 1},
            ],
            is_required=True,
            display_order=2,
        )

        self._start_session()
        url = reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?q=1'
        form_response = self.client.get(url)
        response = self.client.post(
            url,
            {
                'survey_form_id': str(self.form.id),
                'survey_form_version': str(self.form.version),
                'survey_form_signature': form_response.context['survey_form_signature'],
                'answers_payload': json.dumps(
                    {
                        str(self.question.id): '중간점 보기',
                        str(second_question.id): '몸 최저점',
                    }
                ),
                'submit': '1',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        submission = SurveySubmission.objects.get()
        self.assertEqual(submission.answers_json[self.question.key], '중간점 보기')
        self.assertEqual(submission.answers_json[second_question.key], '몸 최저점')
        self.assertEqual(submission.result_json['sections']['body']['average_score'], 25)

    def test_missing_required_choice_is_not_filled_with_first_option(self):
        self.question.options_json = [
            {'label': '최고점 보기', 'score': 5},
            {'label': '최저점 보기', 'score': 1},
        ]
        self.question.save(update_fields=['options_json', 'updated_at'])

        self._start_session()
        url = reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?q=1'
        form_response = self.client.get(url)
        response = self.client.post(
            url,
            {
                'survey_form_id': str(self.form.id),
                'survey_form_version': str(self.form.version),
                'survey_form_signature': form_response.context['survey_form_signature'],
                'answers_payload': json.dumps({}),
                'submit': '1',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('필수 문항', response.json()['error'])
        self.assertEqual(SurveySubmission.objects.count(), 0)

    def test_conflicting_answers_payload_and_question_post_is_blocked(self):
        self.question.options_json = [
            {'label': '최고점 보기', 'score': 5},
            {'label': '최저점 보기', 'score': 1},
        ]
        self.question.save(update_fields=['options_json', 'updated_at'])

        self._start_session()
        url = reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?q=1'
        form_response = self.client.get(url)
        response = self.client.post(
            url,
            {
                'survey_form_id': str(self.form.id),
                'survey_form_version': str(self.form.version),
                'survey_form_signature': form_response.context['survey_form_signature'],
                'answers_payload': json.dumps({str(self.question.id): '최고점 보기'}),
                f'q_{self.question.id}': '최저점 보기',
                'submit': '1',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('일치하지 않습니다', response.json()['error'])
        self.assertEqual(SurveySubmission.objects.count(), 0)

    def test_day_survey_choice_inputs_use_question_id_and_no_default_checked(self):
        self.question.options_json = [
            {'label': '최고점 보기', 'score': 5},
            {'label': '최저점 보기', 'score': 1},
        ]
        self.question.save(update_fields=['options_json', 'updated_at'])
        self._start_session()

        response = self.client.get(reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?q=1')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        input_tags = re.findall(r'<input[^>]+class="survey-choice__input"[^>]*>', html)
        self.assertEqual(len(input_tags), 2)
        for tag in input_tags:
            self.assertIn(f'name="q_{self.question.id}"', tag)
            self.assertIn(f'data-question-id="{self.question.id}"', tag)
            self.assertNotIn('checked', tag)
        self.assertIn('value="최고점 보기"', input_tags[0])
        self.assertIn('value="최저점 보기"', input_tags[1])

    def test_day_survey_submit_script_builds_payload_from_dom_controls(self):
        self._start_session()
        response = self.client.get(reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?q=1')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('function buildPayloadFromDom()', html)
        self.assertIn('readVisibleValue(question)', html)
        self.assertIn('readMirrorValue(question)', html)
        self.assertIn('answersPayload.value = JSON.stringify(payloadAnswers);', html)

    def test_two_respondents_store_independent_answers_results_and_result_pages(self):
        self.question.options_json = [{'label': '낮음', 'score': 1}, {'label': '높음', 'score': 5}]
        self.question.save(update_fields=['options_json', 'updated_at'])
        self._add_participant(name='김철수', birth_date='1990-02-03', phone='01022223333')

        client_a, respondent_a = self._identified_client()
        client_b, respondent_b = self._identified_client(
            name='김철수',
            birth_date='1990-02-03',
            phone='010-2222-3333',
        )

        self._submit_json_answer(client_a, answer='낮음')
        self._submit_json_answer(client_b, answer='높음')

        submission_a = SurveySubmission.objects.get(team=self.team, respondent=respondent_a, day_no=1)
        submission_b = SurveySubmission.objects.get(team=self.team, respondent=respondent_b, day_no=1)
        self.assertNotEqual(submission_a.id, submission_b.id)
        self.assertNotEqual(submission_a.respondent_id, submission_b.respondent_id)
        self.assertEqual(submission_a.answers_json, {self.question.key: '낮음'})
        self.assertEqual(submission_b.answers_json, {self.question.key: '높음'})
        self.assertNotEqual(submission_a.answers_json, submission_b.answers_json)
        self.assertEqual(self._answer_rows_for_submission(submission_a), [])
        self.assertEqual(self._answer_rows_for_submission(submission_b), [])
        self.assertEqual(submission_a.result_json['sections']['body']['average_score'], 0)
        self.assertEqual(submission_b.result_json['sections']['body']['average_score'], 100)
        self.assertNotEqual(submission_a.result_json['sections']['body']['average_score'], submission_b.result_json['sections']['body']['average_score'])

        result_a = client_a.get(reverse('surveys:result', kwargs={'day_no': 1}))
        result_b = client_b.get(reverse('surveys:result', kwargs={'day_no': 1}))
        self.assertEqual(result_a.status_code, 200)
        self.assertEqual(result_b.status_code, 200)
        self.assertEqual(result_a.context['body_result']['score'], 0)
        self.assertEqual(result_b.context['body_result']['score'], 100)

    def test_day_survey_storage_key_includes_submission_scope_values(self):
        client, respondent = self._identified_client()
        _, html = self._form_signature_for_client(client)
        expected_storage_key = (
            f'survey-draft:team-{self.team.id}:respondent-{respondent.id}:'
            f'day-1:form-{self.form.id}:v{self.form.version}'
        )
        self.assertIn(f'data-storage-key="{expected_storage_key}"', html)
        self.assertNotIn('respondent-None', html)
        self.assertNotIn('respondent-:day', html)

    def test_json_payload_ignores_unknown_question_and_invalid_option_values(self):
        self._start_session()
        url = reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?q=1'
        form_response = self.client.get(url)
        response = self.client.post(
            url,
            {
                'survey_form_id': str(self.form.id),
                'survey_form_version': str(self.form.version),
                'survey_form_signature': form_response.context['survey_form_signature'],
                'answers_payload': json.dumps(
                    {
                        str(self.question.id): '없는 선택지',
                        '999999': '예',
                    }
                ),
                'submit': '1',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['question_index'], 1)
        self.assertEqual(SurveySubmission.objects.count(), 0)

    def test_duplicate_question_keys_are_not_saved_with_json_payload(self):
        mental = SurveySection.objects.create(
            form=self.form,
            title='정신',
            section_type=SurveySection.SectionType.MENTAL,
            display_order=2,
        )
        SurveyQuestion.objects.create(
            section=mental,
            key=self.question.key,
            label='같은 키를 가진 문항입니다.',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=['예', '아니오'],
            is_required=True,
            display_order=1,
        )

        self._start_session()
        url = reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?q=1'
        form_response = self.client.get(url)
        question_payload = {
            str(question.id): '예'
            for question in SurveyQuestion.objects.filter(section__form=self.form)
        }
        response = self.client.post(
            url,
            {
                'survey_form_id': str(self.form.id),
                'survey_form_version': str(self.form.version),
                'survey_form_signature': form_response.context['survey_form_signature'],
                'answers_payload': json.dumps(question_payload),
                'submit': '1',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('설문 문항 설정', response.json()['error'])
        self.assertEqual(SurveySubmission.objects.count(), 0)

    def test_duplicate_submit_blocked_and_redirect_to_result(self):
        self._start_session()
        self.client.post(reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?q=1', {f'q_{self.question.id}': '예', 'submit': '1'})

        response = self.client.get(reverse('surveys:day-survey', kwargs={'day_no': 1}))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            reverse('surveys:preview-v2-completion', kwargs={'completion_type': 'health'}),
        )
        self.assertEqual(SurveySubmission.objects.count(), 1)

    def test_common_questions_replace_existing_active_form_as_new_version(self):
        common_set = CommonQuestionSet.objects.create(name='공통1', title='1일차', day_no=1, is_active=True)
        common_section = CommonQuestionSection.objects.create(
            question_set=common_set,
            title='공통 섹션',
            section_type=SurveySection.SectionType.PHYSICAL,
            display_order=2,
        )
        common_question = CommonQuestion.objects.create(
            section=common_section,
            key='common_day1',
            label='공통 문항입니다.',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=[{'label': '예', 'score': 1}],
            is_required=True,
            display_order=1,
        )
        result = CommonQuestionSetAdmin(CommonQuestionSet, AdminSite())._deploy_to_team(
            question_set=common_set,
            team=self.team,
        )
        target_form = SurveyForm.objects.get(team=self.team, day_no=1, is_active=True)
        self.form.refresh_from_db()
        self.assertNotEqual(target_form.id, self.form.id)
        self.assertFalse(self.form.is_active)
        self.assertEqual(target_form.version, 2)
        self.assertEqual(result.status, CommonQuestionSetDeployment.Status.CREATED)
        self.assertTrue(
            SurveyQuestion.objects.filter(
                section__form=target_form,
                key=common_question.key,
            ).exists()
        )
        view_question_count = SurveyQuestion.objects.filter(section__form=target_form).count()
        self.assertEqual(view_question_count, 1)

        self._start_session()
        response_q1 = self.client.get(reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?q=1')
        self.assertEqual(response_q1.status_code, 200)
        self.assertContains(response_q1, '문항 1 / 1')

    def test_result_page_renders_summary_cards(self):
        self._start_session()
        response = self.client.get(reverse('surveys:result', kwargs={'day_no': 1}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Health')
        self.assertContains(response, 'Psychology')
        self.assertContains(response, '제출 전')
        self.assertContains(response, '기타 특이사항 없음')
        self.assertContains(response, reverse('surveys:result-detail', kwargs={'day_no': 1}), html=False)
        self.assertContains(response, '#body')
        self.assertContains(response, '#emotion')

    def test_result_page_marks_special_notes_when_text_answer_exists(self):
        text_question = SurveyQuestion.objects.create(
            section=self.section,
            key='PT_note',
            label='기타 특이사항을 적어주세요',
            question_type=SurveyQuestion.QuestionType.TEXT,
            options_json=[],
            is_required=False,
            display_order=2,
        )
        self._start_session()
        respondent = Respondent.objects.get(team=self.team, normalized_name='홍길동')
        SurveySubmission.objects.create(
            team=self.team,
            respondent=respondent,
            day_no=1,
            answers_json={self.question.key: '예', text_question.key: '어지러움이 함께 있습니다.'},
            result_json={
                'sections': {
                    'body': {
                        'average_score': 82,
                        'status_label': '최상',
                        'surveys': [
                            {
                                'question_id': self.question.id,
                                'question_key': self.question.key,
                                'graph_label': '두통',
                                'score': 75,
                            }
                        ],
                    },
                    'emotion': {'average_score': 0, 'status_label': '설문 전', 'surveys': []},
                }
            },
            form_version=1,
            form_snapshot_json={},
            is_locked=True,
            submitted_at=timezone.now(),
        )

        response = self.client.get(reverse('surveys:result', kwargs={'day_no': 1}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '기타 특이사항 있음')
        self.assertContains(response, '기타 특이사항 없음')

    def test_result_page_uses_return_to_query_for_back_and_detail_links(self):
        self._start_session()
        response = self.client.get(
            reverse('surveys:result', kwargs={'day_no': 1}),
            {'source': 'user', 'return_to': '/surveys/preview/v2/home/'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="/surveys/preview/v2/home/"', html=False)
        self.assertContains(
            response,
            '/surveys/day/1/result/detail/?source=user&amp;return_to=%2Fsurveys%2Fpreview%2Fv2%2Fhome%2F#body',
            html=False,
        )

    def test_result_payload_queries_only_selected_day_submission(self):
        self._start_session()
        respondent = Respondent.objects.get(team=self.team, normalized_name='홍길동')
        SurveySubmission.objects.create(
            team=self.team,
            respondent=respondent,
            day_no=1,
            answers_json={self.question.key: '예'},
            result_json={
                'sections': {
                    'body': {
                        'average_score': 82,
                        'status_label': '최상',
                        'surveys': [
                            {
                                'question_id': self.question.id,
                                'question_key': self.question.key,
                                'graph_label': '두통',
                                'score': 75,
                            }
                        ],
                    },
                    'emotion': {'average_score': 0, 'status_label': '설문 전', 'surveys': []},
                }
            },
            form_version=1,
            form_snapshot_json={},
            is_locked=True,
            submitted_at=timezone.now(),
        )

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(
                reverse('surveys:result', kwargs={'day_no': 2}),
                {'source': 'user', 'return_to': '/surveys/status/'},
            )

        self.assertEqual(response.status_code, 200)
        submission_queries = [
            query['sql']
            for query in queries
            if 'FROM "survey_submissions"' in query['sql']
        ]
        self.assertTrue(
            any('"survey_submissions"."day_no" = 2' in sql for sql in submission_queries),
            submission_queries,
        )
        self.assertFalse(
            any('"survey_submissions"."day_no" <= ' in sql for sql in submission_queries),
            submission_queries,
        )

    def test_result_detail_page_renders_combined_sections(self):
        self._start_session()
        respondent = Respondent.objects.get(team=self.team, normalized_name='홍길동')
        SurveySubmission.objects.create(
            team=self.team,
            respondent=respondent,
            day_no=1,
            answers_json={self.question.key: '예'},
            result_json={
                'sections': {
                    'body': {
                        'average_score': 82,
                        'status_label': '최상',
                        'surveys': [
                            {
                                'question_id': self.question.id,
                                'question_key': self.question.key,
                                'graph_label': '두통',
                                'score': 75,
                            }
                        ],
                    },
                    'emotion': {
                        'average_score': 0,
                        'status_label': '설문 전',
                        'surveys': [],
                    },
                }
            },
            form_version=1,
            form_snapshot_json={},
            is_locked=True,
            submitted_at=timezone.now(),
        )
        response = self.client.get(reverse('surveys:result-detail', kwargs={'day_no': 1}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="body"', html=False)
        self.assertContains(response, 'id="emotion"', html=False)
        self.assertContains(response, 'Health')
        self.assertContains(response, 'Psychology')
        self.assertContains(response, '두통이 있습니까?')
        self.assertContains(response, '&gt; 예', html=False)
        self.assertContains(response, 'style="width: 75%"', html=False)
        self.assertContains(response, 'style="left: 75%"', html=False)
        self.assertNotContains(response, 'graph-up_green.svg')
        self.assertNotContains(response, 'graph-up_purple.svg')

    def test_result_detail_day_tabs_keep_detail_context(self):
        self._start_session()
        response = self.client.get(
            reverse('surveys:result-detail', kwargs={'day_no': 1}),
            {'source': 'user', 'return_to': '/surveys/status/'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            '/surveys/day/2/result/detail/?source=user&amp;return_to=%2Fsurveys%2Fstatus%2F',
            html=False,
        )
        self.assertNotContains(
            response,
            '/surveys/day/2/result/?source=user&amp;return_to=%2Fsurveys%2Fstatus%2F',
            html=False,
        )

    def test_result_page_rejects_external_return_to(self):
        self._start_session()
        response = self.client.get(
            reverse('surveys:result', kwargs={'day_no': 1}),
            {'source': 'manager', 'return_to': 'https://evil.example/out'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('admins:prototype-manager') + '?day_no=1')

    def test_day_survey_uses_section_scoped_progress_and_title(self):
        mental = SurveySection.objects.create(
            form=self.form,
            title='정신',
            section_type=SurveySection.SectionType.MENTAL,
            display_order=2,
        )
        SurveyQuestion.objects.create(
            section=mental,
            key='EM_day1',
            label='현재 기분 상태는 어떤가요?',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=['매우 좋음', '보통'],
            is_required=True,
            display_order=1,
        )

        self._start_session()

        response_body = self.client.get(reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?q=1')
        self.assertEqual(response_body.status_code, 200)
        self.assertContains(response_body, 'Body')
        self.assertContains(response_body, '1 / 1 문항')
        self.assertContains(response_body, '신체 영역')
        self.assertContains(response_body, 'survey-page--body')
        self.assertContains(response_body, '계속하기')
        self.assertContains(
            response_body,
            f'href="{reverse("surveys:preview-v2-home")}"',
        )

        response_emotion = self.client.get(reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?q=2')
        self.assertEqual(response_emotion.status_code, 200)
        self.assertContains(response_emotion, 'Emotion')
        self.assertContains(response_emotion, '1 / 1 문항')
        self.assertContains(response_emotion, '정신 영역')
        self.assertContains(response_emotion, 'survey-page--emotion')

    def test_manager_entry_code_redirects_to_manager_login(self):
        response = self.client.post(reverse('surveys:preview-v2-code'), {'team_code': 'goyang-admin'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('/ops/login/', response.url)

    def test_manager_entry_code_clears_verified_admin_team_session(self):
        session = self.client.session
        session['ops_login_verified_team_id'] = 999
        session.save()

        response = self.client.post(reverse('surveys:preview-v2-code'), {'team_code': 'goyang-admin'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('/ops/login/', response.url)
        self.assertIsNone(self.client.session.get('ops_login_verified_team_id'))


class SurveyAdminOptionFormTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(
            name='테스트팀',
            code='team-code',
            is_active=True,
            start_date=date.today(),
            max_day_no=3,
        )
        self.form = SurveyForm.objects.create(team=self.team, day_no=1, title='1일차', version=1, schema_json={}, is_active=True)
        self.section = SurveySection.objects.create(
            form=self.form,
            title='신체',
            section_type=SurveySection.SectionType.PHYSICAL,
            display_order=1,
        )

    def test_option_lines_are_converted_to_scored_options_json(self):
        form = SurveyQuestionForm(
            data={
                'section': self.section.id,
                'key': 'q_score',
                'label': '증상 정도',
                'graph_label': '증상',
                'question_type': SurveyQuestion.QuestionType.SINGLE,
                'is_required': True,
                'display_order': 1,
                'option_lines': '전혀 없음|1\n가끔 있음|2\n자주 있음|3',
                'options_json': '[]',
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        question = form.save()
        self.assertEqual(
            question.options_json,
            [
                {'label': '전혀 없음', 'score': 1},
                {'label': '가끔 있음', 'score': 2},
                {'label': '자주 있음', 'score': 3},
            ],
        )
        self.assertEqual(question.graph_label, '증상')

    def test_english_question_and_option_labels_are_saved_with_korean_scores(self):
        form = SurveyQuestionForm(
            data={
                'section': self.section.id,
                'key': 'q_score_en',
                'label': '증상 정도',
                'label_en': 'Symptom severity',
                'graph_label': '증상',
                'graph_label_en': 'Symptom',
                'question_type': SurveyQuestion.QuestionType.SINGLE,
                'is_required': True,
                'display_order': 1,
                'option_lines': '전혀 없음|1\n가끔 있음|2',
                'option_lines_en': 'Not at all\nSometimes',
                'options_json': '[]',
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        question = form.save()
        self.assertEqual(question.label_en, 'Symptom severity')
        self.assertEqual(
            question.options_json,
            [
                {'label': '전혀 없음', 'score': 1, 'label_en': 'Not at all'},
                {'label': '가끔 있음', 'score': 2, 'label_en': 'Sometimes'},
            ],
        )

    def test_english_option_labels_drop_accidentally_repeated_scores(self):
        form = SurveyQuestionForm(
            data={
                'section': self.section.id,
                'key': 'q_score_en_legacy',
                'label': '증상 정도',
                'label_en': 'Symptom severity',
                'graph_label': '증상',
                'graph_label_en': 'Symptom',
                'question_type': SurveyQuestion.QuestionType.SINGLE,
                'is_required': True,
                'display_order': 1,
                'option_lines': '전혀 없음|1\n가끔 있음|2',
                'option_lines_en': 'Not at all|1\nSometimes|2',
                'options_json': '[]',
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        question = form.save()
        self.assertEqual(
            question.options_json,
            [
                {'label': '전혀 없음', 'score': 1, 'label_en': 'Not at all'},
                {'label': '가끔 있음', 'score': 2, 'label_en': 'Sometimes'},
            ],
        )

    def test_existing_string_options_are_shown_as_score_zero_lines(self):
        question = SurveyQuestion.objects.create(
            section=self.section,
            key='q_old',
            label='기존 형식',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=['예', '아니오'],
            is_required=True,
            display_order=2,
        )
        form = SurveyQuestionForm(instance=question)
        self.assertEqual(form.fields['option_lines'].initial, '예|0\n아니오|0')

    def test_survey_form_admin_auto_increments_version_per_team_day(self):
        request = RequestFactory().post('/admin/surveys/surveyform/add/')
        request.user = get_user_model().objects.create_superuser('root2', 'root2@example.com', 'pass')
        admin_obj = SurveyFormAdmin(SurveyForm, AdminSite())

        first = SurveyForm(team=self.team, day_no=2, title='2일차', is_active=True, schema_json={})
        admin_obj.save_model(request, first, form=None, change=False)
        second = SurveyForm(team=self.team, day_no=2, title='2일차 v2', is_active=True, schema_json={})
        admin_obj.save_model(request, second, form=None, change=False)

        self.assertEqual(first.version, 1)
        self.assertEqual(second.version, 2)

    def test_survey_question_key_validation_on_create(self):
        form = SurveyQuestionForm(
            data={
                'section': self.section.id,
                'key': '잘못된 키',
                'label': '문항',
                'question_type': SurveyQuestion.QuestionType.SINGLE,
                'is_required': True,
                'display_order': 3,
                'option_lines': '예|1',
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn('key', form.errors)

    def test_survey_question_admin_change_form_handles_readonly_key_without_keyerror(self):
        question = SurveyQuestion.objects.create(
            section=self.section,
            key='q_existing',
            label='기존 문항',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=[{'label': '예', 'score': 1}],
            is_required=True,
            display_order=4,
        )
        request = RequestFactory().get('/admin/surveys/surveyquestion/{}/change/'.format(question.id))
        request.user = get_user_model().objects.create_superuser('admin-change', 'admin-change@example.com', 'pass')

        admin_obj = SurveyQuestionAdmin(SurveyQuestion, AdminSite())
        form_class = admin_obj.get_form(request, obj=question)
        form = form_class(instance=question)
        self.assertIn('option_lines', form.fields)

    def test_common_question_admin_layout_matches_survey_question_flow(self):
        common_set = CommonQuestionSet.objects.create(name='layout-set', title='레이아웃', day_no=1, is_active=True)
        common_section = CommonQuestionSection.objects.create(
            question_set=common_set,
            title='공통 섹션',
            section_type=SurveySection.SectionType.PHYSICAL,
            display_order=1,
        )
        common_question = CommonQuestion.objects.create(
            section=common_section,
            key='q_common_layout',
            label='공통 문항 레이아웃',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=[{'label': '예', 'score': 1}],
            is_required=True,
            display_order=1,
        )
        request = RequestFactory().get('/admin/surveys/commonquestion/{}/change/'.format(common_question.id))
        request.user = get_user_model().objects.create_superuser('admin-layout', 'admin-layout@example.com', 'pass')
        survey_question = SurveyQuestion.objects.create(
            section=self.section,
            key='q_survey_layout',
            label='설문 문항 레이아웃',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=[{'label': '예', 'score': 1}],
            is_required=True,
            display_order=10,
        )

        survey_admin = SurveyQuestionAdmin(SurveyQuestion, AdminSite())
        common_admin = CommonQuestionAdmin(CommonQuestion, AdminSite())
        survey_sections = [title for title, _ in survey_admin.get_fieldsets(request, obj=survey_question)]
        common_sections = [title for title, _ in common_admin.get_fieldsets(request, obj=common_question)]
        self.assertEqual(survey_sections, common_sections)


class CommonQuestionSetDeployTest(TestCase):
    def test_section_type_supports_physical_mental_and_survey(self):
        self.assertEqual(
            list(SurveySection.SectionType.choices),
            [
                ('physical', '신체'),
                ('mental', '정신'),
                ('survey', '설문'),
            ],
        )
        section_type_field = CommonQuestionSection._meta.get_field(
            'section_type'
        )
        self.assertEqual(
            list(section_type_field.choices),
            list(SurveySection.SectionType.choices),
        )

    def test_common_question_set_stores_supported_form_types(self):
        health_template = CommonQuestionSet.objects.create(
            name='자가검진 유형',
            title='자가검진',
            day_no=1,
        )
        exit_template = CommonQuestionSet.objects.create(
            name='종료 설문 유형',
            title='탐방 종료 설문',
            form_type=CommonQuestionSet.FormType.EXIT_SURVEY,
            day_no=1,
        )

        self.assertEqual(
            health_template.form_type,
            CommonQuestionSet.FormType.DAILY_HEALTH,
        )
        self.assertEqual(health_template.get_form_type_display(), '자가건강검진')
        self.assertEqual(exit_template.get_form_type_display(), '탐방 종료 설문')

    def test_internal_form_section_and_question_admins_are_hidden_from_menu(self):
        user = get_user_model().objects.create_superuser(
            username='internal-menu-admin',
            email='internal@example.com',
            password='test-password',
        )
        request = RequestFactory().get('/admin/')
        request.user = user

        internal_admins = (
            SurveyFormAdmin(SurveyForm, AdminSite()),
            SurveySectionAdmin(SurveySection, AdminSite()),
            SurveyQuestionAdmin(SurveyQuestion, AdminSite()),
        )
        for model_admin in internal_admins:
            with self.subTest(model=model_admin.model.__name__):
                self.assertEqual(model_admin.get_model_perms(request), {})
                self.assertTrue(model_admin.has_view_permission(request))
                self.assertTrue(model_admin.has_change_permission(request))

    def test_common_survey_admin_remains_visible(self):
        user = get_user_model().objects.create_superuser(
            username='common-survey-menu-admin',
            email='common@example.com',
            password='test-password',
        )
        request = RequestFactory().get('/admin/')
        request.user = user
        common_admin = CommonQuestionSetAdmin(CommonQuestionSet, AdminSite())

        self.assertTrue(common_admin.get_model_perms(request)['view'])

    def test_common_question_set_admin_exposes_form_type(self):
        admin_obj = CommonQuestionSetAdmin(CommonQuestionSet, AdminSite())

        self.assertIn('survey_type', admin_obj.list_display)
        self.assertIn('form_type', admin_obj.list_filter)
        basic_fields = admin_obj.fieldsets[0][1]['fields']
        self.assertIn('form_type', basic_fields)
        self.assertEqual(CommonQuestionSet._meta.verbose_name, '공통 설문')

    def test_common_question_set_admin_form_uses_operator_labels_and_strips_text(self):
        form = CommonQuestionSetAdminForm(
            data={
                'name': '  1일차 기본 자가검진  ',
                'title': '  오늘의 자가검진  ',
                'form_type': CommonQuestionSet.FormType.DAILY_HEALTH,
                'day_no': 1,
                'is_active': 'on',
                'description': '  운영팀 참고 메모  ',
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.fields['name'].label, '설문 관리명')
        self.assertEqual(form.fields['title'].label, '참가자 화면 제목')
        self.assertEqual(form.fields['day_no'].label, '적용 일차')
        self.assertEqual(form.cleaned_data['name'], '1일차 기본 자가검진')
        self.assertEqual(form.cleaned_data['title'], '오늘의 자가검진')
        self.assertEqual(form.cleaned_data['description'], '운영팀 참고 메모')

    def test_common_question_set_admin_uses_section_and_question_inlines(self):
        admin_obj = CommonQuestionSetAdmin(CommonQuestionSet, AdminSite())

        self.assertEqual(
            admin_obj.inlines,
            (CommonQuestionSectionInline, CommonQuestionInline),
        )

    def test_common_question_automatically_tracks_its_parent_question_set(self):
        question_set = CommonQuestionSet.objects.create(
            name='인라인 설문',
            title='인라인 설문',
            day_no=1,
        )
        section = CommonQuestionSection.objects.create(
            question_set=question_set,
            title='신체',
            section_type=SurveySection.SectionType.PHYSICAL,
            display_order=1,
        )

        question = CommonQuestion.objects.create(
            section=section,
            key='inline_q1',
            label='현재 상태는 어떤가요?',
            question_type=SurveyQuestion.QuestionType.TEXT,
            display_order=1,
        )

        self.assertEqual(question.question_set, question_set)

    def test_common_question_form_rejects_section_from_another_question_set(self):
        first_set = CommonQuestionSet.objects.create(
            name='첫 설문',
            title='첫 설문',
            day_no=1,
        )
        second_set = CommonQuestionSet.objects.create(
            name='둘째 설문',
            title='둘째 설문',
            day_no=1,
        )
        first_section = CommonQuestionSection.objects.create(
            question_set=first_set,
            title='첫 섹션',
            section_type=SurveySection.SectionType.PHYSICAL,
            display_order=1,
        )
        second_section = CommonQuestionSection.objects.create(
            question_set=second_set,
            title='둘째 섹션',
            section_type=SurveySection.SectionType.MENTAL,
            display_order=1,
        )
        question = CommonQuestion.objects.create(
            section=first_section,
            key='fixed_parent',
            label='기존 문항',
            question_type=SurveyQuestion.QuestionType.TEXT,
            display_order=1,
        )
        form = CommonQuestionForm(
            data={
                'section': second_section.pk,
                'key': question.key,
                'label': question.label,
                'graph_label': '',
                'question_type': SurveyQuestion.QuestionType.TEXT,
                'option_lines': '',
                'is_required': 'on',
                'display_order': 1,
            },
            instance=question,
        )

        self.assertFalse(form.is_valid())
        self.assertIn('section', form.errors)

    def test_common_question_inline_saves_option_lines_to_options_json(self):
        user_model = get_user_model()
        user_model.objects.create_superuser(
            username='inline-admin',
            password='pass',
            email='inline@example.com',
        )
        self.client.login(username='inline-admin', password='pass')
        question_set = CommonQuestionSet.objects.create(
            name='인라인 저장 설문',
            title='인라인 저장 설문',
            day_no=1,
        )
        section = CommonQuestionSection.objects.create(
            question_set=question_set,
            title='신체',
            section_type=SurveySection.SectionType.PHYSICAL,
            display_order=1,
        )
        response = self.client.post(
            reverse('admin:surveys_commonquestionset_change', args=[question_set.pk]),
            {
                'name': question_set.name,
                'title': question_set.title,
                'form_type': CommonQuestionSet.FormType.DAILY_HEALTH,
                'day_no': 1,
                'is_active': 'on',
                'description': '',
                'sections-TOTAL_FORMS': '1',
                'sections-INITIAL_FORMS': '1',
                'sections-MIN_NUM_FORMS': '0',
                'sections-MAX_NUM_FORMS': '1000',
                'sections-0-id': str(section.pk),
                'sections-0-question_set': str(question_set.pk),
                'sections-0-title': section.title,
                'sections-0-section_type': SurveySection.SectionType.PHYSICAL,
                'sections-0-display_order': '1',
                'questions-TOTAL_FORMS': '1',
                'questions-INITIAL_FORMS': '0',
                'questions-MIN_NUM_FORMS': '0',
                'questions-MAX_NUM_FORMS': '1000',
                'questions-0-id': '',
                'questions-0-question_set': str(question_set.pk),
                'questions-0-section': str(section.pk),
                'questions-0-key': 'inline_scored',
                'questions-0-label': '현재 컨디션은 어떤가요?',
                'questions-0-graph_label': '컨디션',
                'questions-0-question_type': SurveyQuestion.QuestionType.SINGLE,
                'questions-0-option_lines': '좋음|1\n보통|3\n나쁨|5',
                'questions-0-is_required': 'on',
                'questions-0-display_order': '1',
                '_save': '저장',
            },
        )

        self.assertEqual(response.status_code, 302)
        question = CommonQuestion.objects.get(
            question_set=question_set,
            key='inline_scored',
        )
        self.assertEqual(
            question.options_json,
            [
                {'label': '좋음', 'score': 1},
                {'label': '보통', 'score': 3},
                {'label': '나쁨', 'score': 5},
            ],
        )

    def test_common_question_set_deploys_to_selected_teams_even_if_submission_exists(self):
        team_a = Team.objects.create(name='A팀', code='A001', manager_entry_code='A-MGR', start_date=date.today(), max_day_no=3, is_active=True)
        team_b = Team.objects.create(name='B팀', code='B001', manager_entry_code='B-MGR', start_date=date.today(), max_day_no=1, is_active=True)

        template = CommonQuestionSet.objects.create(name='기본세트', title='공통 1일차', day_no=1, is_active=True)
        section = CommonQuestionSection.objects.create(question_set=template, title='신체', section_type=SurveySection.SectionType.PHYSICAL, display_order=1)
        CommonQuestion.objects.create(
            section=section,
            key='q_common_1',
            label='오늘 컨디션은 어떤가요?',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=[{'label': '좋음', 'score': 1}, {'label': '보통', 'score': 2}],
            is_required=True,
            display_order=1,
        )

        SurveySubmission.objects.create(
            team=team_b,
            respondent=Respondent.objects.create(
                team=team_b,
                name='테스터',
                birth_date='1990-01-01',
                phone='01011112222',
                normalized_name='테스터',
                normalized_phone='01011112222',
            ),
            day_no=1,
            answers_json={'q': 'a'},
            result_json={'status': 'submitted'},
            form_version=1,
            form_snapshot_json={},
            is_locked=True,
            submitted_at=timezone.now(),
        )

        admin_obj = CommonQuestionSetAdmin(CommonQuestionSet, AdminSite())
        results = admin_obj.deploy_to_selected_teams(question_set=template, teams=[team_a, team_b])

        self.assertEqual([result.status for result in results], [
            CommonQuestionSetDeployment.Status.CREATED,
            CommonQuestionSetDeployment.Status.CREATED,
        ])
        self.assertTrue(SurveyForm.objects.filter(team=team_a, day_no=1, title='공통 1일차').exists())
        self.assertTrue(SurveyForm.objects.filter(team=team_b, day_no=1, title='공통 1일차').exists())

    def test_common_question_deploy_respects_team_exclusion_keys(self):
        team = Team.objects.create(name='A팀', code='A001X', manager_entry_code='A-MGR-X', start_date=date.today(), max_day_no=3, is_active=True)
        template = CommonQuestionSet.objects.create(name='제외세트', title='공통 2일차', day_no=2, is_active=True)
        section = CommonQuestionSection.objects.create(question_set=template, title='신체', section_type=SurveySection.SectionType.PHYSICAL, display_order=1)
        CommonQuestion.objects.create(
            section=section,
            key='q_keep',
            label='남길 문항',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=[{'label': '예', 'score': 1}],
            is_required=True,
            display_order=1,
        )
        CommonQuestion.objects.create(
            section=section,
            key='q_skip',
            label='제외 문항',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=[{'label': '예', 'score': 1}],
            is_required=True,
            display_order=2,
        )
        TeamCommonQuestionExclusion.objects.create(team=team, question_set=template, question_key='q_skip')

        admin_obj = CommonQuestionSetAdmin(CommonQuestionSet, AdminSite())
        result = admin_obj._deploy_to_team(question_set=template, team=team)
        self.assertEqual(result.status, CommonQuestionSetDeployment.Status.CREATED)

        form = SurveyForm.objects.get(team=team, day_no=2, title='공통 2일차')
        deployed_keys = set(SurveyQuestion.objects.filter(section__form=form).values_list('key', flat=True))
        self.assertIn('q_keep', deployed_keys)
        self.assertNotIn('q_skip', deployed_keys)

    def test_common_question_set_deploy_creates_new_version_for_changed_active_form(self):
        team = Team.objects.create(name='폼팀', code='FORM1', manager_entry_code='FORM-MGR1', start_date=date.today(), max_day_no=3, is_active=True)
        existing_form = SurveyForm.objects.create(team=team, day_no=1, title='1일차', version=1, schema_json={}, is_active=True)
        section = SurveySection.objects.create(
            form=existing_form,
            title='기존 섹션',
            section_type=SurveySection.SectionType.PHYSICAL,
            display_order=1,
        )
        SurveyQuestion.objects.create(
            section=section,
            key='q_team',
            label='팀 문항',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=[{'label': '예', 'score': 1}],
            is_required=True,
            display_order=1,
        )

        template = CommonQuestionSet.objects.create(name='공통-동일제목', title='1일차', day_no=1, is_active=True)
        common_section = CommonQuestionSection.objects.create(
            question_set=template,
            title='공통 섹션',
            section_type=SurveySection.SectionType.PHYSICAL,
            display_order=2,
        )
        CommonQuestion.objects.create(
            section=common_section,
            key='q_common',
            label='공통 문항',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=[{'label': '예', 'score': 1}],
            is_required=True,
            display_order=1,
        )

        admin_obj = CommonQuestionSetAdmin(CommonQuestionSet, AdminSite())
        result = admin_obj._deploy_to_team(question_set=template, team=team)

        self.assertEqual(result.status, CommonQuestionSetDeployment.Status.CREATED)
        self.assertEqual(SurveyForm.objects.filter(team=team, day_no=1).count(), 2)
        existing_form.refresh_from_db()
        self.assertFalse(existing_form.is_active)
        target_form = SurveyForm.objects.get(team=team, day_no=1, is_active=True)
        self.assertNotEqual(target_form.id, existing_form.id)
        self.assertEqual(target_form.version, 2)
        deployed_keys = set(
            SurveyQuestion.objects.filter(
                section__form=target_form
            ).values_list('key', flat=True)
        )
        self.assertNotIn('q_team', deployed_keys)
        self.assertIn('q_common', deployed_keys)
        self.assertTrue(
            SurveyQuestion.objects.filter(
                section__form=existing_form,
                key='q_team',
            ).exists()
        )

    def test_common_question_deploy_copies_graph_label(self):
        team = Team.objects.create(name='그래프팀', code='GRAPH1', manager_entry_code='GRAPH-MGR', start_date=date.today(), max_day_no=3, is_active=True)
        template = CommonQuestionSet.objects.create(name='그래프세트', title='공통 1일차', day_no=1, is_active=True)
        section = CommonQuestionSection.objects.create(question_set=template, title='신체', section_type=SurveySection.SectionType.PHYSICAL, display_order=1)
        CommonQuestion.objects.create(
            section=section,
            key='q_graph',
            label='현재 느껴지는 통증이 있나요?',
            graph_label='통증',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=[{'label': '없음', 'score': 1}, {'label': '심함', 'score': 5}],
            is_required=True,
            display_order=1,
        )

        admin_obj = CommonQuestionSetAdmin(CommonQuestionSet, AdminSite())
        result = admin_obj._deploy_to_team(question_set=template, team=team)
        self.assertEqual(result.status, CommonQuestionSetDeployment.Status.CREATED)

        deployed_question = SurveyQuestion.objects.get(section__form__team=team, key='q_graph')
        self.assertEqual(deployed_question.graph_label, '통증')


class StatusResultPayloadTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(
            name='상태테스트팀',
            code='STATUS1',
            manager_entry_code='STATUS-MGR',
            is_active=True,
            start_date=date.today(),
            max_day_no=4,
        )
        self.form = SurveyForm.objects.create(
            team=self.team,
            day_no=1,
            title='1일차',
            version=1,
            schema_json={},
            is_active=True,
        )
        self.physical = SurveySection.objects.create(
            form=self.form,
            title='신체',
            section_type=SurveySection.SectionType.PHYSICAL,
            display_order=1,
        )
        self.mental = SurveySection.objects.create(
            form=self.form,
            title='정신',
            section_type=SurveySection.SectionType.MENTAL,
            display_order=2,
        )
        self.q_body = SurveyQuestion.objects.create(
            section=self.physical,
            key='body_q1',
            label='현재 느껴지는 통증은 어떤가요?',
            graph_label='통증',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=[{'label': '없음', 'score': 1}, {'label': '보통', 'score': 3}, {'label': '심함', 'score': 5}],
            is_required=True,
            display_order=1,
        )
        self.q_emotion = SurveyQuestion.objects.create(
            section=self.mental,
            key='emotion_q1',
            label='오늘 긴장감은 어떤가요?',
            graph_label='긴장감',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=[{'label': '편안함', 'score': 1}, {'label': '보통', 'score': 3}, {'label': '높음', 'score': 5}],
            is_required=True,
            display_order=1,
        )
        Participant.objects.create(
            team=self.team,
            name='김정달',
            gender='M',
            birth_date='1991-07-22',
            phone='01011112222',
            normalized_name='김정달',
            normalized_phone='01011112222',
            is_active=True,
        )

    def _login_and_identify(self):
        # v1 로그인 화면은 폐기됨. v2 본인확인을 마친 상태와 같은 세션을 만든다.
        respondent, _ = Respondent.objects.get_or_create(
            team=self.team,
            normalized_name='김정달',
            birth_date=date(1991, 7, 22),
            gender='M',
            defaults={'name': '김정달'},
        )
        session = self.client.session
        session['survey_team_id'] = self.team.id
        session['survey_team_code'] = self.team.code
        session['survey_respondent_id'] = respondent.id
        session['survey_day_no'] = 1
        session.save()
        return respondent

class V2ExpansionModelTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(
            name='v2팀',
            code='V2TEAM',
            manager_entry_code='V2TEAM-ADMIN',
            is_active=True,
            start_date=date.today(),
            max_day_no=3,
        )
        self.respondent = Respondent.objects.create(
            team=self.team,
            name='홍길동',
            birth_date='1990-01-01',
            phone='01012345678',
            normalized_name='홍길동',
            normalized_phone='01012345678',
        )
        self.form = SurveyForm.objects.create(
            team=self.team,
            day_no=1,
            title='설문',
            version=1,
            form_type=SurveyForm.FormType.EXIT_ANONYMOUS,
            is_anonymous=True,
            is_active=True,
        )

    def test_document_template_hash_and_submission_snapshot_fields(self):
        template = DocumentTemplate.objects.create(
            team=self.team,
            document_type=DocumentTemplate.DocumentType.PRIVACY_CONSENT,
            title='개인정보동의',
            content_html='<p>동의합니다.</p>',
            version=1,
        )
        self.assertEqual(len(template.body_hash), 64)
        submission = DocumentSubmission.objects.create(
            team=self.team,
            respondent=self.respondent,
            template=template,
            document_type=template.document_type,
            template_version=template.version,
            template_hash_at_submit=template.body_hash,
            agreed=True,
            signer_name='홍길동',
            agreed_at=timezone.now(),
        )
        self.assertEqual(submission.template_hash_at_submit, template.body_hash)

    def test_required_document_feature_settings_and_reflection_file_metadata(self):
        settings = TeamFeatureSetting.objects.create(
            team=self.team,
            enable_privacy_consent=True,
            enable_safety_pledge=True,
        )
        self.assertTrue(settings.enable_privacy_consent)
        reflection = ReflectionSubmission.objects.create(
            team=self.team,
            respondent=self.respondent,
            content='좋았습니다.',
            file_path='reflections/v2team/hong.pdf',
            file_name='hong.pdf',
            file_size=1234,
            file_hash='a' * 64,
            generated_at=timezone.now(),
            submitted_at=timezone.now(),
        )
        self.assertEqual(reflection.file_name, 'hong.pdf')
        self.assertEqual(reflection.file_size, 1234)

    def test_anonymous_submission_model_has_no_identity_fields(self):
        field_names = {field.name for field in AnonymousSurveySubmission._meta.fields}
        forbidden = {'respondent', 'name', 'birth_date', 'phone', 'ip_address', 'user_agent', 'session_key'}
        self.assertTrue(forbidden.isdisjoint(field_names))
        submission = AnonymousSurveySubmission.objects.create(
            team=self.team,
            survey_form=self.form,
            form_version=self.form.version,
            form_snapshot_json={'sections': []},
            answers_json={'q1': '만족'},
            submitted_at=timezone.now(),
        )
        self.assertEqual(submission.team, self.team)
        self.assertEqual(submission.answers_json['q1'], '만족')

    def test_v2_code_entry_renders_and_legacy_v1_urls_are_closed(self):
        preview_response = self.client.get(reverse('surveys:preview-v2-code'))
        self.assertEqual(preview_response.status_code, 200)
        self.assertContains(preview_response, 'Preview · 기존 public URL 미전환')
        self.assertContains(preview_response, 'name="team_code"')
        self.assertContains(preview_response, 'method="post"')
        self.assertContains(preview_response, reverse('surveys:preview-v2-code'))
        self.assertContains(preview_response, 'csrfmiddlewaretoken')
        self.assertContains(preview_response, 'surveys/icons/v2-code-lock.svg')
        self.assertContains(preview_response, 'v2-mobile-page v2-code-entry')
        self.assertContains(preview_response, 'v2-field-control v2-code-input-wrap')
        self.assertContains(preview_response, 'v2-primary-button')
        self.assertContains(preview_response, 'v2-language-switch--code')
        self.assertContains(preview_response, 'surveys/motion.css')
        self.assertContains(preview_response, '한국어')
        self.assertContains(preview_response, 'English')
        root_response = self.client.get(reverse('surveys:entry'))
        self.assertEqual(root_response.status_code, 302)
        self.assertEqual(root_response.url, reverse('surveys:preview-v2-code'))
        for legacy_path in ('/surveys/respondent/', '/surveys/status/', '/surveys/day/1/submitted/',
                            '/surveys/prototype/login/', '/surveys/prototype/main/', '/api/public/healthcheck/team/verify/'):
            with self.subTest(path=legacy_path):
                self.assertEqual(self.client.get(legacy_path).status_code, 404)

    def test_preview_v2_code_language_switch_renders_english_copy(self):
        response = self.client.get(
            reverse('surveys:preview-v2-code'),
            {'lang': 'en'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Tour Program Platform')
        self.assertContains(response, 'Access Code')
        self.assertEqual(self.client.session['v2_public_language'], 'en')

    def test_preview_v2_identity_renders_distinct_english_gender_choices(self):
        session = self.client.session
        session['survey_team_id'] = self.team.id
        session['survey_team_code'] = self.team.code
        session['v2_public_language'] = 'en'
        session.save()

        response = self.client.get(reverse('surveys:preview-v2-identity'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<option value="M">Male</option>', html=True)
        self.assertContains(response, '<option value="F">Female</option>', html=True)
        self.assertEqual(response.content.decode().count('>Male</option>'), 1)
        self.assertEqual(response.content.decode().count('>Female</option>'), 1)

    def test_preview_v2_code_keeps_required_and_invalid_code_errors(self):
        required_response = self.client.post(reverse('surveys:preview-v2-code'), {'team_code': ''})
        self.assertEqual(required_response.status_code, 200)
        self.assertContains(required_response, '필수 항목입니다.')

        invalid_response = self.client.post(reverse('surveys:preview-v2-code'), {'team_code': 'NOT-A-TEAM'})
        self.assertEqual(invalid_response.status_code, 200)
        self.assertContains(invalid_response, '유효하지 않은 팀 코드 또는 관리자 코드입니다.')

    def test_preview_v2_manager_code_redirects_to_scoped_manager_login(self):
        response = self.client.post(
            reverse('surveys:preview-v2-code'),
            {'team_code': self.team.manager_entry_code},
        )
        self.assertRedirects(response, reverse('admins:login'), fetch_redirect_response=False)
        self.assertEqual(self.client.session.get('ops_login_team_id'), self.team.id)

    def test_preview_v2_manager_code_clears_previous_verified_team(self):
        session = self.client.session
        session['ops_login_verified_team_id'] = 999
        session.save()

        response = self.client.post(
            reverse('surveys:preview-v2-code'),
            {'team_code': self.team.manager_entry_code},
        )
        self.assertRedirects(response, reverse('admins:login'), fetch_redirect_response=False)
        self.assertIsNone(self.client.session.get('ops_login_verified_team_id'))

    def test_preview_v2_code_post_remains_csrf_protected(self):
        csrf_client = Client(enforce_csrf_checks=True)
        response = csrf_client.post(reverse('surveys:preview-v2-code'), {'team_code': 'ANY'})
        self.assertEqual(response.status_code, 403)



class CommonQuestionSetTargetDeployAdminTest(TestCase):
    def setUp(self):
        cache.clear()
        self.site = AdminSite()
        self.admin = CommonQuestionSetAdmin(CommonQuestionSet, self.site)
        self.question_set = CommonQuestionSet.objects.create(
            name='base-health',
            title='기본 자가검진',
            day_no=1,
            is_active=True,
        )
        self.common_section = CommonQuestionSection.objects.create(
            question_set=self.question_set,
            title='신체',
            section_type=SurveySection.SectionType.PHYSICAL,
            display_order=1,
        )
        self.common_question = CommonQuestion.objects.create(
            section=self.common_section,
            key='headache',
            label='두통이 있습니까?',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=[{'label': '없음', 'score': 1}, {'label': '있음', 'score': 3}],
            is_required=True,
            display_order=1,
        )
        self.target_team = Team.objects.create(
            name='대상팀',
            code='TARGET',
            is_active=True,
            start_date=date.today(),
            max_day_no=2,
        )
        self.other_team = Team.objects.create(
            name='미선택팀',
            code='OTHER',
            is_active=True,
            start_date=date.today(),
            max_day_no=2,
        )

    def test_deploy_to_selected_team_creates_form_questions_and_history_only_for_target(self):
        result = deploy_common_question_set_to_team(
            question_set=self.question_set,
            team=self.target_team,
        )

        self.assertEqual(result.status, CommonQuestionSetDeployment.Status.CREATED)
        self.assertEqual(result.team, self.target_team)
        self.assertIsNotNone(result.survey_form)
        self.assertEqual(result.survey_form.form_type, SurveyForm.FormType.DAILY_HEALTH)
        self.assertFalse(result.survey_form.is_anonymous)
        self.assertTrue(
            SurveyQuestion.objects.filter(
                section__form=result.survey_form,
                key='headache',
                label='두통이 있습니까?',
            ).exists()
        )
        self.assertFalse(SurveyForm.objects.filter(team=self.other_team, day_no=1).exists())

    def test_deploy_service_processes_only_explicit_team_collection(self):
        results = deploy_common_question_set_to_teams(
            question_set=self.question_set,
            teams=[self.target_team],
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].team, self.target_team)
        self.assertFalse(
            CommonQuestionSetDeployment.objects.filter(team=self.other_team).exists()
        )

    def test_exit_question_set_deploys_as_identified_exit_survey(self):
        exit_question_set = CommonQuestionSet.objects.create(
            name='exit-survey',
            title='탐방 종료 설문',
            day_no=1,
            form_type=CommonQuestionSet.FormType.EXIT_SURVEY,
            is_active=True,
        )
        exit_section = CommonQuestionSection.objects.create(
            question_set=exit_question_set,
            title='탐방 만족도',
            section_type=SurveySection.SectionType.SURVEY,
            display_order=1,
        )
        CommonQuestion.objects.create(
            section=exit_section,
            key='satisfaction',
            label='탐방에 만족하셨습니까?',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=[
                {'label': '만족', 'score': 5},
                {'label': '보통', 'score': 3},
            ],
            is_required=True,
            display_order=1,
        )

        result = deploy_common_question_set_to_team(
            question_set=exit_question_set,
            team=self.target_team,
        )

        self.assertEqual(result.status, CommonQuestionSetDeployment.Status.CREATED)
        self.assertEqual(result.survey_form.form_type, SurveyForm.FormType.EXIT_SURVEY)
        self.assertFalse(result.survey_form.is_anonymous)
        self.assertEqual(
            result.survey_form.sections.get().section_type,
            SurveySection.SectionType.SURVEY,
        )
        self.assertTrue(
            SurveyQuestion.objects.filter(
                section__form=result.survey_form,
                key='satisfaction',
            ).exists()
        )
        self.assertIsNone(
            get_active_survey_form(team=self.target_team, day_no=1)
        )
        self.assertNotIn(
            1,
            get_active_form_day_numbers(team=self.target_team),
        )

    def test_exit_question_set_ignores_team_daily_max_day_limit(self):
        exit_question_set = CommonQuestionSet.objects.create(
            name='exit-after-last-day',
            title='탐방 종료 설문',
            day_no=self.target_team.max_day_no + 1,
            form_type=CommonQuestionSet.FormType.EXIT_SURVEY,
            is_active=True,
        )
        exit_section = CommonQuestionSection.objects.create(
            question_set=exit_question_set,
            title='탐방 만족도',
            section_type=SurveySection.SectionType.SURVEY,
            display_order=1,
        )
        CommonQuestion.objects.create(
            section=exit_section,
            key='overall_satisfaction',
            label='전체 일정에 만족하셨습니까?',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=[{'label': '만족', 'score': 5}],
            is_required=True,
            display_order=1,
        )

        result = deploy_common_question_set_to_team(
            question_set=exit_question_set,
            team=self.target_team,
        )

        self.assertEqual(result.status, CommonQuestionSetDeployment.Status.CREATED)
        self.assertEqual(result.survey_form.form_type, SurveyForm.FormType.EXIT_SURVEY)
        self.assertEqual(result.survey_form.day_no, self.target_team.max_day_no + 1)

    def test_daily_question_set_still_respects_team_max_day_limit(self):
        self.question_set.day_no = self.target_team.max_day_no + 1
        self.question_set.save(update_fields=['day_no', 'updated_at'])

        result = deploy_common_question_set_to_team(
            question_set=self.question_set,
            team=self.target_team,
        )

        self.assertEqual(result.status, CommonQuestionSetDeployment.Status.SKIPPED)
        self.assertIn('max_day_no', result.message)
        self.assertIsNone(result.survey_form)

    def test_deploy_invalidates_cached_missing_daily_form(self):
        self.assertIsNone(
            get_active_survey_form(team=self.target_team, day_no=1)
        )
        self.assertNotIn(
            1,
            get_active_form_day_numbers(team=self.target_team),
        )

        result = deploy_common_question_set_to_team(
            question_set=self.question_set,
            team=self.target_team,
        )

        self.assertEqual(
            get_active_survey_form(team=self.target_team, day_no=1),
            result.survey_form,
        )
        self.assertIn(
            1,
            get_active_form_day_numbers(team=self.target_team),
        )

    def test_deploy_skips_question_set_without_questions(self):
        self.common_question.delete()

        result = deploy_common_question_set_to_team(
            question_set=self.question_set,
            team=self.target_team,
        )

        self.assertEqual(result.status, CommonQuestionSetDeployment.Status.SKIPPED)
        self.assertEqual(result.message, '문항이 없어 배포하지 않음')
        self.assertIsNone(result.survey_form)
        self.assertFalse(SurveyForm.objects.filter(team=self.target_team, day_no=1).exists())

    def test_deploy_skips_when_team_exclusions_remove_every_question(self):
        TeamCommonQuestionExclusion.objects.create(
            team=self.target_team,
            question_set=self.question_set,
            question_key='headache',
        )

        result = deploy_common_question_set_to_team(
            question_set=self.question_set,
            team=self.target_team,
        )

        self.assertEqual(result.status, CommonQuestionSetDeployment.Status.SKIPPED)
        self.assertEqual(result.message, '문항이 없어 배포하지 않음')
        self.assertFalse(SurveyForm.objects.filter(team=self.target_team, day_no=1).exists())

    @patch('surveys.admin.deploy_common_question_set_to_teams')
    def test_admin_save_related_does_not_deploy_question_set_without_questions(self, deploy_mock):
        self.common_question.delete()
        request = RequestFactory().post('/admin/surveys/commonquestionset/1/change/')
        request.user = get_user_model().objects.create_superuser(
            username='empty-set-admin',
            email='empty@example.com',
            password='pass',
        )
        request.session = {}
        request._messages = FallbackStorage(request)

        class FormStub:
            instance = self.question_set
            cleaned_data = {'target_teams': [self.target_team], 'deploy_after_save': True}

            def save_m2m(self):
                return None

        self.admin.save_related(request, FormStub(), [], True)

        deploy_mock.assert_not_called()
        self.assertEqual(
            [str(message) for message in request._messages],
            ['문항이 없어 배포하지 않았습니다. 문항을 추가한 뒤 다시 배포하세요.'],
        )

    def test_missing_daily_form_lookup_is_not_cached(self):
        self.assertIsNone(get_active_survey_form(team=self.target_team, day_no=1))

        # 이 워커의 캐시를 무효화하지 않고 폼이 생겨도(다른 워커가 배포한 경우)
        # 다음 조회가 바로 찾아야 한다.
        form = SurveyForm.objects.create(
            team=self.target_team,
            day_no=1,
            title='1일차',
            version=1,
            is_active=True,
        )

        self.assertEqual(get_active_survey_form(team=self.target_team, day_no=1), form)

    def test_found_daily_form_lookup_is_cached(self):
        form = SurveyForm.objects.create(
            team=self.target_team,
            day_no=1,
            title='1일차',
            version=1,
            is_active=True,
        )
        self.assertEqual(get_active_survey_form(team=self.target_team, day_no=1), form)

        with self.assertNumQueries(0):
            self.assertEqual(get_active_survey_form(team=self.target_team, day_no=1), form)

    @patch('surveys.admin.deploy_common_question_set_to_teams')
    def test_admin_delegates_selected_team_deployment_to_service(self, deploy_mock):
        deploy_mock.return_value = []

        result = self.admin.deploy_to_selected_teams(
            question_set=self.question_set,
            teams=[self.target_team],
        )

        self.assertEqual(result, [])
        deploy_mock.assert_called_once_with(
            question_set=self.question_set,
            teams=[self.target_team],
        )

    def test_deploy_creates_new_version_for_changed_existing_form(self):
        existing_form = SurveyForm.objects.create(
            team=self.target_team,
            day_no=1,
            title='기존 설문',
            version=1,
            form_type=SurveyForm.FormType.DAILY_HEALTH,
            is_active=True,
        )

        result = self.admin._deploy_to_team(question_set=self.question_set, team=self.target_team)

        self.assertEqual(result.status, CommonQuestionSetDeployment.Status.CREATED)
        self.assertNotEqual(result.survey_form, existing_form)
        self.assertEqual(result.survey_form.version, 2)
        self.assertIn('새 버전', result.message)
        existing_form.refresh_from_db()
        self.assertFalse(existing_form.is_active)
        self.assertEqual(
            SurveyForm.objects.filter(
                team=self.target_team,
                day_no=1,
                form_type=SurveyForm.FormType.DAILY_HEALTH,
            ).count(),
            2,
        )

    def test_deploy_skips_when_active_form_matches_common_set(self):
        first = self.admin._deploy_to_team(
            question_set=self.question_set,
            team=self.target_team,
        )

        second = self.admin._deploy_to_team(
            question_set=self.question_set,
            team=self.target_team,
        )

        self.assertEqual(first.status, CommonQuestionSetDeployment.Status.CREATED)
        self.assertEqual(second.status, CommonQuestionSetDeployment.Status.SKIPPED)
        self.assertEqual(second.survey_form, first.survey_form)
        self.assertIn('변경 없음', second.message)
        self.assertEqual(
            SurveyForm.objects.filter(
                team=self.target_team,
                day_no=1,
                form_type=SurveyForm.FormType.DAILY_HEALTH,
            ).count(),
            1,
        )

    def test_redeploy_updates_korean_and_english_as_new_version(self):
        self.question_set.title = '갱신된 건강검진'
        self.question_set.title_en = 'Updated Health Check'
        self.question_set.save(
            update_fields=['title', 'title_en', 'updated_at']
        )
        self.common_section.title = '갱신된 신체'
        self.common_section.title_en = 'Updated Body'
        self.common_section.save(
            update_fields=['title', 'title_en', 'updated_at']
        )
        self.common_question.label_en = 'Do you have a headache?'
        self.common_question.options_json = [
            {'label': '전혀 없음', 'score': 5, 'label_en': 'Not at all|5'},
            {'label': '심함', 'score': 1, 'label_en': 'Severe|1'},
        ]
        self.common_question.save(
            update_fields=['label_en', 'options_json', 'updated_at']
        )
        first = self.admin._deploy_to_team(
            question_set=self.question_set,
            team=self.target_team,
        )
        self.common_question.label = '현재 두통이 있습니까?'
        self.common_question.label_en = 'Do you currently have a headache?'
        self.common_question.save(
            update_fields=['label', 'label_en', 'updated_at']
        )

        second = self.admin._deploy_to_team(
            question_set=self.question_set,
            team=self.target_team,
        )

        self.assertEqual(second.status, CommonQuestionSetDeployment.Status.CREATED)
        self.assertEqual(second.survey_form.version, first.survey_form.version + 1)
        first.survey_form.refresh_from_db()
        self.assertFalse(first.survey_form.is_active)
        self.assertTrue(second.survey_form.is_active)
        deployed_section = second.survey_form.sections.get()
        deployed_question = deployed_section.questions.get()
        self.assertEqual(second.survey_form.title, '갱신된 건강검진')
        self.assertEqual(second.survey_form.title_en, 'Updated Health Check')
        self.assertEqual(deployed_section.title, '갱신된 신체')
        self.assertEqual(deployed_section.title_en, 'Updated Body')
        self.assertEqual(deployed_question.label, '현재 두통이 있습니까?')
        self.assertEqual(
            deployed_question.label_en,
            'Do you currently have a headache?',
        )
        self.assertEqual(
            deployed_question.options_json,
            [
                {'label': '전혀 없음', 'label_en': 'Not at all', 'score': 5},
                {'label': '심함', 'label_en': 'Severe', 'score': 1},
            ],
        )
        self.assertTrue(
            SurveyQuestion.objects.filter(
                section__form=first.survey_form,
                label='두통이 있습니까?',
            ).exists()
        )

    def test_failed_version_redeploy_keeps_previous_form_active(self):
        first = self.admin._deploy_to_team(
            question_set=self.question_set,
            team=self.target_team,
        )
        self.common_question.label = '변경된 문항'
        self.common_question.save(update_fields=['label', 'updated_at'])

        with (
            patch(
                'surveys.survey_deployment.SurveyQuestion.objects.create',
                side_effect=RuntimeError('문항 생성 실패'),
            ),
            self.assertRaises(RuntimeError),
        ):
            self.admin._deploy_to_team(
                question_set=self.question_set,
                team=self.target_team,
            )

        first.survey_form.refresh_from_db()
        self.assertTrue(first.survey_form.is_active)
        self.assertEqual(
            SurveyForm.objects.filter(
                team=self.target_team,
                day_no=1,
                form_type=SurveyForm.FormType.DAILY_HEALTH,
            ).count(),
            1,
        )

    def test_existing_question_set_form_integrates_explicit_target_team_selection(self):
        form = CommonQuestionSetAdminForm(
            instance=self.question_set,
            data={
                'name': self.question_set.name,
                'title': self.question_set.title,
                'form_type': self.question_set.form_type,
                'day_no': self.question_set.day_no,
                'is_active': 'on',
                'description': '',
                'target_teams': [self.target_team.pk],
                'deploy_after_save': 'on',
            },
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            list(form.cleaned_data['target_teams']),
            [self.target_team],
        )
        self.assertTrue(form.cleaned_data['deploy_after_save'])
        self.assertFalse(form.fields['target_teams'].disabled)
        self.assertIn(
            'target_teams',
            self.admin.fieldsets[-1][1]['fields'],
        )
        self.assertNotIn('deploy_link', self.admin.list_display)

    def test_integrated_deploy_requires_at_least_one_target_team(self):
        form = CommonQuestionSetAdminForm(
            instance=self.question_set,
            data={
                'name': self.question_set.name,
                'title': self.question_set.title,
                'form_type': self.question_set.form_type,
                'day_no': self.question_set.day_no,
                'is_active': 'on',
                'description': '',
                'deploy_after_save': 'on',
            },
        )

        self.assertFalse(form.is_valid())
        self.assertIn('target_teams', form.errors)

    def test_new_question_set_disables_deploy_until_first_save(self):
        form = CommonQuestionSetAdminForm()

        self.assertTrue(form.fields['target_teams'].disabled)
        self.assertTrue(form.fields['deploy_after_save'].disabled)
        self.assertIn('먼저 저장', form.fields['target_teams'].help_text)

    def test_save_related_deploys_after_inline_save_for_selected_teams(self):
        form = CommonQuestionSetAdminForm(
            instance=self.question_set,
            data={
                'name': self.question_set.name,
                'title': self.question_set.title,
                'form_type': self.question_set.form_type,
                'day_no': self.question_set.day_no,
                'is_active': 'on',
                'description': '',
                'target_teams': [self.target_team.pk],
                'deploy_after_save': 'on',
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.instance = form.save(commit=False)
        form.instance.save()
        request = RequestFactory().post('/')
        request.session = {}
        request._messages = FallbackStorage(request)

        with patch.object(
            self.admin,
            'deploy_to_selected_teams',
            return_value=[],
        ) as deploy_mock:
            self.admin.save_related(request, form, [], change=True)

        deploy_mock.assert_called_once()
        call_kwargs = deploy_mock.call_args.kwargs
        self.assertEqual(call_kwargs['question_set'], self.question_set)
        self.assertEqual(list(call_kwargs['teams']), [self.target_team])


class CommonQuestionSetCloneTest(TestCase):
    def setUp(self):
        cache.clear()
        self.source = CommonQuestionSet.objects.create(
            name='건강_1일차_공통',
            title='자가건강검진',
            title_en='Daily Health Check',
            day_no=1,
            is_active=True,
            description='운영 메모',
        )
        self.physical = CommonQuestionSection.objects.create(
            question_set=self.source,
            title='신체',
            title_en='Body',
            section_type=SurveySection.SectionType.PHYSICAL,
            display_order=1,
        )
        self.mental = CommonQuestionSection.objects.create(
            question_set=self.source,
            title='정신',
            title_en='Mind',
            section_type=SurveySection.SectionType.MENTAL,
            display_order=2,
        )
        self.headache_options = [
            {'label': '없음', 'label_en': 'None', 'score': 1},
            {'label': '있음', 'label_en': 'Yes', 'score': 3},
        ]
        CommonQuestion.objects.create(
            section=self.physical,
            key='headache',
            label='두통이 있습니까?',
            label_en='Do you have a headache?',
            graph_label='두통',
            graph_label_en='Headache',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=self.headache_options,
            is_required=True,
            display_order=1,
        )
        CommonQuestion.objects.create(
            section=self.mental,
            key='mood_note',
            label='기분을 적어주세요',
            question_type=SurveyQuestion.QuestionType.TEXT,
            options_json=[],
            is_required=False,
            display_order=1,
        )
        self.team = Team.objects.create(
            name='복제팀',
            code='CLONE',
            is_active=True,
            start_date=date.today(),
            max_day_no=4,
        )
        TeamCommonQuestionExclusion.objects.create(
            team=self.team,
            question_set=self.source,
            question_key='mood_note',
        )

    def test_clone_copies_sections_questions_and_exclusions_to_target_day(self):
        clone = clone_common_question_set(source=self.source, day_no=3)

        self.assertNotEqual(clone.pk, self.source.pk)
        self.assertEqual(clone.name, '건강_3일차_공통')
        self.assertEqual(clone.day_no, 3)
        self.assertEqual(clone.title, '자가건강검진')
        self.assertEqual(clone.title_en, 'Daily Health Check')
        self.assertEqual(clone.form_type, CommonQuestionSet.FormType.DAILY_HEALTH)
        self.assertTrue(clone.is_active)
        self.assertEqual(clone.description, '운영 메모')

        sections = list(clone.sections.order_by('display_order', 'id'))
        self.assertEqual(
            [(s.title, s.title_en, s.section_type, s.display_order) for s in sections],
            [('신체', 'Body', 'physical', 1), ('정신', 'Mind', 'mental', 2)],
        )
        self.assertEqual(clone.questions.count(), 2)
        cloned_question = clone.questions.get(key='headache')
        self.assertEqual(cloned_question.section, sections[0])
        self.assertEqual(cloned_question.question_set, clone)
        self.assertEqual(cloned_question.label, '두통이 있습니까?')
        self.assertEqual(cloned_question.label_en, 'Do you have a headache?')
        self.assertEqual(cloned_question.graph_label, '두통')
        self.assertEqual(cloned_question.graph_label_en, 'Headache')
        self.assertEqual(cloned_question.question_type, SurveyQuestion.QuestionType.SINGLE)
        self.assertEqual(cloned_question.options_json, self.headache_options)
        self.assertTrue(cloned_question.is_required)
        self.assertEqual(
            list(clone.team_exclusions.values_list('team_id', 'question_key')),
            [(self.team.id, 'mood_note')],
        )

        self.source.refresh_from_db()
        self.assertEqual(self.source.day_no, 1)
        self.assertEqual(self.source.sections.count(), 2)
        self.assertEqual(self.source.questions.count(), 2)
        self.assertEqual(self.source.team_exclusions.count(), 1)

    def test_clone_is_independent_from_source(self):
        clone = clone_common_question_set(source=self.source, day_no=2)

        source_question = self.source.questions.get(key='headache')
        source_question.options_json.append({'label': '심함', 'label_en': 'Severe', 'score': 5})
        source_question.label = '변경된 문항'
        source_question.save()
        self.source.questions.get(key='mood_note').delete()

        cloned_question = clone.questions.get(key='headache')
        self.assertEqual(cloned_question.label, '두통이 있습니까?')
        self.assertEqual(len(cloned_question.options_json), 2)
        self.assertEqual(clone.questions.count(), 2)

    def test_clone_name_replaces_day_label_or_appends_suffix_and_stays_unique(self):
        self.assertEqual(
            build_cloned_question_set_name(source_name='건강_1일차_공통', day_no=4),
            '건강_4일차_공통',
        )
        self.assertEqual(
            build_cloned_question_set_name(source_name='설문_해양재단', day_no=2),
            '설문_해양재단 (2일차)',
        )
        max_length = CommonQuestionSet._meta.get_field('name').max_length
        self.assertLessEqual(
            len(build_cloned_question_set_name(source_name='가' * max_length, day_no=2)),
            max_length,
        )

        first = clone_common_question_set(source=self.source, day_no=3)
        second = clone_common_question_set(source=self.source, day_no=3)
        self.assertEqual(first.name, '건강_3일차_공통')
        self.assertEqual(second.name, '건강_3일차_공통 (2)')

    def test_clone_rejects_day_below_one(self):
        with self.assertRaises(ValueError):
            clone_common_question_set(source=self.source, day_no=0)
        self.assertEqual(CommonQuestionSet.objects.count(), 1)

    def test_clone_to_multiple_days_creates_one_set_per_day_or_nothing(self):
        clones = clone_common_question_set_to_days(source=self.source, day_numbers=[2, 3, 4])

        self.assertEqual([clone.day_no for clone in clones], [2, 3, 4])
        self.assertEqual(
            [clone.name for clone in clones],
            ['건강_2일차_공통', '건강_3일차_공통', '건강_4일차_공통'],
        )
        self.assertEqual(CommonQuestionSet.objects.count(), 4)
        for clone in clones:
            self.assertEqual(clone.questions.count(), 2)

        with self.assertRaises(ValueError):
            clone_common_question_set_to_days(source=self.source, day_numbers=[5, 0])
        self.assertFalse(CommonQuestionSet.objects.filter(day_no=5).exists())
        self.assertEqual(CommonQuestionSet.objects.count(), 4)

    def test_deploying_clone_creates_form_for_clone_day_with_copied_questions(self):
        clone = clone_common_question_set(source=self.source, day_no=3)

        result = deploy_common_question_set_to_team(question_set=clone, team=self.team)

        self.assertEqual(result.status, CommonQuestionSetDeployment.Status.CREATED)
        self.assertEqual(result.survey_form.day_no, 3)
        self.assertEqual(result.survey_form.form_type, SurveyForm.FormType.DAILY_HEALTH)
        self.assertEqual(
            list(
                SurveyQuestion.objects.filter(section__form=result.survey_form)
                .values_list('key', flat=True)
            ),
            ['headache'],
        )
        self.assertEqual(get_active_survey_form(team=self.team, day_no=3), result.survey_form)
        self.assertFalse(SurveyForm.objects.filter(team=self.team, day_no=1).exists())

    def test_clone_form_parses_days_and_rejects_invalid_input(self):
        form = CommonQuestionSetCloneForm({'target_days': '2, 3,4 3'})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['target_days'], [2, 3, 4])

        for raw in ('', '   ', '0', 'a', '2,-1', '2.5'):
            with self.subTest(raw=raw):
                self.assertFalse(CommonQuestionSetCloneForm({'target_days': raw}).is_valid())

    def _login_superuser(self):
        superuser = get_user_model().objects.create_superuser(
            username='clone-admin',
            email='clone@example.com',
            password='pass',
        )
        self.client.force_login(superuser)

    def test_change_form_links_to_clone_view(self):
        self._login_superuser()

        response = self.client.get(
            reverse('admin:surveys_commonquestionset_change', args=[self.source.pk])
        )

        self.assertContains(
            response,
            reverse('admin:surveys_commonquestionset_clone', args=[self.source.pk]),
        )
        self.assertContains(response, '다른 일차로 복제')

    def test_admin_clone_view_creates_sets_and_links_them(self):
        self._login_superuser()
        clone_url = reverse('admin:surveys_commonquestionset_clone', args=[self.source.pk])

        get_response = self.client.get(clone_url)
        self.assertEqual(get_response.status_code, 200)
        self.assertContains(get_response, '건강_1일차_공통')
        self.assertContains(get_response, '섹션 2개, 문항 2개, 팀별 제외 문항 1개')

        response = self.client.post(clone_url, {'target_days': '2,3,4'})

        self.assertRedirects(
            response,
            reverse('admin:surveys_commonquestionset_changelist'),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            list(CommonQuestionSet.objects.order_by('day_no').values_list('name', 'day_no')),
            [
                ('건강_1일차_공통', 1),
                ('건강_2일차_공통', 2),
                ('건강_3일차_공통', 3),
                ('건강_4일차_공통', 4),
            ],
        )
        for clone in CommonQuestionSet.objects.exclude(pk=self.source.pk):
            self.assertEqual(clone.sections.count(), 2)
            self.assertEqual(clone.questions.count(), 2)
            self.assertEqual(clone.team_exclusions.count(), 1)
            self.assertFalse(CommonQuestionSetDeployment.objects.filter(question_set=clone).exists())

        follow = self.client.get(response.url)
        self.assertContains(follow, '공통 설문 3개를 복제했습니다')
        day4 = CommonQuestionSet.objects.get(day_no=4)
        self.assertContains(
            follow,
            reverse('admin:surveys_commonquestionset_change', args=[day4.pk]),
        )

    def test_admin_clone_view_rejects_invalid_days_without_creating(self):
        self._login_superuser()
        clone_url = reverse('admin:surveys_commonquestionset_clone', args=[self.source.pk])

        response = self.client.post(clone_url, {'target_days': '0'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '일차는 1 이상의 정수로 입력하세요.')
        self.assertEqual(CommonQuestionSet.objects.count(), 1)

    def test_admin_clone_view_requires_add_permission(self):
        from django.contrib.auth.models import Permission

        viewer = get_user_model().objects.create_user(
            username='clone-viewer',
            password='pass',
            is_staff=True,
        )
        viewer.user_permissions.add(
            Permission.objects.get(
                content_type__app_label='surveys',
                codename='view_commonquestionset',
            )
        )
        self.client.force_login(viewer)
        clone_url = reverse('admin:surveys_commonquestionset_clone', args=[self.source.pk])

        self.assertEqual(self.client.get(clone_url).status_code, 403)
        self.assertEqual(self.client.post(clone_url, {'target_days': '2'}).status_code, 403)
        self.assertEqual(CommonQuestionSet.objects.count(), 1)

    def test_admin_clone_view_returns_404_for_unknown_set(self):
        self._login_superuser()

        response = self.client.get(
            reverse('admin:surveys_commonquestionset_clone', args=[self.source.pk + 999])
        )

        self.assertEqual(response.status_code, 404)


class IdentifiedExitSurveyFlowTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(
            name='탐방 종료 설문팀',
            code='EXIT01',
            is_active=True,
            start_date=date.today(),
            max_day_no=3,
        )
        self.respondent = Respondent.objects.create(
            team=self.team,
            name='설문참가자',
            birth_date=date(1995, 5, 5),
            phone='01055556666',
            normalized_name='설문참가자',
            normalized_phone='01055556666',
        )
        self.form = SurveyForm.objects.create(
            team=self.team,
            day_no=1,
            title='탐방 종료 설문',
            version=1,
            form_type=SurveyForm.FormType.EXIT_SURVEY,
            is_anonymous=False,
            is_active=True,
        )
        self.section = SurveySection.objects.create(
            form=self.form,
            title='탐방 만족도',
            section_type=SurveySection.SectionType.SURVEY,
            display_order=1,
        )
        self.question = SurveyQuestion.objects.create(
            section=self.section,
            key='exit_satisfaction',
            label='이번 탐방에 만족하셨습니까?',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=[
                {'label': '만족', 'score': 5},
                {'label': '보통', 'score': 3},
            ],
            is_required=True,
            display_order=1,
        )
        session = self.client.session
        session['survey_team_id'] = self.team.id
        session['survey_team_code'] = self.team.code
        session['survey_respondent_id'] = self.respondent.id
        session['survey_day_no'] = 1
        session.save()

    def test_home_links_figma_start_page_then_existing_survey_template(self):
        exit_url = reverse('surveys:exit-survey')
        start_url = reverse('surveys:preview-v2-exit-start')

        home_response = self.client.get(reverse('surveys:preview-v2-home'))
        start_response = self.client.get(start_url)
        survey_response = self.client.get(exit_url)

        self.assertEqual(home_response.status_code, 200)
        self.assertContains(
            home_response,
            f'data-card="exit-survey" href="{start_url}"',
            html=False,
        )
        self.assertEqual(start_response.status_code, 200)
        self.assertTemplateUsed(
            start_response,
            'surveys/public_v2/exit_survey_start.html',
        )
        self.assertContains(start_response, '설문지 작성을 시작합니다')
        self.assertContains(start_response, f'href="{exit_url}"')
        self.assertContains(
            start_response,
            'surveys/icons/v2-survey-start/clipboard.svg',
        )
        self.assertContains(
            start_response,
            'surveys/icons/v2-survey-start/check-pen.png',
        )
        self.assertEqual(survey_response.status_code, 200)
        self.assertTemplateUsed(survey_response, 'surveys/day_survey.html')
        self.assertContains(survey_response, self.question.label)
        self.assertContains(survey_response, '설문 영역')
        self.assertContains(survey_response, 'survey-page--survey')
        self.assertContains(
            survey_response,
            'surveys/icons/v2-questionnaire/clipboard.svg',
        )
        self.assertContains(survey_response, 'aria-label="이전 문항"')
        self.assertContains(survey_response, 'aria-label="다음 문항"')
        self.assertEqual(survey_response.context['section_key'], 'survey')

    def test_exit_survey_renders_deployed_english_question_and_option(self):
        self.section.title_en = 'Survey'
        self.section.save(update_fields=['title_en', 'updated_at'])
        self.question.label_en = 'Were you satisfied with this tour?'
        self.question.options_json = [
            {'label': '만족', 'score': 5, 'label_en': 'Satisfied|5'},
            {'label': '보통', 'score': 3, 'label_en': 'Neutral|3'},
        ]
        self.question.save(update_fields=['label_en', 'options_json', 'updated_at'])

        response = self.client.get(reverse('surveys:exit-survey') + '?lang=en')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Were you satisfied with this tour?')
        self.assertContains(response, '>Satisfied</span>', html=False)
        self.assertNotContains(response, '>Satisfied|5</span>', html=False)

    def test_start_page_redirects_completed_respondent_to_completion(self):
        ExitSurveySubmission.objects.create(
            team=self.team,
            respondent=self.respondent,
            survey_form=self.form,
            form_version=self.form.version,
            form_snapshot_json={'sections': []},
            answers_json={self.question.key: '만족'},
            is_locked=True,
            submitted_at=timezone.now(),
        )

        response = self.client.get(
            reverse('surveys:preview-v2-exit-start')
        )

        self.assertRedirects(
            response,
            reverse(
                'surveys:preview-v2-completion',
                kwargs={'completion_type': 'survey'},
            ),
        )

    def test_start_page_redirects_home_when_team_has_no_active_exit_form(self):
        self.form.is_active = False
        self.form.save(update_fields=['is_active', 'updated_at'])

        response = self.client.get(
            reverse('surveys:preview-v2-exit-start')
        )

        self.assertRedirects(
            response,
            reverse('surveys:preview-v2-home'),
        )

    def test_submit_stores_identified_answers_and_marks_home_complete(self):
        exit_url = reverse('surveys:exit-survey')
        get_response = self.client.get(exit_url)
        signature = get_response.context['survey_form_signature']

        response = self.client.post(
            exit_url,
            {
                'survey_form_id': str(self.form.id),
                'survey_form_version': str(self.form.version),
                'survey_form_signature': signature,
                'answers_payload': json.dumps(
                    {str(self.question.id): '만족'},
                    ensure_ascii=False,
                ),
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()['redirect_url'],
            reverse(
                'surveys:preview-v2-completion',
                kwargs={'completion_type': 'survey'},
            ),
        )
        submission = ExitSurveySubmission.objects.get()
        self.assertEqual(submission.team, self.team)
        self.assertEqual(submission.respondent, self.respondent)
        self.assertEqual(
            submission.answers_json,
            {'exit_satisfaction': '만족'},
        )
        self.assertEqual(submission.form_version, self.form.version)
        self.assertTrue(submission.is_locked)

        repeat_response = self.client.get(exit_url)
        self.assertRedirects(
            repeat_response,
            reverse(
                'surveys:preview-v2-completion',
                kwargs={'completion_type': 'survey'},
            ),
        )
        self.assertEqual(ExitSurveySubmission.objects.count(), 1)

        home_response = self.client.get(reverse('surveys:preview-v2-home'))
        self.assertContains(home_response, '제출 완료')
        self.assertContains(
            home_response,
            'surveys/icons/v2-home/check-survey.svg',
        )

    def test_submit_snapshot_preserves_korean_and_english_survey_content(self):
        self.form.title_en = 'Post-tour Survey'
        self.form.save(update_fields=['title_en', 'updated_at'])
        self.section.title_en = 'Satisfaction'
        self.section.save(update_fields=['title_en', 'updated_at'])
        self.question.label_en = 'Were you satisfied with this tour?'
        self.question.graph_label = '만족도'
        self.question.graph_label_en = 'Satisfaction'
        self.question.options_json = [
            {'label': '만족', 'label_en': 'Satisfied', 'score': 5},
            {'label': '보통', 'label_en': 'Neutral', 'score': 3},
        ]
        self.question.save(
            update_fields=[
                'label_en',
                'graph_label',
                'graph_label_en',
                'options_json',
                'updated_at',
            ]
        )
        exit_url = reverse('surveys:exit-survey')
        get_response = self.client.get(exit_url)

        response = self.client.post(
            exit_url,
            {
                'survey_form_id': str(self.form.id),
                'survey_form_version': str(self.form.version),
                'survey_form_signature': get_response.context[
                    'survey_form_signature'
                ],
                'answers_payload': json.dumps(
                    {str(self.question.id): '만족'},
                    ensure_ascii=False,
                ),
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        snapshot = ExitSurveySubmission.objects.get().form_snapshot_json
        self.assertEqual(snapshot['title'], '탐방 종료 설문')
        self.assertEqual(snapshot['title_en'], 'Post-tour Survey')
        snapshot_section = snapshot['sections'][0]
        self.assertEqual(snapshot_section['title'], '탐방 만족도')
        self.assertEqual(snapshot_section['title_en'], 'Satisfaction')
        snapshot_question = snapshot_section['questions'][0]
        self.assertEqual(
            snapshot_question['label'],
            '이번 탐방에 만족하셨습니까?',
        )
        self.assertEqual(
            snapshot_question['label_en'],
            'Were you satisfied with this tour?',
        )
        self.assertEqual(snapshot_question['graph_label'], '만족도')
        self.assertEqual(snapshot_question['graph_label_en'], 'Satisfaction')
        self.assertEqual(
            snapshot_question['options'],
            [
                {'label': '만족', 'label_en': 'Satisfied', 'score': 5},
                {'label': '보통', 'label_en': 'Neutral', 'score': 3},
            ],
        )

    def test_required_exit_question_is_validated_on_server(self):
        get_response = self.client.get(reverse('surveys:exit-survey'))

        response = self.client.post(
            reverse('surveys:exit-survey'),
            {
                'survey_form_id': str(self.form.id),
                'survey_form_version': str(self.form.version),
                'survey_form_signature': get_response.context[
                    'survey_form_signature'
                ],
                'answers_payload': '{}',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('필수 문항', response.json()['error'])
        self.assertFalse(ExitSurveySubmission.objects.exists())

    def test_submission_model_preserves_form_and_answer_snapshot(self):
        snapshot = {
            'sections': [
                {
                    'title': self.section.title,
                    'questions': [
                        {
                            'id': self.question.id,
                            'key': self.question.key,
                            'label': self.question.label,
                        },
                    ],
                },
            ],
        }
        submission = ExitSurveySubmission.objects.create(
            team=self.team,
            respondent=self.respondent,
            survey_form=self.form,
            form_version=self.form.version,
            form_snapshot_json=snapshot,
            answers_json={self.question.key: '만족'},
            is_locked=True,
            submitted_at=timezone.now(),
        )

        self.question.label = '변경된 현재 문항'
        self.question.save(update_fields=['label', 'updated_at'])
        submission.refresh_from_db()

        self.assertEqual(submission.form_version, 1)
        self.assertEqual(
            submission.form_snapshot_json['sections'][0]['questions'][0]['label'],
            '이번 탐방에 만족하셨습니까?',
        )
        self.assertEqual(
            submission.answers_json,
            {'exit_satisfaction': '만족'},
        )

    def test_submission_model_rejects_duplicate_respondent_form(self):
        submission_values = {
            'team': self.team,
            'respondent': self.respondent,
            'survey_form': self.form,
            'form_version': self.form.version,
            'form_snapshot_json': {'sections': []},
            'answers_json': {self.question.key: '만족'},
            'is_locked': True,
            'submitted_at': timezone.now(),
        }
        ExitSurveySubmission.objects.create(**submission_values)

        with self.assertRaises(IntegrityError), transaction.atomic():
            ExitSurveySubmission.objects.create(**submission_values)

        self.assertEqual(ExitSurveySubmission.objects.count(), 1)

    def test_submission_model_has_no_unapproved_tracking_fields(self):
        field_names = {
            field.name
            for field in ExitSurveySubmission._meta.get_fields()
        }

        self.assertTrue(
            {
                'team',
                'respondent',
                'survey_form',
                'form_version',
                'form_snapshot_json',
                'answers_json',
                'is_locked',
                'submitted_at',
            }.issubset(field_names)
        )
        self.assertTrue(
            {
                'ip_address',
                'user_agent',
                'session_key',
                'location',
            }.isdisjoint(field_names)
        )


class StaticFilesTestStorageSettingTest(TestCase):
    def test_test_environment_uses_non_manifest_staticfiles_storage(self):
        self.assertEqual(
            settings.STORAGES['staticfiles']['BACKEND'],
            'django.contrib.staticfiles.storage.StaticFilesStorage',
        )


class CacheSettingsTest(SimpleTestCase):
    def test_defaults_to_per_process_locmem_without_url(self):
        for cache_url in (None, '', '   '):
            with self.subTest(cache_url=cache_url):
                caches = build_caches(cache_url=cache_url, app_env='production')
                self.assertEqual(caches['default']['BACKEND'], LOCMEM_BACKEND)

    def test_redis_url_selects_shared_redis_backend(self):
        for cache_url in ('redis://red-abc:6379', 'rediss://:secret@cache.example:6380/1'):
            with self.subTest(cache_url=cache_url):
                caches = build_caches(cache_url=cache_url, app_env='production')
                self.assertEqual(caches['default']['BACKEND'], REDIS_BACKEND)
                self.assertEqual(caches['default']['LOCATION'], cache_url)
                self.assertEqual(caches['default']['KEY_PREFIX'], 'self-medical')

    def test_unsupported_scheme_fails_closed(self):
        for cache_url in ('memcached://cache:11211', 'cache.example:6379'):
            with self.subTest(cache_url=cache_url):
                with self.assertRaises(ImproperlyConfigured):
                    build_caches(cache_url=cache_url, app_env='production')

    def test_tests_always_use_locmem_even_with_url(self):
        by_app_env = build_caches(cache_url='redis://red:6379', app_env='test')
        by_runner = build_caches(cache_url='redis://red:6379', app_env='production', running_tests=True)
        self.assertEqual(by_app_env['default']['BACKEND'], LOCMEM_BACKEND)
        self.assertEqual(by_runner['default']['BACKEND'], LOCMEM_BACKEND)

    def test_settings_cache_is_locmem_under_test_runner(self):
        self.assertEqual(settings.CACHES['default']['BACKEND'], LOCMEM_BACKEND)


class DatabaseSafetyTest(SimpleTestCase):
    postgres = 'django.db.backends.postgresql'

    def classify(self, **overrides):
        values = {
            'engine': self.postgres,
            'host': 'unclassified.example.invalid',
            'name': 'unclassified_db',
            'explicit_is_production': None,
        }
        values.update(overrides)
        return classify_database(**values)

    def validate(self, app_env, classification, *, override=False):
        return validate_database_target(
            app_env=app_env,
            classification=classification,
            override_enabled=override,
            engine=self.postgres,
            host='prod-example.supabase.co',
            name='production_db',
        )

    def test_app_env_defaults_normalizes_and_rejects_unknown_values(self):
        self.assertEqual(parse_app_env(None), 'local')
        for value in ('local', 'development', 'staging', 'production', 'test'):
            self.assertEqual(parse_app_env(f' {value.upper()} '), value)
        with self.assertRaises(ImproperlyConfigured):
            parse_app_env('preview')

    def test_strict_boolean_parser_accepts_supported_values(self):
        for value in ('true', '1', 'yes', 'on', 'TRUE'):
            self.assertIs(parse_strict_bool('FLAG', value), True)
        for value in ('false', '0', 'no', 'off', 'FALSE'):
            self.assertIs(parse_strict_bool('FLAG', value), False)
        self.assertIsNone(parse_strict_bool('FLAG', None, default=None))
        with self.assertRaises(ImproperlyConfigured):
            parse_strict_bool('FLAG', 'sometimes')

    def test_marker_parser_trims_lowercases_and_drops_empty_values(self):
        self.assertEqual(parse_markers(' Prod-A , ,PROD-B  '), ('prod-a', 'prod-b'))
        self.assertEqual(parse_markers(''), ())

    def test_sqlite_is_classified_before_explicit_flags(self):
        self.assertEqual(
            self.classify(
                engine='django.db.backends.sqlite3',
                name=':memory:',
                explicit_is_production=True,
            ),
            SQLITE_TEST,
        )
        self.assertEqual(
            self.classify(engine='django.db.backends.sqlite3', name='db.sqlite3'),
            SQLITE_TEST,
        )

    def test_explicit_production_flag_has_priority_over_markers(self):
        self.assertEqual(
            self.classify(
                host='prod-example.supabase.co',
                explicit_is_production=False,
                production_host_markers=('prod-example',),
            ),
            NON_PRODUCTION,
        )
        self.assertEqual(self.classify(explicit_is_production=True), PRODUCTION)

    def test_host_and_name_markers_classify_database(self):
        self.assertEqual(
            self.classify(host='prod-example.supabase.co', production_host_markers=('prod-example',)),
            PRODUCTION,
        )
        self.assertEqual(
            self.classify(name='production_db', production_name_markers=('production_db',)),
            PRODUCTION,
        )
        self.assertEqual(
            self.classify(host='dev-example.supabase.co', non_production_host_markers=('dev-example',)),
            NON_PRODUCTION,
        )
        self.assertEqual(
            self.classify(name='development_db', non_production_name_markers=('development_db',)),
            NON_PRODUCTION,
        )
        self.assertEqual(self.classify(), UNKNOWN)

    def test_environment_database_compatibility_matrix(self):
        for app_env in ('local', 'development', 'test', 'staging'):
            with self.subTest(app_env=app_env), self.assertRaises(ImproperlyConfigured):
                self.validate(app_env, PRODUCTION)
        self.assertEqual(self.validate('production', PRODUCTION).status, 'SAFE')
        for classification in (NON_PRODUCTION, UNKNOWN, SQLITE_TEST):
            with self.subTest(classification=classification), self.assertRaises(ImproperlyConfigured):
                self.validate('production', classification)
        self.assertEqual(self.validate('development', NON_PRODUCTION).status, 'SAFE')
        sqlite_result = validate_database_target(
            app_env='test',
            classification=SQLITE_TEST,
            override_enabled=False,
            engine='django.db.backends.sqlite3',
            host='',
            name=':memory:',
        )
        self.assertEqual(sqlite_result.status, 'SAFE')
        with self.assertRaises(ImproperlyConfigured):
            self.validate('development', UNKNOWN)

    def test_mismatch_override_warns_and_marks_status(self):
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter('always')
            result = self.validate('development', PRODUCTION, override=True)
        self.assertEqual(result.status, 'OVERRIDE')
        self.assertTrue(any('override is active' in str(item.message) for item in captured))

    def test_diagnostics_mask_target_and_do_not_expose_credentials(self):
        host = 'prod-example.supabase.co'
        name = 'production_db'
        self.assertEqual(mask_host(host), 'prod****.supabase.co')
        self.assertEqual(mask_name(name), 'prod****')
        with self.assertRaises(ImproperlyConfigured) as caught:
            self.validate('local', PRODUCTION)
        message = str(caught.exception)
        for secret in (host, name, 'db-secret-password', 'db-secret-user', 'postgresql://secret'):
            self.assertNotIn(secret, message)

    def test_show_db_target_command_uses_masked_settings_only(self):
        output = StringIO()
        call_command('show_db_target', stdout=output)
        rendered = output.getvalue()
        self.assertIn('APP_ENV', rendered)
        self.assertIn('DB_CLASSIFICATION', rendered)
        self.assertIn('STATUS', rendered)
        self.assertIn('CACHE_BACKEND', rendered)
        self.assertIn('locmem', rendered)
        self.assertNotIn('redis://', rendered)
        self.assertNotIn('DB_PASSWORD', rendered)
        self.assertNotIn('DB_USER', rendered)
        self.assertNotIn('DATABASE_URL', rendered)


class RequiredDocumentFlowTest(TestCase):
    def setUp(self):
        cache.clear()
        self.team = Team.objects.create(
            name='v2 필수서류팀',
            code='V2DOC',
            is_active=True,
            start_date=date.today(),
            max_day_no=3,
        )
        self.participant = Participant.objects.create(
            team=self.team,
            name='홍길동',
            gender='M',
            birth_date=date(1990, 1, 1),
            phone='010-1234-5678',
            normalized_name='홍길동',
            normalized_phone='01012345678',
            is_active=True,
        )
        self.respondent = Respondent.objects.create(
            team=self.team,
            name='홍길동',
            birth_date=date(1990, 1, 1),
            phone='01012345678',
            normalized_name='홍길동',
            normalized_phone='01012345678',
        )
        self.features = TeamFeatureSetting.objects.create(
            team=self.team,
            enable_privacy_consent=True,
            enable_third_party_consent=True,
            enable_safety_pledge=True,
        )
        self.templates = {}
        for index, document_type in enumerate(
            (
                DocumentTemplate.DocumentType.PRIVACY_CONSENT,
                DocumentTemplate.DocumentType.THIRD_PARTY_CONSENT,
                DocumentTemplate.DocumentType.SAFETY_PLEDGE,
            ),
            start=1,
        ):
            self.templates[document_type] = DocumentTemplate.objects.create(
                team=self.team,
                document_type=document_type,
                title=f'필수 문서 {index}',
                content_html=f'<p>문서 본문 {index}</p>',
                version=1,
                is_active=True,
            )

    def login_session(self):
        session = self.client.session
        session['survey_team_id'] = self.team.id
        session['survey_team_code'] = self.team.code
        session['survey_respondent_id'] = self.respondent.id
        session['survey_day_no'] = 1
        session.save()

    def begin_pre_identity_consent(self):
        response = self.client.post(
            reverse('surveys:preview-v2-code'),
            {'team_code': self.team.code},
        )
        self.assertRedirects(
            response,
            reverse('surveys:preview-v2-privacy-consent'),
            fetch_redirect_response=False,
        )
        consent_response = self.client.post(
            reverse('surveys:preview-v2-privacy-consent'),
            {'agreed': 'on'},
        )
        self.assertRedirects(
            consent_response,
            reverse('surveys:preview-v2-identity'),
            fetch_redirect_response=False,
        )
        self.assertIn(
            SESSION_PRE_IDENTITY_PRIVACY_CONSENT,
            self.client.session,
        )

    def submit_document(self, document_type):
        return create_required_document_submission(
            team=self.team,
            respondent=self.respondent,
            document_type=document_type,
            template=self.templates[document_type],
        )

    def post_document_consent(self, url, document_type, *, finalize=False, **extra):
        payload = {
            'action': 'finalize' if finalize else 'record_consent',
            'document_type': document_type,
            'agreed': 'on',
            **extra,
        }
        return self.client.post(url, payload)

    def complete_document_flow(self, url, document_types=None, **extra):
        document_types = document_types or list(get_enabled_required_document_types(self.team))
        responses = []
        for index, current_type in enumerate(document_types):
            responses.append(
                self.post_document_consent(
                    url,
                    current_type,
                    finalize=index == len(document_types) - 1,
                    **extra,
                )
            )
        return responses

    def test_enabled_document_types_follow_fixed_order_and_skip_disabled(self):
        self.assertEqual(
            get_enabled_required_document_types(self.team),
            ('privacy_consent', 'third_party_consent', 'safety_pledge'),
        )
        combinations = (
            ((True, False, False), ('privacy_consent',)),
            ((False, True, False), ('third_party_consent',)),
            ((False, False, True), ('safety_pledge',)),
            ((True, False, True), ('privacy_consent', 'safety_pledge')),
            ((False, False, False), ()),
        )
        for flags, expected in combinations:
            with self.subTest(flags=flags):
                (
                    self.features.enable_privacy_consent,
                    self.features.enable_third_party_consent,
                    self.features.enable_safety_pledge,
                ) = flags
                self.features.save()
                self.assertEqual(get_enabled_required_document_types(self.team), expected)

    def test_required_documents_are_disabled_by_default(self):
        default_team = Team.objects.create(
            name='기본 필수문서 팀',
            organization_name='운영기관',
            organization_name_en='ORGANIZATION',
            code='DEFAULT-DOCS',
            is_active=True,
            start_date=date.today(),
            max_day_no=1,
        )
        settings = TeamFeatureSetting.objects.create(team=default_team)

        self.assertFalse(settings.enable_privacy_consent)
        self.assertFalse(settings.enable_third_party_consent)
        self.assertFalse(settings.enable_safety_pledge)
        self.assertEqual(
            get_enabled_required_document_types(default_team),
            (),
        )

    def test_team_without_feature_settings_keeps_legacy_flow_ungated(self):
        team_without_settings = Team.objects.create(
            name='설정 없는 팀',
            organization_name='운영기관',
            organization_name_en='ORGANIZATION',
            code='NO-FEATURE-ROW',
            is_active=True,
            start_date=date.today(),
            max_day_no=1,
        )

        self.assertEqual(
            get_enabled_required_document_types(team_without_settings),
            (),
        )

    def test_identity_creates_privacy_submission_before_remaining_documents(self):
        self.begin_pre_identity_consent()
        response = self.client.post(
            reverse('surveys:preview-v2-identity'),
            {'name': '홍길동', 'birth_date': '900101', 'phone': '01012345678', 'gender': 'M'},
        )

        self.assertRedirects(
            response,
            reverse('surveys:preview-v2-documents-next'),
            fetch_redirect_response=False,
        )
        next_response = self.client.get(response.url)
        self.assertRedirects(
            next_response,
            reverse('surveys:preview-v2-document', kwargs={'document_type': 'third_party_consent'}),
            fetch_redirect_response=False,
        )
        privacy = DocumentSubmission.objects.get(document_type='privacy_consent')
        self.assertEqual(privacy.respondent, self.respondent)
        self.assertNotIn(
            SESSION_PRE_IDENTITY_PRIVACY_CONSENT,
            self.client.session,
        )

    def test_identity_sets_login_expiry_to_team_end(self):
        self.begin_pre_identity_consent()
        response = self.client.post(
            reverse('surveys:preview-v2-identity'),
            {
                'name': '홍길동',
                'birth_date': '900101',
                'phone': '01012345678',
                'gender': 'M',
            },
        )

        self.assertEqual(response.status_code, 302)
        session = self.client.session
        expected_end = timezone.make_aware(
            datetime.combine(
                self.team.start_date + timedelta(days=self.team.max_day_no - 1),
                datetime.max.time().replace(microsecond=0),
            ),
            timezone.get_current_timezone(),
        )
        self.assertEqual(
            datetime.fromisoformat(session[SESSION_LOGIN_EXPIRES_AT]),
            expected_end,
        )
        self.assertEqual(session.get_expiry_date(), expected_end)
        self.assertEqual(session[SESSION_RESPONDENT_ID], self.respondent.pk)

    def test_persisted_login_revalidates_active_participant(self):
        self.begin_pre_identity_consent()
        self.client.post(
            reverse('surveys:preview-v2-identity'),
            {
                'name': '홍길동',
                'birth_date': '900101',
                'phone': '01012345678',
                'gender': 'M',
            },
        )
        self.participant.is_active = False
        self.participant.save(update_fields=['is_active', 'updated_at'])
        cache.clear()

        response = self.client.get(reverse('surveys:preview-v2-home'))

        self.assertRedirects(
            response,
            reverse('surveys:preview-v2-code'),
            fetch_redirect_response=False,
        )
        session = self.client.session
        self.assertNotIn(SESSION_RESPONDENT_ID, session)
        self.assertNotIn(SESSION_LOGIN_EXPIRES_AT, session)

    def test_code_page_restores_valid_persisted_login(self):
        self.begin_pre_identity_consent()
        self.client.post(
            reverse('surveys:preview-v2-identity'),
            {
                'name': '홍길동',
                'birth_date': '900101',
                'phone': '01012345678',
                'gender': 'M',
            },
        )

        response = self.client.get(reverse('surveys:preview-v2-code'))

        self.assertRedirects(
            response,
            reverse('surveys:preview-v2-documents-next'),
            fetch_redirect_response=False,
        )

    def test_reset_login_clears_identity_and_keeps_language(self):
        self.begin_pre_identity_consent()
        self.client.post(
            reverse('surveys:preview-v2-identity'),
            {
                'name': '홍길동',
                'birth_date': '900101',
                'phone': '01012345678',
                'gender': 'M',
            },
        )
        session = self.client.session
        session[SESSION_PUBLIC_LANGUAGE] = 'en'
        session['survey_answers_draft'] = {'answer': 'temporary'}
        session.save()
        old_session_key = session.session_key

        response = self.client.post(reverse('surveys:preview-v2-reset-login'))

        self.assertRedirects(
            response,
            reverse('surveys:preview-v2-code'),
            fetch_redirect_response=False,
        )
        reset_session = self.client.session
        self.assertNotEqual(reset_session.session_key, old_session_key)
        self.assertEqual(reset_session[SESSION_PUBLIC_LANGUAGE], 'en')
        for key in (
            SESSION_TEAM_ID,
            SESSION_TEAM_CODE,
            SESSION_RESPONDENT_ID,
            SESSION_DAY_NO,
            SESSION_LOGIN_EXPIRES_AT,
            SESSION_REQUIRED_DOCUMENT_CONSENTS,
            SESSION_PRE_IDENTITY_PRIVACY_CONSENT,
            'survey_answers_draft',
        ):
            self.assertNotIn(key, reset_session)

    def test_reset_login_is_post_only_and_home_contains_csrf_form(self):
        self.login_session()
        for document_type in (
            'privacy_consent',
            'third_party_consent',
            'safety_pledge',
        ):
            self.submit_document(document_type)

        home = self.client.get(reverse('surveys:preview-v2-home'))

        self.assertContains(home, '로그인 정보 초기화')
        self.assertContains(home, 'csrfmiddlewaretoken')
        self.assertContains(home, reverse('surveys:preview-v2-reset-login'))
        self.assertEqual(
            self.client.get(reverse('surveys:preview-v2-reset-login')).status_code,
            405,
        )

    def test_progress_and_next_document_resume_after_partial_submission(self):
        self.assertEqual(get_next_pending_required_document(team=self.team, respondent=self.respondent), 'privacy_consent')
        self.submit_document('privacy_consent')
        self.assertEqual(get_next_pending_required_document(team=self.team, respondent=self.respondent), 'third_party_consent')
        progress = get_required_document_progress(team=self.team, respondent=self.respondent)
        self.assertEqual((progress.completed, progress.total), (1, 3))
        self.submit_document('third_party_consent')
        self.submit_document('safety_pledge')
        self.assertIsNone(get_next_pending_required_document(team=self.team, respondent=self.respondent))

    def test_active_template_uses_latest_version_and_excludes_inactive(self):
        DocumentTemplate.objects.create(
            team=self.team,
            document_type='privacy_consent',
            title='비활성 최신본',
            content_html='inactive',
            version=3,
            is_active=False,
        )
        latest = DocumentTemplate.objects.create(
            team=self.team,
            document_type='privacy_consent',
            title='활성 최신본',
            content_html='active',
            version=2,
            is_active=True,
        )
        self.assertEqual(
            get_active_document_template(team=self.team, document_type='privacy_consent'),
            latest,
        )

    def test_submission_saves_server_owned_snapshot_and_is_unique(self):
        template = self.templates['privacy_consent']
        submission = self.submit_document('privacy_consent')
        self.assertEqual(submission.team, self.team)
        self.assertEqual(submission.respondent, self.respondent)
        self.assertEqual(submission.template, template)
        self.assertEqual(submission.template_version, template.version)
        self.assertEqual(submission.template_hash_at_submit, template.body_hash)
        self.assertEqual(submission.signer_name, self.respondent.name)
        self.assertIsNotNone(submission.agreed_at)
        template.content_html = '수정된 원문'
        template.save()
        submission.refresh_from_db()
        self.assertNotEqual(submission.template_hash_at_submit, template.body_hash)
        with self.assertRaises(RequiredDocumentAlreadySubmitted):
            self.submit_document('privacy_consent')

    def test_submission_rejects_team_respondent_type_and_inactive_template_mismatch(self):
        other_team = Team.objects.create(
            name='다른 팀', code='OTHERDOC', is_active=True, start_date=date.today(), max_day_no=1,
        )
        other_respondent = Respondent.objects.create(
            team=other_team,
            name='다른 사람',
            birth_date=date(1991, 1, 1),
            phone='01099999999',
            normalized_name='다른사람',
            normalized_phone='01099999999',
        )
        with self.assertRaises(RequiredDocumentTemplateInvalid):
            create_required_document_submission(
                team=self.team,
                respondent=other_respondent,
                document_type='privacy_consent',
                template=self.templates['privacy_consent'],
            )
        with self.assertRaises(RequiredDocumentTemplateInvalid):
            create_required_document_submission(
                team=self.team,
                respondent=self.respondent,
                document_type='privacy_consent',
                template=self.templates['safety_pledge'],
            )
        inactive = self.templates['privacy_consent']
        inactive.is_active = False
        inactive.save()
        with self.assertRaises(RequiredDocumentTemplateInvalid):
            create_required_document_submission(
                team=self.team,
                respondent=self.respondent,
                document_type='privacy_consent',
                template=inactive,
            )

    def test_integrity_error_is_reported_as_duplicate(self):
        with patch('surveys.required_documents.DocumentSubmission.objects.create', side_effect=IntegrityError):
            with self.assertRaises(RequiredDocumentAlreadySubmitted):
                self.submit_document('privacy_consent')

    def test_v2_code_and_identity_post_redirect_to_required_documents(self):
        response = self.client.post(reverse('surveys:preview-v2-code'), {'team_code': self.team.code})
        self.assertRedirects(
            response,
            reverse('surveys:preview-v2-privacy-consent'),
            fetch_redirect_response=False,
        )
        privacy_response = self.client.get(reverse('surveys:preview-v2-privacy-consent'))
        self.assertContains(privacy_response, 'name="agreed"')
        self.assertContains(privacy_response, '개인정보 수집·이용 동의서')
        self.begin_pre_identity_consent()
        identity_response = self.client.get(reverse('surveys:preview-v2-identity'))
        self.assertEqual(identity_response.status_code, 200)
        self.assertContains(identity_response, 'method="post"')
        self.assertContains(identity_response, 'name="gender"')
        self.assertContains(identity_response, 'name="name"')
        self.assertNotContains(identity_response, 'name="phone"')
        self.assertContains(identity_response, 'name="birth_date"')
        self.assertContains(identity_response, 'csrfmiddlewaretoken')
        self.assertContains(identity_response, 'surveys/icons/v2-identity-shield.svg')
        self.assertContains(identity_response, 'surveys/icons/v2-identity-name.svg')
        self.assertContains(identity_response, 'surveys/icons/v2-identity-gender.svg')
        self.assertNotContains(identity_response, 'surveys/icons/v2-identity-user.svg')
        self.assertContains(identity_response, 'v2-primary-button')
        identity_html = identity_response.content.decode()
        field_positions = [
            identity_html.index('name="name"'),
            identity_html.index('name="birth_date"'),
            identity_html.index('name="gender"'),
        ]
        self.assertEqual(field_positions, sorted(field_positions))
        self.assertIn('v2-identity-control--select', identity_html)
        response = self.client.post(
            reverse('surveys:preview-v2-identity'),
            {'name': '홍 길동', 'birth_date': '900101', 'phone': '010-1234-5678', 'gender': 'M'},
        )
        self.assertRedirects(response, reverse('surveys:preview-v2-documents-next'), fetch_redirect_response=False)
        next_response = self.client.get(reverse('surveys:preview-v2-documents-next'))
        self.assertRedirects(
            next_response,
            reverse('surveys:preview-v2-document', kwargs={'document_type': 'third_party_consent'}),
            fetch_redirect_response=False,
        )

    def test_privacy_consent_requires_explicit_checkbox_before_identity(self):
        self.client.post(
            reverse('surveys:preview-v2-code'),
            {'team_code': self.team.code},
        )

        response = self.client.post(
            reverse('surveys:preview-v2-privacy-consent'),
            {},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '동의 또는 서약 확인이 필요합니다.')
        self.assertNotIn(
            SESSION_PRE_IDENTITY_PRIVACY_CONSENT,
            self.client.session,
        )
        self.assertFalse(DocumentSubmission.objects.exists())

    def test_identity_cannot_be_opened_before_privacy_consent(self):
        self.client.post(
            reverse('surveys:preview-v2-code'),
            {'team_code': self.team.code},
        )

        response = self.client.get(reverse('surveys:preview-v2-identity'))

        self.assertRedirects(
            response,
            reverse('surveys:preview-v2-privacy-consent'),
            fetch_redirect_response=False,
        )
        self.assertFalse(DocumentSubmission.objects.exists())

    def test_changed_privacy_evidence_is_not_saved_during_identity(self):
        self.begin_pre_identity_consent()
        respondent_count = Respondent.objects.count()

        with patch(
            'surveys.required_documents.build_canonical_document_evidence',
            side_effect=lambda **kwargs: {
                **build_canonical_document_evidence(**kwargs),
                'version': STATIC_REQUIRED_DOCUMENT_VERSION + 1,
            },
        ):
            response = self.client.post(
                reverse('surveys:preview-v2-identity'),
                {
                    'name': '홍길동',
                    'birth_date': '900101',
                    'phone': '01012345678',
                    'gender': 'M',
                },
            )

        self.assertRedirects(
            response,
            reverse('surveys:preview-v2-privacy-consent'),
            fetch_redirect_response=False,
        )
        self.assertEqual(Respondent.objects.count(), respondent_count)
        self.assertFalse(DocumentSubmission.objects.exists())

    def test_v2_identity_keeps_required_field_errors(self):
        self.begin_pre_identity_consent()
        response = self.client.post(
            reverse('surveys:preview-v2-identity'),
            {'name': '', 'birth_date': '', 'phone': '', 'gender': ''},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '필수 항목입니다.', count=2)
        self.assertContains(response, '성별을 선택해 주세요.')
        self.assertContains(response, 'v2-field--invalid', count=3)

    def test_v2_identity_blocks_participant_with_missing_gender(self):
        self.participant.gender = None
        self.participant.save(update_fields=['gender', 'updated_at'])
        self.begin_pre_identity_consent()
        response = self.client.post(
            reverse('surveys:preview-v2-identity'),
            {'name': '홍길동', 'birth_date': '900101', 'phone': '01012345678', 'gender': 'M'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '입력한 정보가 참가자 명단과 일치하지 않습니다.')
        self.assertContains(response, 'v2-field--invalid', count=1)
        self.assertNotIn('survey_respondent_id', self.client.session)
        self.assertFalse(DocumentSubmission.objects.exists())

    def test_v2_identity_marks_name_mismatch_on_name_field(self):
        self.begin_pre_identity_consent()
        response = self.client.post(
            reverse('surveys:preview-v2-identity'),
            {'name': '다른이름', 'birth_date': '900101', 'phone': '01012345678', 'gender': 'M'},
        )
        self.assertContains(response, '입력한 정보가 참가자 명단과 일치하지 않습니다.')
        self.assertContains(response, 'v2-field--invalid', count=1)
        self.assertFalse(DocumentSubmission.objects.exists())

    def test_v2_identity_marks_birth_date_mismatch_on_birth_date_field(self):
        self.begin_pre_identity_consent()
        response = self.client.post(
            reverse('surveys:preview-v2-identity'),
            {'name': '홍길동', 'birth_date': '910721', 'phone': '01012345678', 'gender': 'M'},
        )
        self.assertContains(response, '입력한 정보가 참가자 명단과 일치하지 않습니다.')
        self.assertContains(response, 'v2-field--invalid', count=1)
        self.assertFalse(DocumentSubmission.objects.exists())

    def test_v2_identity_marks_gender_mismatch_on_gender_field(self):
        self.begin_pre_identity_consent()
        response = self.client.post(
            reverse('surveys:preview-v2-identity'),
            {'name': '홍길동', 'birth_date': '900101', 'phone': '01012345678', 'gender': 'F'},
        )
        self.assertContains(response, '입력한 정보가 참가자 명단과 일치하지 않습니다.')
        self.assertContains(response, 'v2-field--invalid', count=1)
        self.assertFalse(DocumentSubmission.objects.exists())

    def test_v2_english_name_login_is_case_insensitive(self):
        Participant.objects.create(
            team=self.team,
            name='Alexandra Johnson',
            gender='F',
            birth_date=date(1993, 5, 6),
            phone='010-7777-8888',
            normalized_name='AlexandraJohnson',
            normalized_phone='01077778888',
            is_active=True,
        )
        self.begin_pre_identity_consent()

        response = self.client.post(
            reverse('surveys:preview-v2-identity'),
            {
                'name': 'aLeXaNdRa jOhNsOn',
                'birth_date': '930506',
                'phone': '01077778888',
                'gender': 'F',
            },
        )

        self.assertRedirects(
            response,
            reverse('surveys:preview-v2-documents-next'),
            fetch_redirect_response=False,
        )
        respondent = Respondent.objects.get(pk=self.client.session[SESSION_RESPONDENT_ID])
        self.assertEqual(respondent.name, 'Alexandra Johnson')
        self.assertEqual(respondent.normalized_name, 'AlexandraJohnson')

    def test_identity_redirects_home_when_all_documents_disabled(self):
        self.features.enable_privacy_consent = False
        self.features.enable_third_party_consent = False
        self.features.enable_safety_pledge = False
        self.features.save()
        self.client.post(reverse('surveys:preview-v2-code'), {'team_code': self.team.code})
        response = self.client.post(
            reverse('surveys:preview-v2-identity'),
            {'name': '홍길동', 'birth_date': '900101', 'phone': '01012345678', 'gender': 'M'},
        )
        self.assertRedirects(response, reverse('surveys:preview-v2-documents-next'), fetch_redirect_response=False)
        self.assertRedirects(
            self.client.get(response.url),
            reverse('surveys:preview-v2-home'),
            fetch_redirect_response=False,
        )

    def test_identity_redirects_home_when_team_has_no_feature_settings(self):
        self.features.delete()
        self.client.post(reverse('surveys:preview-v2-code'), {'team_code': self.team.code})
        response = self.client.post(
            reverse('surveys:preview-v2-identity'),
            {'name': '홍길동', 'birth_date': '900101', 'phone': '01012345678', 'gender': 'M'},
        )

        self.assertRedirects(
            response,
            reverse('surveys:preview-v2-documents-next'),
            fetch_redirect_response=False,
        )
        self.assertRedirects(
            self.client.get(response.url),
            reverse('surveys:preview-v2-home'),
            fetch_redirect_response=False,
        )

    def test_identity_resumes_at_next_pending_document_after_relogin(self):
        self.submit_document('privacy_consent')
        self.begin_pre_identity_consent()
        response = self.client.post(
            reverse('surveys:preview-v2-identity'),
            {'name': '홍길동', 'birth_date': '900101', 'phone': '01012345678', 'gender': 'M'},
        )
        self.assertRedirects(response, reverse('surveys:preview-v2-documents-next'), fetch_redirect_response=False)
        next_response = self.client.get(response.url)
        self.assertRedirects(
            next_response,
            reverse('surveys:preview-v2-document', kwargs={'document_type': 'third_party_consent'}),
            fetch_redirect_response=False,
        )

    def test_direct_out_of_order_disabled_and_submitted_document_access_redirects(self):
        self.login_session()
        safety_url = reverse('surveys:preview-v2-document', kwargs={'document_type': 'safety_pledge'})
        privacy_url = reverse('surveys:preview-v2-document', kwargs={'document_type': 'privacy_consent'})
        self.assertRedirects(self.client.get(safety_url), privacy_url, fetch_redirect_response=False)
        self.submit_document('privacy_consent')
        third_url = reverse('surveys:preview-v2-document', kwargs={'document_type': 'third_party_consent'})
        self.assertRedirects(self.client.get(privacy_url), third_url, fetch_redirect_response=False)
        self.features.enable_third_party_consent = False
        self.features.save()
        self.assertRedirects(self.client.get(third_url), safety_url, fetch_redirect_response=False)

    def test_static_documents_do_not_require_document_template_rows(self):
        self.login_session()
        DocumentTemplate.objects.filter(team=self.team).delete()
        url = reverse('surveys:preview-v2-document', kwargs={'document_type': 'privacy_consent'})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '등록된 문서가 없습니다.')
        self.assertContains(response, '수집·이용하는 개인정보 항목')
        responses = self.complete_document_flow(url)
        self.assertTrue(responses[-1].json()['completed'])
        self.assertEqual(
            responses[-1].json()['redirect_url'],
            reverse(
                'surveys:preview-v2-completion',
                kwargs={'completion_type': 'documents'},
            ),
        )
        self.assertEqual(DocumentSubmission.objects.count(), 3)
        self.assertFalse(DocumentSubmission.objects.exclude(template=None).exists())

    def test_checkbox_required_and_forged_signer_is_ignored(self):
        self.login_session()
        url = reverse('surveys:preview-v2-document', kwargs={'document_type': 'privacy_consent'})
        response = self.client.post(
            url,
            {
                'action': 'record_consent',
                'document_type': 'privacy_consent',
                'signer_name': '위조 이름',
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], '동의 또는 서약 확인이 필요합니다.')
        responses = self.complete_document_flow(url, signer_name='위조 이름')
        self.assertTrue(responses[-1].json()['completed'])
        self.assertEqual(DocumentSubmission.objects.count(), 3)
        self.assertFalse(DocumentSubmission.objects.exclude(signer_name='홍길동').exists())

    def test_required_document_uses_figma_layout_and_keeps_secure_form_contract(self):
        self.team.organization_name = '독도재단'
        self.team.organization_name_en = 'DOKDO FOUNDATION'
        self.team.save(update_fields=['organization_name', 'organization_name_en', 'updated_at'])
        self.login_session()

        response = self.client.get(
            reverse('surveys:preview-v2-document', kwargs={'document_type': 'privacy_consent'}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'v2-document-hero')
        self.assertContains(response, '개인정보 수집 및 이용 동의')
        self.assertNotContains(response, 'Personal Information Agreement')
        self.assertContains(response, 'v2-document-shield.svg')
        self.assertContains(response, 'surveys/v2_document.js')
        self.assertContains(response, 'method="post"')
        self.assertContains(response, 'name="agreed"')
        self.assertContains(response, 'csrfmiddlewaretoken')
        self.assertContains(response, '독도재단')
        self.assertNotContains(response, 'DOKDO FOUNDATION')
        self.assertContains(response, '동의하고 계속하기')
        html = response.content.decode()
        self.assertEqual(html.count('data-v2-document-form'), 1)
        self.assertEqual(html.count('type="submit"'), 1)
        self.assertEqual(html.count('data-document-next'), 2)

    def test_single_submit_creates_only_documents_enabled_for_team(self):
        self.features.enable_third_party_consent = False
        self.features.save(update_fields=['enable_third_party_consent', 'updated_at'])
        self.login_session()

        url = reverse('surveys:preview-v2-document', kwargs={'document_type': 'privacy_consent'})
        responses = self.complete_document_flow(
            url,
            ['privacy_consent', 'safety_pledge'],
        )

        self.assertTrue(responses[-1].json()['completed'])
        self.assertEqual(
            set(DocumentSubmission.objects.values_list('document_type', flat=True)),
            {'privacy_consent', 'safety_pledge'},
        )

    def test_required_document_stepper_marks_disabled_team_document(self):
        self.features.enable_third_party_consent = False
        self.features.save(update_fields=['enable_third_party_consent', 'updated_at'])
        self.login_session()

        response = self.client.get(
            reverse('surveys:preview-v2-document', kwargs={'document_type': 'privacy_consent'}),
        )
        html = response.content.decode()
        disabled_step = html.index('is-disabled')
        third_party_label = html.index('정보제공 동의서')

        self.assertLess(disabled_step, third_party_label)
        self.assertContains(response, 'aria-current="step"')

    def test_single_final_submission_creates_all_documents_and_prevents_reread(self):
        self.login_session()
        url = reverse('surveys:preview-v2-document', kwargs={'document_type': 'privacy_consent'})
        responses = self.complete_document_flow(url)
        self.assertTrue(responses[-1].json()['completed'])
        self.assertEqual(
            set(DocumentSubmission.objects.values_list('document_type', flat=True)),
            {'privacy_consent', 'third_party_consent', 'safety_pledge'},
        )
        old_url = reverse('surveys:preview-v2-document', kwargs={'document_type': 'privacy_consent'})
        self.assertRedirects(self.client.get(old_url), reverse('surveys:preview-v2-home'), fetch_redirect_response=False)

    def test_home_gate_and_completed_group_card(self):
        self.login_session()
        home_url = reverse('surveys:preview-v2-home')
        self.assertRedirects(
            self.client.get(home_url),
            reverse('surveys:preview-v2-documents-next'),
            fetch_redirect_response=False,
        )
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)
        response = self.client.get(home_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '홍길동')
        self.assertContains(response, '동의서')
        self.assertContains(response, '3 / 3 제출 완료')
        self.assertContains(
            response,
            'data-card="required-documents" '
            f'href="{reverse("surveys:preview-v2-completion", kwargs={"completion_type": "documents"})}"',
        )
        self.assertContains(response, 'check-documents.svg')
        self.assertNotContains(response, 'href="/surveys/preview/v2/documents/')

    def test_home_keeps_document_card_when_no_documents_enabled(self):
        self.features.enable_privacy_consent = False
        self.features.enable_third_party_consent = False
        self.features.enable_safety_pledge = False
        self.features.save()
        self.login_session()
        response = self.client.get(reverse('surveys:preview-v2-home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-card="required-documents"', count=1)
        self.assertContains(response, '0 / 0 제출 완료')

    def test_home_shows_two_of_two_when_only_two_documents_are_enabled(self):
        self.features.enable_third_party_consent = False
        self.features.save()
        self.submit_document('privacy_consent')
        self.submit_document('safety_pledge')
        self.login_session()
        response = self.client.get(reverse('surveys:preview-v2-home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '2 / 2 제출 완료')

    def test_home_cards_follow_fixed_order_and_feature_visibility(self):
        self._create_daily_form_with_question(title='1일차 건강관리')
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)
        self.login_session()

        response = self.client.get(reverse('surveys:preview-v2-home'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        positions = [
            content.index('data-card="required-documents"'),
            content.index('data-card="health"'),
            content.index('data-card="reflection"'),
            content.index('data-card="exit-survey"'),
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertContains(response, reverse('surveys:preview-v2-health-start'))
        self.assertContains(response, '소감문')
        self.assertContains(response, '설문지')
        home_url = reverse('surveys:preview-v2-home')
        self.assertContains(
            response,
            f'data-card="reflection" href="{reverse("surveys:preview-v2-reflection")}"',
        )
        self.assertContains(
            response,
            f'data-card="exit-survey" href="{home_url}"',
        )

    def test_home_language_switch_persists_english_in_session(self):
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)
        self.login_session()

        response = self.client.get(
            reverse('surveys:preview-v2-home'),
            {'lang': 'en'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Health Check')
        self.assertContains(response, 'Consent')
        self.assertContains(response, '한국어')
        self.assertEqual(self.client.session['v2_public_language'], 'en')

    def test_initial_english_login_opens_home_in_english(self):
        self.features.enable_privacy_consent = False
        self.features.enable_third_party_consent = False
        self.features.enable_safety_pledge = False
        self.features.save()

        language_response = self.client.get(
            reverse('surveys:preview-v2-code'),
            {'lang': 'en'},
        )
        self.assertEqual(language_response.status_code, 200)
        code_response = self.client.post(
            reverse('surveys:preview-v2-code'),
            {'team_code': self.team.code},
        )
        self.assertRedirects(
            code_response,
            reverse('surveys:preview-v2-identity'),
            fetch_redirect_response=False,
        )
        identity_response = self.client.post(
            reverse('surveys:preview-v2-identity'),
            {
                'name': '홍길동',
                'birth_date': '900101',
                'phone': '01012345678',
                'gender': 'M',
            },
        )
        self.assertRedirects(
            identity_response,
            reverse('surveys:preview-v2-documents-next'),
            fetch_redirect_response=False,
        )
        next_response = self.client.get(identity_response.url)
        self.assertRedirects(
            next_response,
            reverse('surveys:preview-v2-home'),
            fetch_redirect_response=False,
        )
        home_response = self.client.get(next_response.url)

        self.assertEqual(home_response.status_code, 200)
        self.assertEqual(home_response.context['page_language'], 'en')
        self.assertContains(home_response, 'Welcome!')
        self.assertContains(home_response, 'Health Check')
        self.assertEqual(self.client.session[SESSION_PUBLIC_LANGUAGE], 'en')

    def test_home_always_shows_all_four_card_sections(self):
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)
        self.login_session()

        response = self.client.get(reverse('surveys:preview-v2-home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-card="required-documents"', count=1)
        self.assertContains(response, 'data-card="health"', count=1)
        self.assertContains(response, 'data-card="reflection"', count=1)
        self.assertContains(response, 'data-card="exit-survey"', count=1)

    def test_home_health_card_shows_notice_without_daily_health_form(self):
        SurveyForm.objects.create(
            team=self.team,
            day_no=1,
            title='설문',
            version=1,
            is_active=True,
            form_type=SurveyForm.FormType.EXIT_ANONYMOUS,
            is_anonymous=True,
        )
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)
        self.login_session()

        response = self.client.get(reverse('surveys:preview-v2-home'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-card="health"', count=1)
        self.assertContains(response, 'v2-menu-card--health is-unavailable')
        self.assertContains(response, '오늘은 자가검진이 준비되지 않았습니다.')
        self.assertNotContains(response, reverse('surveys:preview-v2-health-start'))

    def _create_daily_form_with_question(self, *, title='1일차 자가검진', day_no=1):
        form = SurveyForm.objects.create(
            team=self.team,
            day_no=day_no,
            title=title,
            version=1,
            is_active=True,
            form_type=SurveyForm.FormType.DAILY_HEALTH,
        )
        section = SurveySection.objects.create(
            form=form,
            title='신체',
            section_type=SurveySection.SectionType.PHYSICAL,
            display_order=1,
        )
        SurveyQuestion.objects.create(
            section=section,
            key=f'headache_day{day_no}',
            label='두통이 있습니까?',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=['예', '아니오'],
            is_required=True,
            display_order=1,
        )
        return form

    def _create_empty_daily_form(self):
        form = SurveyForm.objects.create(
            team=self.team,
            day_no=1,
            title='1일차 자가검진',
            version=1,
            is_active=True,
            form_type=SurveyForm.FormType.DAILY_HEALTH,
        )
        SurveySection.objects.create(
            form=form,
            title='신체',
            section_type=SurveySection.SectionType.PHYSICAL,
            display_order=1,
        )
        return form

    def test_home_health_card_shows_notice_when_daily_form_has_no_questions(self):
        self._create_empty_daily_form()
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)
        self.login_session()

        response = self.client.get(reverse('surveys:preview-v2-home'))

        self.assertContains(response, 'v2-menu-card--health is-unavailable')
        self.assertContains(response, '오늘은 자가검진이 준비되지 않았습니다.')
        self.assertNotContains(response, reverse('surveys:preview-v2-health-start'))

        english = self.client.get(reverse('surveys:preview-v2-home') + '?lang=en')
        self.assertContains(english, 'health check is not ready yet.')

    def test_health_start_redirects_home_with_notice_when_daily_form_has_no_questions(self):
        self._create_empty_daily_form()
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)
        self.login_session()

        with self.assertLogs('surveys.views', level='WARNING') as logs:
            response = self.client.get(reverse('surveys:preview-v2-health-start'), follow=True)

        self.assertRedirects(response, reverse('surveys:preview-v2-home'))
        self.assertContains(response, 'is-warning')
        self.assertContains(response, '오늘은 자가검진이 준비되지 않았습니다. 운영자에게 문의해 주세요.')
        self.assertIn('reason=no_questions', logs.output[0])

    def test_day_survey_redirects_home_with_notice_when_daily_form_has_no_questions(self):
        form = self._create_empty_daily_form()
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)
        self.login_session()
        day_url = reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?source=v2'

        with self.assertLogs('surveys.views', level='WARNING') as logs:
            response = self.client.get(day_url, follow=True)

        self.assertRedirects(response, reverse('surveys:preview-v2-home'))
        self.assertContains(response, '오늘은 자가검진이 준비되지 않았습니다. 운영자에게 문의해 주세요.')
        self.assertIn('reason=no_questions', logs.output[0])
        self.assertIn(f'form_id={form.id}', logs.output[0])

        async_response = self.client.get(day_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(async_response.status_code, 200)
        self.assertEqual(async_response.json(), {'redirect_url': reverse('surveys:preview-v2-home')})

    def test_day_survey_without_session_redirects_to_code_entry_with_notice(self):
        day_url = reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?source=v2'

        with self.assertLogs('surveys.views', level='WARNING') as logs:
            response = self.client.get(day_url, follow=True)

        self.assertRedirects(response, reverse('surveys:preview-v2-code'))
        self.assertContains(response, 'v2-code-notice')
        self.assertContains(response, '로그인이 만료되었거나 탐방 기간이 아닙니다. 팀 코드를 다시 입력해 주세요.')
        self.assertIn('reason=no_session', logs.output[0])

    def test_day_survey_for_other_day_redirects_with_notice_or_to_result(self):
        self.team.start_date = date.today() - timedelta(days=1)
        self.team.save(update_fields=['start_date', 'updated_at'])
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)
        self.login_session()
        home_url = reverse('surveys:preview-v2-home')

        past = self.client.get(reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?source=v2', follow=True)
        self.assertRedirects(past, home_url)
        self.assertContains(past, '1일차 자가검진은 해당 날짜에만 작성할 수 있습니다. 오늘은 2일차입니다.')

        future = self.client.get(reverse('surveys:day-survey', kwargs={'day_no': 3}) + '?source=v2', follow=True)
        self.assertRedirects(future, home_url)
        self.assertContains(future, '3일차 자가검진은 해당 날짜에만 작성할 수 있습니다. 오늘은 2일차입니다.')

        beyond = self.client.get(reverse('surveys:day-survey', kwargs={'day_no': 4}) + '?source=v2', follow=True)
        self.assertRedirects(beyond, home_url)
        self.assertContains(beyond, '4일차는 이 팀의 탐방 일정에 없습니다.')

        SurveySubmission.objects.create(
            team=self.team,
            respondent=self.respondent,
            day_no=1,
            answers_json={},
            result_json={},
            form_version=1,
            form_snapshot_json={},
            submitted_at=timezone.now(),
        )
        submitted = self.client.get(reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?source=v2')
        self.assertRedirects(
            submitted,
            reverse('surveys:result', kwargs={'day_no': 1})
            + '?'
            + urlencode({'source': 'user', 'return_to': home_url}),
            fetch_redirect_response=False,
        )

    def test_day_survey_notice_uses_english_when_session_language_is_english(self):
        self.team.start_date = date.today() - timedelta(days=1)
        self.team.save(update_fields=['start_date', 'updated_at'])
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)
        self.login_session()

        response = self.client.get(
            reverse('surveys:day-survey', kwargs={'day_no': 1}) + '?source=v2&lang=en',
            follow=True,
        )

        self.assertContains(response, 'The Day 1 health check can only be completed on that day. Today is Day 2.')

    def test_home_replaces_health_and_reflection_prompts_after_submission(self):
        SurveyForm.objects.create(
            team=self.team,
            day_no=1,
            title='1일차 건강관리',
            version=1,
            is_active=True,
            form_type=SurveyForm.FormType.DAILY_HEALTH,
        )
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)
        SurveySubmission.objects.create(
            team=self.team,
            respondent=self.respondent,
            day_no=1,
            answers_json={},
            result_json={},
            form_version=1,
            form_snapshot_json={},
            submitted_at=timezone.now(),
        )
        ReflectionSubmission.objects.create(
            team=self.team,
            respondent=self.respondent,
            content='완료된 소감문',
            submitted_at=timezone.now(),
        )
        self.login_session()

        response = self.client.get(reverse('surveys:preview-v2-home'))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        health_card_start = content.index('data-card="health"')
        health_card_end = content.index('</a>', health_card_start)
        self.assertIn('결과 보기', content[health_card_start:health_card_end])
        reflection_card_start = content.index('data-card="reflection"')
        reflection_card_end = content.index('</a>', reflection_card_start)
        self.assertIn('제출 완료', content[reflection_card_start:reflection_card_end])
        expected_health_result_url = (
            reverse('surveys:result', kwargs={'day_no': 1})
            + '?'
            + urlencode(
                {
                    'source': 'user',
                    'return_to': reverse('surveys:preview-v2-home'),
                }
            )
        )
        self.assertIn(
            f'data-card="health" href="{expected_health_result_url.replace("&", "&amp;")}"',
            content,
        )
        self.assertContains(response, 'check-health.png')
        self.assertContains(response, 'check-reflection.svg')

    def test_home_health_check_is_based_on_current_day_submission(self):
        self.team.start_date = date.today() - timedelta(days=1)
        self.team.save(update_fields=['start_date', 'updated_at'])
        SurveyForm.objects.create(
            team=self.team,
            day_no=2,
            title='2일차 건강관리',
            version=1,
            is_active=True,
            form_type=SurveyForm.FormType.DAILY_HEALTH,
        )
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)
        SurveySubmission.objects.create(
            team=self.team,
            respondent=self.respondent,
            day_no=1,
            answers_json={},
            result_json={},
            form_version=1,
            form_snapshot_json={},
            submitted_at=timezone.now(),
        )
        self.login_session()

        response = self.client.get(reverse('surveys:preview-v2-home'))
        self.assertNotContains(response, 'check-health.png')

        SurveySubmission.objects.create(
            team=self.team,
            respondent=self.respondent,
            day_no=2,
            answers_json={},
            result_json={},
            form_version=1,
            form_snapshot_json={},
            submitted_at=timezone.now(),
        )
        response = self.client.get(reverse('surveys:preview-v2-home'))
        self.assertContains(response, 'check-health.png')

    def test_health_start_connects_home_to_existing_daily_survey(self):
        self._create_daily_form_with_question(title='1일차 건강관리')
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)
        self.login_session()

        start_url = reverse('surveys:preview-v2-health-start')
        home_response = self.client.get(reverse('surveys:preview-v2-home'))
        self.assertContains(
            home_response,
            f'data-card="health" href="{start_url}"',
        )

        response = self.client.get(start_url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'surveys/public_v2/health_start.html')
        self.assertContains(response, '건강관리 설문을 시작합니다')
        self.assertContains(
            response,
            (
                f'href="{reverse("surveys:day-survey", kwargs={"day_no": 1})}'
                '?source=v2"'
            ),
        )
        self.assertContains(response, 'v2-health-start/main-illustration.png')
        self.assertContains(response, 'v2-health-start/heart.png')
        self.assertContains(response, 'v2-health-start/shield-plus.png')
        self.assertContains(response, 'v2-health-start/lock.png')
        self.assertContains(response, 'v2-health-start/clock.png')

    def test_health_start_redirects_completed_respondent_to_result(self):
        SurveyForm.objects.create(
            team=self.team,
            day_no=1,
            title='1일차 건강관리',
            version=1,
            is_active=True,
            form_type=SurveyForm.FormType.DAILY_HEALTH,
        )
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)
        SurveySubmission.objects.create(
            team=self.team,
            respondent=self.respondent,
            day_no=1,
            answers_json={},
            result_json={},
            form_version=1,
            form_snapshot_json={},
            submitted_at=timezone.now(),
        )
        self.login_session()

        expected_url = (
            reverse('surveys:result', kwargs={'day_no': 1})
            + '?'
            + urlencode(
                {
                    'source': 'user',
                    'return_to': reverse('surveys:preview-v2-home'),
                }
            )
        )
        response = self.client.get(reverse('surveys:preview-v2-health-start'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, expected_url)

    def test_health_start_redirects_home_without_daily_health_form(self):
        SurveyForm.objects.create(
            team=self.team,
            day_no=1,
            title='종료 설문',
            version=1,
            is_active=True,
            form_type=SurveyForm.FormType.EXIT_SURVEY,
        )
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)
        self.login_session()

        response = self.client.get(reverse('surveys:preview-v2-health-start'), follow=True)

        self.assertRedirects(response, reverse('surveys:preview-v2-home'))
        self.assertContains(response, '오늘은 자가검진이 준비되지 않았습니다. 운영자에게 문의해 주세요.')

    def test_reflection_page_requires_completed_documents_but_not_feature_approval(self):
        self.login_session()
        reflection_url = reverse('surveys:preview-v2-reflection')
        self.assertRedirects(
            self.client.get(reflection_url),
            reverse('surveys:preview-v2-documents-next'),
            fetch_redirect_response=False,
        )
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)
        self.assertEqual(self.client.get(reflection_url).status_code, 200)

    def test_reflection_page_renders_fields_without_optional_team_image(self):
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)
        self.login_session()

        response = self.client.get(reverse('surveys:preview-v2-reflection'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="affiliation"')
        self.assertContains(response, 'name="country"')
        self.assertContains(response, 'name="title"')
        self.assertContains(response, 'name="content"')
        self.assertContains(response, 'csrfmiddlewaretoken')
        self.assertContains(response, '탐방 프로그램 동안으로')
        self.assertContains(response, reverse('surveys:preview-v2-home'))
        self.assertNotContains(response, 'v2-reflection__image')

    def test_reflection_rejects_short_content_and_accepts_internal_space(self):
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)
        self.login_session()
        reflection_url = reverse('surveys:preview-v2-reflection')
        common = {
            'affiliation': '독도재단',
            'country': '대한민국',
            'title': '탐방 소감',
        }

        response = self.client.post(reflection_url, {**common, 'content': '가' * 9})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '소감문은 10자 이상 입력해 주세요.')
        self.assertFalse(ReflectionSubmission.objects.exists())

        content = ('가' * 5) + ' ' + ('나' * 4)
        response = self.client.post(reflection_url, {**common, 'content': f'  {content}  '})
        self.assertRedirects(
            response,
            reverse(
                'surveys:preview-v2-completion',
                kwargs={'completion_type': 'reflection'},
            ),
            fetch_redirect_response=False,
        )
        submission = ReflectionSubmission.objects.get()
        self.assertEqual(submission.content, content)
        self.assertTrue(submission.is_locked)

    def test_reflection_submission_is_one_time_and_cannot_be_overwritten(self):
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)
        self.login_session()
        reflection_url = reverse('surveys:preview-v2-reflection')
        payload = {
            'affiliation': '독도재단',
            'country': '대한민국',
            'title': '첫 소감문',
            'content': '가' * 40,
        }
        self.client.post(reflection_url, payload)

        response = self.client.post(
            reflection_url,
            {**payload, 'title': '변경된 제목', 'content': '나' * 40},
        )

        self.assertRedirects(
            response,
            reverse(
                'surveys:preview-v2-completion',
                kwargs={'completion_type': 'reflection'},
            ),
            fetch_redirect_response=False,
        )
        self.assertEqual(ReflectionSubmission.objects.count(), 1)
        self.assertEqual(ReflectionSubmission.objects.get().title, '첫 소감문')

    def test_v2_document_completion_requires_submission_and_matches_figma_copy(self):
        self.login_session()
        completion_url = reverse(
            'surveys:preview-v2-completion',
            kwargs={'completion_type': 'documents'},
        )
        self.assertRedirects(
            self.client.get(completion_url),
            reverse('surveys:preview-v2-documents-next'),
            fetch_redirect_response=False,
        )
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)

        response = self.client.get(completion_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '개인정보동의서')
        self.assertContains(response, '제출이 완료되었습니다!')
        self.assertContains(response, '동의서 등 작성 및 제출이')
        self.assertContains(response, '#9f7df3')
        self.assertContains(response, reverse('surveys:preview-v2-home'))
        self.assertContains(response, 'v2-completion/document-base.svg')

    def test_v2_health_completion_requires_current_day_submission_and_links_result(self):
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)
        self.login_session()
        completion_url = reverse(
            'surveys:preview-v2-completion',
            kwargs={'completion_type': 'health'},
        )
        self.assertRedirects(
            self.client.get(completion_url),
            reverse('surveys:preview-v2-home'),
            fetch_redirect_response=False,
        )
        SurveySubmission.objects.create(
            team=self.team,
            respondent=self.respondent,
            day_no=1,
            answers_json={},
            result_json={},
            form_version=1,
            form_snapshot_json={},
            submitted_at=timezone.now(),
        )

        response = self.client.get(completion_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '자가 검진')
        self.assertContains(response, '자가 검진 확인서 작성 및 제출이')
        self.assertContains(response, '결과 보기')
        self.assertContains(
            response,
            reverse('surveys:result', kwargs={'day_no': 1}),
        )
        self.assertContains(response, '#84d895')

    def test_v2_reflection_completion_requires_locked_submission(self):
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)
        self.login_session()
        completion_url = reverse(
            'surveys:preview-v2-completion',
            kwargs={'completion_type': 'reflection'},
        )
        self.assertRedirects(
            self.client.get(completion_url),
            reverse('surveys:preview-v2-home'),
            fetch_redirect_response=False,
        )
        ReflectionSubmission.objects.create(
            team=self.team,
            respondent=self.respondent,
            content='완료된 소감문',
            submitted_at=timezone.now(),
        )

        response = self.client.get(completion_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '소감문작성')
        self.assertContains(response, '소감문 작성 및 제출이')
        self.assertContains(response, '#ff9230')
        self.assertContains(response, 'v2-completion/reflection.svg')

    def test_v2_anonymous_survey_completion_does_not_infer_personal_completion(self):
        for document_type in ('privacy_consent', 'third_party_consent', 'safety_pledge'):
            self.submit_document(document_type)
        self.login_session()

        response = self.client.get(
            reverse(
                'surveys:preview-v2-completion',
                kwargs={'completion_type': 'survey'},
            ),
        )

        self.assertRedirects(
            response,
            reverse('surveys:preview-v2-home'),
            fetch_redirect_response=False,
        )

    def test_daily_survey_is_gated_until_required_documents_complete(self):
        form = SurveyForm.objects.create(team=self.team, day_no=1, title='1일차', version=1, is_active=True)
        self.login_session()
        response = self.client.get(reverse('surveys:day-survey', kwargs={'day_no': 1}))
        self.assertRedirects(
            response,
            reverse('surveys:preview-v2-documents-next'),
            fetch_redirect_response=False,
        )


class RequiredDocumentConsentEvidenceTest(TestCase):
    def setUp(self):
        cache.clear()
        self.team = Team.objects.create(
            name='1차 원정대',
            organization_name='독도재단',
            organization_name_en='DOKDO FOUNDATION',
            code='EVIDENCE',
            is_active=True,
            start_date=date.today(),
            max_day_no=3,
        )
        self.respondent = Respondent.objects.create(
            team=self.team,
            name='홍길동',
            birth_date=date(1990, 1, 2),
            phone='01012345678',
            gender='M',
            normalized_name='홍길동',
            normalized_phone='01012345678',
        )
        self.features = TeamFeatureSetting.objects.create(
            team=self.team,
            enable_privacy_consent=True,
            enable_third_party_consent=True,
            enable_safety_pledge=True,
        )

    def login_session(self):
        session = self.client.session
        session['survey_team_id'] = self.team.id
        session['survey_team_code'] = self.team.code
        session['survey_respondent_id'] = self.respondent.id
        session['survey_day_no'] = 1
        session.save()

    def document_url(self, document_type='privacy_consent'):
        return reverse(
            'surveys:preview-v2-document',
            kwargs={'document_type': document_type},
        )

    def post_consent(self, document_type, *, finalize=False, url=None, **extra):
        return self.client.post(
            url or self.document_url(),
            {
                'action': 'finalize' if finalize else 'record_consent',
                'document_type': document_type,
                'agreed': 'on',
                **extra,
            },
        )

    def changed_evidence_renderer(self, target_type, *, version=None):
        def renderer(*, team, document_type, language='ko'):
            evidence = build_canonical_document_evidence(
                team=team,
                document_type=document_type,
                language=language,
            )
            if document_type != target_type:
                return evidence
            changed = dict(evidence)
            if version is not None:
                changed['version'] = version
            else:
                changed['content'] = f"{evidence['content']}\n<p>변경된 문구</p>"
                changed['content_hash'] = hashlib.sha256(
                    changed['content'].encode('utf-8')
                ).hexdigest()
            return changed
        return renderer

    def test_consent_session_fingerprint_matches_full_canonical_content(self):
        self.login_session()
        self.client.get(self.document_url())
        response = self.post_consent('privacy_consent')
        self.assertEqual(response.status_code, 200)
        consent = self.client.session[SESSION_REQUIRED_DOCUMENT_CONSENTS]['consents']['privacy_consent']
        evidence = build_canonical_document_evidence(
            team=self.team,
            document_type='privacy_consent',
        )
        self.assertEqual(consent['document_type'], 'privacy_consent')
        self.assertEqual(consent['document_title'], evidence['title'])
        self.assertEqual(consent['document_version'], evidence['version'])
        self.assertEqual(consent['document_content_hash'], evidence['content_hash'])
        self.assertEqual(
            consent['document_content_hash'],
            hashlib.sha256(evidence['content'].encode('utf-8')).hexdigest(),
        )
        self.assertNotIn('content', consent)

    def test_final_snapshots_match_session_fingerprints(self):
        self.login_session()
        self.client.get(self.document_url())
        self.post_consent('privacy_consent')
        self.post_consent('third_party_consent')
        last_record = self.post_consent('safety_pledge')
        self.assertEqual(last_record.status_code, 409)
        state = self.client.session[SESSION_REQUIRED_DOCUMENT_CONSENTS]
        session_hashes = {
            document_type: consent['document_content_hash']
            for document_type, consent in state['consents'].items()
        }
        final = self.post_consent('safety_pledge', finalize=True)
        self.assertEqual(final.status_code, 200, final.content)
        self.assertEqual(
            dict(DocumentSubmission.objects.values_list(
                'document_type',
                'document_content_hash_at_submit',
            )),
            session_hashes,
        )

    def test_final_service_revalidates_canonical_fingerprint_inside_transaction(self):
        evidence_by_type = {
            document_type: build_canonical_document_evidence(
                team=self.team,
                document_type=document_type,
            )
            for document_type in (
                'privacy_consent',
                'third_party_consent',
                'safety_pledge',
            )
        }
        agreed_at = timezone.now()
        self.team.organization_name = 'transaction 직전 변경 기관'
        self.team.organization_name_en = 'CHANGED BEFORE TRANSACTION'
        self.team.save(update_fields=['organization_name', 'organization_name_en', 'updated_at'])
        with self.assertRaises(RequiredDocumentEvidenceChanged):
            create_required_document_submissions_from_consents(
                team=self.team,
                respondent=self.respondent,
                agreed_at_by_document_type={
                    document_type: agreed_at
                    for document_type in evidence_by_type
                },
                expected_fingerprints_by_document_type={
                    document_type: {
                        'document_type': document_type,
                        'document_version': evidence['version'],
                        'document_content_hash': evidence['content_hash'],
                    }
                    for document_type, evidence in evidence_by_type.items()
                },
            )
        self.assertFalse(DocumentSubmission.objects.exists())
        self.assertFalse(RequiredDocumentConsentBundle.objects.exists())

    def test_final_service_reloads_feature_settings_inside_transaction(self):
        evidence_by_type = {
            document_type: build_canonical_document_evidence(
                team=self.team,
                document_type=document_type,
            )
            for document_type in (
                'privacy_consent',
                'third_party_consent',
                'safety_pledge',
            )
        }
        self.features.enable_third_party_consent = False
        self.features.save(update_fields=['enable_third_party_consent', 'updated_at'])
        with self.assertRaises(RequiredDocumentTemplateInvalid):
            create_required_document_submissions_from_consents(
                team=self.team,
                respondent=self.respondent,
                agreed_at_by_document_type={
                    document_type: timezone.now()
                    for document_type in evidence_by_type
                },
                expected_fingerprints_by_document_type={
                    document_type: {
                        'document_type': document_type,
                        'document_version': evidence['version'],
                        'document_content_hash': evidence['content_hash'],
                    }
                    for document_type, evidence in evidence_by_type.items()
                },
            )
        self.assertFalse(DocumentSubmission.objects.exists())
        self.assertFalse(RequiredDocumentConsentBundle.objects.exists())

    def test_final_service_does_not_misreport_unrelated_integrity_error_as_duplicate(self):
        with patch(
            'surveys.required_documents.DocumentSubmission.objects.create',
            side_effect=IntegrityError('template_id violates not-null constraint'),
        ):
            with self.assertRaises(IntegrityError):
                create_required_document_submissions(
                    team=self.team,
                    respondent=self.respondent,
                )
        self.assertFalse(DocumentSubmission.objects.exists())
        self.assertFalse(RequiredDocumentConsentBundle.objects.exists())

    def test_repeated_final_service_call_is_idempotent_after_all_documents_exist(self):
        document_types = (
            'privacy_consent',
            'third_party_consent',
            'safety_pledge',
        )
        evidence_by_type = {
            document_type: build_canonical_document_evidence(
                team=self.team,
                document_type=document_type,
            )
            for document_type in document_types
        }
        agreed_at_by_type = {
            document_type: timezone.now()
            for document_type in document_types
        }
        fingerprints = {
            document_type: {
                'document_type': document_type,
                'document_version': evidence['version'],
                'document_content_hash': evidence['content_hash'],
            }
            for document_type, evidence in evidence_by_type.items()
        }
        first = create_required_document_submissions_from_consents(
            team=self.team,
            respondent=self.respondent,
            agreed_at_by_document_type=agreed_at_by_type,
            expected_fingerprints_by_document_type=fingerprints,
        )
        second = create_required_document_submissions_from_consents(
            team=self.team,
            respondent=self.respondent,
            agreed_at_by_document_type=agreed_at_by_type,
            expected_fingerprints_by_document_type=fingerprints,
        )
        self.assertEqual(len(first), 3)
        self.assertEqual(second, [])
        self.assertEqual(DocumentSubmission.objects.count(), 3)
        self.assertEqual(RequiredDocumentConsentBundle.objects.count(), 1)

    def test_concurrent_completion_is_returned_as_success_instead_of_409(self):
        self.login_session()
        self.client.get(self.document_url())
        self.post_consent('privacy_consent')
        self.post_consent('third_party_consent')

        def complete_then_report_duplicate(**kwargs):
            create_required_document_submissions(
                team=self.team,
                respondent=self.respondent,
            )
            raise RequiredDocumentAlreadySubmitted('동시에 완료된 요청')

        with patch(
            'surveys.views.create_required_document_submissions_from_consents',
            side_effect=complete_then_report_duplicate,
        ):
            response = self.post_consent('safety_pledge', finalize=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['completed'])
        self.assertEqual(
            response.json()['redirect_url'],
            reverse(
                'surveys:preview-v2-completion',
                kwargs={'completion_type': 'documents'},
            ),
        )
        self.assertEqual(DocumentSubmission.objects.count(), 3)
        self.assertNotIn(SESSION_REQUIRED_DOCUMENT_CONSENTS, self.client.session)

    def test_privacy_change_clears_all_temporary_consents(self):
        self.login_session()
        self.client.get(self.document_url())
        self.post_consent('privacy_consent')
        self.post_consent('third_party_consent')
        with patch(
            'surveys.views.build_canonical_document_evidence',
            side_effect=self.changed_evidence_renderer('privacy_consent'),
        ):
            response = self.post_consent('safety_pledge', finalize=True)
        self.assertEqual(response.status_code, 409)
        self.assertIn('동의서 내용이 변경', response.json()['error'])
        self.assertEqual(
            self.client.session[SESSION_REQUIRED_DOCUMENT_CONSENTS]['consents'],
            {},
        )
        self.assertFalse(DocumentSubmission.objects.exists())

    def test_third_party_change_keeps_only_privacy_consent(self):
        self.login_session()
        self.client.get(self.document_url())
        self.post_consent('privacy_consent')
        self.post_consent('third_party_consent')
        with patch(
            'surveys.views.build_canonical_document_evidence',
            side_effect=self.changed_evidence_renderer('third_party_consent'),
        ):
            response = self.post_consent('safety_pledge', finalize=True)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            set(self.client.session[SESSION_REQUIRED_DOCUMENT_CONSENTS]['consents']),
            {'privacy_consent'},
        )
        self.assertFalse(DocumentSubmission.objects.exists())

    def test_safety_change_keeps_prior_two_consents(self):
        self.login_session()
        self.client.get(self.document_url())
        self.post_consent('privacy_consent')
        self.post_consent('third_party_consent')
        self.post_consent('safety_pledge')
        with patch(
            'surveys.views.build_canonical_document_evidence',
            side_effect=self.changed_evidence_renderer('safety_pledge'),
        ):
            response = self.post_consent('safety_pledge', finalize=True)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            set(self.client.session[SESSION_REQUIRED_DOCUMENT_CONSENTS]['consents']),
            {'privacy_consent', 'third_party_consent'},
        )
        self.assertFalse(DocumentSubmission.objects.exists())

    def test_organization_name_change_requires_reconsent(self):
        self.login_session()
        self.client.get(self.document_url())
        self.post_consent('privacy_consent')
        self.team.organization_name = '변경된 기관'
        self.team.organization_name_en = 'CHANGED ORGANIZATION'
        self.team.save(update_fields=['organization_name', 'organization_name_en', 'updated_at'])
        response = self.post_consent('third_party_consent')
        self.assertEqual(response.status_code, 409)
        self.assertIn('동의서 내용이 변경', response.json()['error'])
        self.assertEqual(
            self.client.session[SESSION_REQUIRED_DOCUMENT_CONSENTS]['consents'],
            {},
        )

    def test_document_version_change_requires_reconsent(self):
        self.login_session()
        self.client.get(self.document_url())
        self.post_consent('privacy_consent')
        with patch(
            'surveys.views.build_canonical_document_evidence',
            side_effect=self.changed_evidence_renderer(
                'privacy_consent',
                version=STATIC_REQUIRED_DOCUMENT_VERSION + 1,
            ),
        ):
            response = self.post_consent('third_party_consent')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            self.client.session[SESSION_REQUIRED_DOCUMENT_CONSENTS]['consents'],
            {},
        )
        self.assertFalse(DocumentSubmission.objects.exists())

    def test_expired_temporary_flow_is_reset_without_database_submission(self):
        self.login_session()
        self.client.get(self.document_url())
        self.post_consent('privacy_consent')
        session = self.client.session
        state = session[SESSION_REQUIRED_DOCUMENT_CONSENTS]
        expired_time = timezone.now() - timedelta(hours=2)
        state['started_at'] = expired_time.isoformat()
        state['last_updated_at'] = expired_time.isoformat()
        state['consents']['privacy_consent']['agreed_at'] = expired_time.isoformat()
        session[SESSION_REQUIRED_DOCUMENT_CONSENTS] = state
        session.save()
        with self.settings(REQUIRED_DOCUMENT_CONSENT_SESSION_MAX_AGE_SECONDS=3600):
            response = self.post_consent('third_party_consent')
        self.assertEqual(response.status_code, 409)
        self.assertIn('만료', response.json()['error'])
        self.assertEqual(
            self.client.session[SESSION_REQUIRED_DOCUMENT_CONSENTS]['consents'],
            {},
        )
        self.assertFalse(DocumentSubmission.objects.exists())

    def test_future_naive_and_reversed_consent_times_are_rejected(self):
        invalid_mutators = {
            'future': lambda state: state['consents']['privacy_consent'].__setitem__(
                'agreed_at',
                (timezone.now() + timedelta(hours=1)).isoformat(),
            ),
            'naive': lambda state: state['consents']['privacy_consent'].__setitem__(
                'agreed_at',
                timezone.now().replace(tzinfo=None).isoformat(),
            ),
            'reversed': self._reverse_consent_times,
        }
        for label, mutate in invalid_mutators.items():
            with self.subTest(label=label):
                self.client.cookies.clear()
                self.login_session()
                self.client.get(self.document_url())
                self.post_consent('privacy_consent')
                if label == 'reversed':
                    self.post_consent('third_party_consent')
                session = self.client.session
                state = session[SESSION_REQUIRED_DOCUMENT_CONSENTS]
                mutate(state)
                session[SESSION_REQUIRED_DOCUMENT_CONSENTS] = state
                session.save()
                response = self.post_consent(
                    'safety_pledge' if label == 'reversed' else 'third_party_consent'
                )
                self.assertEqual(response.status_code, 409)
                self.assertFalse(DocumentSubmission.objects.exists())

    def _reverse_consent_times(self, state):
        started_at = timezone.now() - timedelta(seconds=30)
        first_time = started_at + timedelta(seconds=20)
        second_time = started_at + timedelta(seconds=10)
        state['started_at'] = started_at.isoformat()
        state['consents']['privacy_consent']['agreed_at'] = first_time.isoformat()
        state['consents']['third_party_consent']['agreed_at'] = second_time.isoformat()
        state['last_updated_at'] = timezone.now().isoformat()

    def test_server_session_records_each_consent_with_independent_server_time(self):
        self.login_session()
        self.client.get(self.document_url())
        first_time = timezone.now()
        second_time = first_time + timedelta(seconds=30)
        third_time = second_time + timedelta(seconds=60)
        with patch('surveys.views._required_document_server_now', return_value=first_time):
            first = self.post_consent(
                'privacy_consent',
                agreed_at='1999-01-01T00:00:00+00:00',
            )
        self.assertEqual(first.status_code, 200)
        state = self.client.session[SESSION_REQUIRED_DOCUMENT_CONSENTS]
        self.assertEqual(list(state['consents']), ['privacy_consent'])
        self.assertEqual(
            datetime.fromisoformat(state['consents']['privacy_consent']['agreed_at']),
            first_time,
        )
        self.assertFalse(DocumentSubmission.objects.exists())

        with patch('surveys.views._required_document_server_now', return_value=second_time):
            second = self.post_consent('third_party_consent')
        self.assertEqual(second.status_code, 200)
        state = self.client.session[SESSION_REQUIRED_DOCUMENT_CONSENTS]
        self.assertEqual(
            list(state['consents']),
            ['privacy_consent', 'third_party_consent'],
        )
        self.assertEqual(
            datetime.fromisoformat(state['consents']['third_party_consent']['agreed_at']),
            second_time,
        )
        self.assertFalse(DocumentSubmission.objects.exists())

        with patch('surveys.views._required_document_server_now', return_value=third_time):
            final = self.post_consent('safety_pledge', finalize=True)
        self.assertEqual(final.status_code, 200)
        self.assertTrue(final.json()['completed'])
        self.assertNotIn(SESSION_REQUIRED_DOCUMENT_CONSENTS, self.client.session)
        agreed_times = dict(
            DocumentSubmission.objects.values_list('document_type', 'agreed_at')
        )
        self.assertEqual(agreed_times['privacy_consent'], first_time)
        self.assertEqual(agreed_times['third_party_consent'], second_time)
        self.assertEqual(agreed_times['safety_pledge'], third_time)
        self.assertIsNotNone(RequiredDocumentConsentBundle.objects.get().completed_at)

    def test_session_contains_only_minimum_ids_document_types_and_server_consents(self):
        self.login_session()
        self.client.get(self.document_url())
        self.post_consent('privacy_consent')
        state = self.client.session[SESSION_REQUIRED_DOCUMENT_CONSENTS]
        self.assertEqual(
            set(state),
            {
                'team_id',
                'respondent_id',
                'language',
                'enabled_document_types',
                'started_at',
                'last_updated_at',
                'consents',
            },
        )
        self.assertEqual(
            set(state['consents']['privacy_consent']),
            {
                'document_type',
                'language',
                'document_title',
                'document_version',
                'document_content_hash',
                'agreed',
                'agreed_at',
            },
        )
        serialized = json.dumps(state, ensure_ascii=False).lower()
        for forbidden in (
            self.respondent.name.lower(),
            self.respondent.phone,
            self.respondent.birth_date.isoformat(),
            'gender',
            'normalized',
            '주민등록',
            'passport',
            'ip_address',
            'user_agent',
            'session_key',
        ):
            self.assertNotIn(forbidden, serialized)

    def test_refresh_and_next_resolver_resume_from_server_session(self):
        self.login_session()
        self.client.get(self.document_url())
        self.post_consent('privacy_consent')
        response = self.client.get(reverse('surveys:preview-v2-documents-next'))
        self.assertRedirects(
            response,
            self.document_url('third_party_consent'),
            fetch_redirect_response=False,
        )
        page = self.client.get(response.url)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'data-document-consent="privacy_consent"')
        self.assertContains(page, 'data-document-consent="third_party_consent"')
        self.assertFalse(DocumentSubmission.objects.exists())

    def test_order_disabled_duplicate_and_document_type_manipulation_are_blocked(self):
        self.login_session()
        self.client.get(self.document_url())
        for document_type in ('third_party_consent', 'safety_pledge', 'forged'):
            with self.subTest(document_type=document_type):
                response = self.post_consent(document_type)
                self.assertEqual(response.status_code, 409)
        first = self.post_consent('privacy_consent')
        self.assertEqual(first.status_code, 200)
        duplicate = self.post_consent('privacy_consent')
        self.assertEqual(duplicate.status_code, 409)
        self.features.enable_third_party_consent = False
        self.features.save(update_fields=['enable_third_party_consent', 'updated_at'])
        changed = self.post_consent('third_party_consent')
        self.assertEqual(changed.status_code, 409)
        self.assertIn('처음부터 다시', changed.json()['error'])
        reset_state = self.client.session[SESSION_REQUIRED_DOCUMENT_CONSENTS]
        self.assertEqual(reset_state['enabled_document_types'], ['privacy_consent', 'safety_pledge'])
        self.assertEqual(reset_state['consents'], {})
        self.assertFalse(DocumentSubmission.objects.exists())

    def test_mismatched_team_or_respondent_session_state_is_reset(self):
        self.login_session()
        session = self.client.session
        session[SESSION_REQUIRED_DOCUMENT_CONSENTS] = {
            'team_id': self.team.id + 999,
            'respondent_id': self.respondent.id + 999,
            'enabled_document_types': ['privacy_consent'],
            'consents': {},
        }
        session.save()
        response = self.post_consent('privacy_consent')
        self.assertEqual(response.status_code, 409)
        state = self.client.session[SESSION_REQUIRED_DOCUMENT_CONSENTS]
        self.assertEqual(state['team_id'], self.team.id)
        self.assertEqual(state['respondent_id'], self.respondent.id)
        self.assertEqual(state['consents'], {})

    def test_final_transaction_failure_keeps_server_session_for_retry(self):
        self.login_session()
        self.client.get(self.document_url())
        self.post_consent('privacy_consent')
        self.post_consent('third_party_consent')
        with patch(
            'surveys.views.create_required_document_submissions_from_consents',
            side_effect=DatabaseError('write failed'),
        ):
            response = self.post_consent('safety_pledge', finalize=True)
        self.assertEqual(response.status_code, 500)
        state = self.client.session[SESSION_REQUIRED_DOCUMENT_CONSENTS]
        self.assertEqual(
            set(state['consents']),
            {'privacy_consent', 'third_party_consent', 'safety_pledge'},
        )
        self.assertFalse(DocumentSubmission.objects.exists())
        retry = self.post_consent('safety_pledge', finalize=True)
        self.assertEqual(retry.status_code, 200)
        self.assertTrue(retry.json()['completed'])

    def test_disabled_middle_document_is_skipped_in_session_and_database(self):
        self.features.enable_third_party_consent = False
        self.features.save(update_fields=['enable_third_party_consent', 'updated_at'])
        self.login_session()
        self.client.get(self.document_url())
        first = self.post_consent('privacy_consent')
        self.assertEqual(first.json()['next_document_type'], 'safety_pledge')
        final = self.post_consent('safety_pledge', finalize=True)
        self.assertTrue(final.json()['completed'])
        bundle = RequiredDocumentConsentBundle.objects.get()
        self.assertEqual(
            bundle.enabled_document_types_snapshot,
            ['privacy_consent', 'safety_pledge'],
        )
        self.assertEqual(
            set(bundle.submissions.values_list('document_type', flat=True)),
            {'privacy_consent', 'safety_pledge'},
        )

    def test_admin_deleted_document_only_is_recorded_and_recreated(self):
        create_required_document_submissions(team=self.team, respondent=self.respondent)
        bundle = RequiredDocumentConsentBundle.objects.get()
        existing = {
            submission.document_type: (
                submission.id,
                submission.agreed_at,
                submission.document_content_hash_at_submit,
            )
            for submission in bundle.submissions.all()
        }
        bundle.submissions.get(document_type='third_party_consent').delete()
        self.login_session()
        page = self.client.get(self.document_url('third_party_consent'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'data-document-consent="third_party_consent"')
        recorded = self.post_consent(
            'third_party_consent',
            url=self.document_url('third_party_consent'),
        )
        self.assertEqual(recorded.status_code, 409)
        with patch(
            'surveys.views.build_canonical_document_evidence',
            side_effect=self.changed_evidence_renderer('third_party_consent'),
        ):
            changed = self.post_consent(
                'third_party_consent',
                finalize=True,
                url=self.document_url('third_party_consent'),
            )
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(bundle.submissions.count(), 2)
        for document_type in ('privacy_consent', 'safety_pledge'):
            submission = bundle.submissions.get(document_type=document_type)
            self.assertEqual(
                (
                    submission.id,
                    submission.agreed_at,
                    submission.document_content_hash_at_submit,
                ),
                existing[document_type],
            )
        final = self.post_consent(
            'third_party_consent',
            finalize=True,
            url=self.document_url('third_party_consent'),
        )
        self.assertTrue(final.json()['completed'])
        bundle.refresh_from_db()
        self.assertIsNotNone(bundle.completed_at)
        self.assertEqual(bundle.submissions.count(), 3)
        for document_type in ('privacy_consent', 'safety_pledge'):
            submission = bundle.submissions.get(document_type=document_type)
            self.assertEqual(
                (
                    submission.id,
                    submission.agreed_at,
                    submission.document_content_hash_at_submit,
                ),
                existing[document_type],
            )

    def test_snapshot_builders_use_only_whitelisted_fields(self):
        self.assertEqual(
            build_participant_snapshot(self.respondent),
            {
                'name': '홍길동',
                'birth_date': '1990-01-02',
                'phone': '01012345678',
                'gender': 'M',
            },
        )
        self.assertEqual(
            build_team_snapshot(self.team),
            {
                'team_id': self.team.id,
                'team_name': '1차 원정대',
                'organization_name': '독도재단',
                'organization_name_en': 'DOKDO FOUNDATION',
            },
        )
        self.assertEqual(
            build_enabled_document_types_snapshot(self.team),
            ['privacy_consent', 'third_party_consent', 'safety_pledge'],
        )
        serialized = json.dumps(build_participant_snapshot(self.respondent), ensure_ascii=False).lower()
        for forbidden in (
            'normalized',
            'code',
            'session',
            'resident',
            'passport',
            '주민등록번호',
            '국적',
            '주소',
            '소속',
            '이메일',
        ):
            self.assertNotIn(forbidden, serialized)

    def test_canonical_document_contains_rendered_organizations_and_exact_hash(self):
        title, content = render_canonical_document(
            team=self.team,
            document_type=DocumentTemplate.DocumentType.PRIVACY_CONSENT,
        )
        self.assertEqual(title, '개인정보 수집·이용 동의서')
        self.assertIn('독도재단', content)
        self.assertNotIn('DOKDO FOUNDATION', content)
        self.assertEqual(
            get_document_content_hash(content),
            hashlib.sha256(content.encode('utf-8')).hexdigest(),
        )
        title_en, content_en = render_canonical_document(
            team=self.team,
            document_type=DocumentTemplate.DocumentType.PRIVACY_CONSENT,
            language='en',
        )
        self.assertEqual(
            title_en,
            'Consent to Collection and Use of Personal Information',
        )
        self.assertIn('DOKDO FOUNDATION', content_en)
        self.assertNotIn('독도재단', content_en)

    def test_english_document_page_shows_only_english_document_copy(self):
        self.login_session()
        response = self.client.get(self.document_url(), {'lang': 'en'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'Consent to Collection and Use of Personal Information',
        )
        self.assertContains(response, 'DOKDO FOUNDATION')
        self.assertContains(response, 'v2-document--en')
        self.assertNotContains(response, '개인정보 수집·이용 동의서')
        self.assertNotContains(response, '독도재단 탐방사업 정보 활용')
        self.assertEqual(self.client.session[SESSION_PUBLIC_LANGUAGE], 'en')

    def test_submitted_document_snapshot_matches_selected_language(self):
        self.login_session()
        self.client.get(self.document_url(), {'lang': 'en'})
        self.post_consent('privacy_consent')
        self.post_consent('third_party_consent')
        final = self.post_consent('safety_pledge', finalize=True)

        self.assertEqual(final.status_code, 200)
        privacy = DocumentSubmission.objects.get(document_type='privacy_consent')
        self.assertEqual(
            privacy.document_title_snapshot,
            'Consent to Collection and Use of Personal Information',
        )
        self.assertIn('DOKDO FOUNDATION', privacy.document_content_snapshot)
        self.assertNotIn('독도재단 탐방사업 정보 활용', privacy.document_content_snapshot)

    def test_live_submit_creates_one_completed_bundle_and_evidence_rows(self):
        submissions = create_required_document_submissions(team=self.team, respondent=self.respondent)
        self.assertEqual(len(submissions), 3)
        bundle = RequiredDocumentConsentBundle.objects.get()
        self.assertIsNotNone(bundle.bundle_uuid)
        self.assertIsNotNone(bundle.completed_at)
        self.assertEqual(bundle.participant_snapshot_json, build_participant_snapshot(self.respondent))
        self.assertEqual(bundle.team_snapshot_json, build_team_snapshot(self.team))
        self.assertEqual(bundle.submissions.count(), 3)
        for submission in bundle.submissions.all():
            self.assertTrue(submission.agreed)
            self.assertEqual(submission.signature_method, DocumentSubmission.SignatureMethod.CHECKBOX)
            self.assertEqual(submission.snapshot_source, DocumentSubmission.SnapshotSource.LIVE_SUBMIT)
            self.assertTrue(submission.document_title_snapshot)
            self.assertTrue(submission.document_content_snapshot)
            self.assertEqual(
                submission.document_content_hash_at_submit,
                hashlib.sha256(submission.document_content_snapshot.encode('utf-8')).hexdigest(),
            )
            self.assertIsNone(submission.ip_address)
            self.assertEqual(submission.user_agent, '')

    def test_snapshots_remain_unchanged_after_current_data_changes(self):
        create_required_document_submissions(team=self.team, respondent=self.respondent)
        bundle = RequiredDocumentConsentBundle.objects.get()
        evidence = list(
            bundle.submissions.values_list(
                'document_title_snapshot',
                'document_content_snapshot',
                'document_content_hash_at_submit',
            )
        )
        participant_snapshot = bundle.participant_snapshot_json.copy()
        team_snapshot = bundle.team_snapshot_json.copy()
        self.respondent.name = '변경된 이름'
        self.respondent.phone = '01000000000'
        self.respondent.save(update_fields=['name', 'phone', 'updated_at'])
        self.team.name = '변경된 팀'
        self.team.organization_name = '변경된 기관'
        self.team.organization_name_en = 'CHANGED'
        self.team.save(update_fields=['name', 'organization_name', 'organization_name_en', 'updated_at'])
        bundle.refresh_from_db()
        self.assertEqual(bundle.participant_snapshot_json, participant_snapshot)
        self.assertEqual(bundle.team_snapshot_json, team_snapshot)
        self.assertEqual(
            list(bundle.submissions.values_list(
                'document_title_snapshot',
                'document_content_snapshot',
                'document_content_hash_at_submit',
            )),
            evidence,
        )

    def test_invalid_checkbox_values_do_not_create_evidence(self):
        self.login_session()
        url = reverse(
            'surveys:preview-v2-document',
            kwargs={'document_type': DocumentTemplate.DocumentType.PRIVACY_CONSENT},
        )
        for value in ('true', '1', 'yes', ''):
            with self.subTest(value=value):
                response = self.client.post(
                    url,
                    {
                        'action': 'record_consent',
                        'document_type': 'privacy_consent',
                        'agreed': value,
                    },
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()['error'], '동의 또는 서약 확인이 필요합니다.')
                self.assertFalse(DocumentSubmission.objects.exists())
                self.assertFalse(RequiredDocumentConsentBundle.objects.exists())

    def test_transaction_failure_rolls_back_bundle_and_submissions(self):
        with patch(
            'surveys.required_documents.DocumentSubmission.objects.create',
            side_effect=RuntimeError('write failed'),
        ):
            with self.assertRaises(RuntimeError):
                create_required_document_submissions(team=self.team, respondent=self.respondent)
        self.assertFalse(RequiredDocumentConsentBundle.objects.exists())
        self.assertFalse(DocumentSubmission.objects.exists())

    def test_delete_one_submission_reopens_and_reuses_bundle(self):
        create_required_document_submissions(team=self.team, respondent=self.respondent)
        bundle = RequiredDocumentConsentBundle.objects.get()
        original_uuid = bundle.bundle_uuid
        bundle.submissions.get(document_type='privacy_consent').delete()
        bundle.refresh_from_db()
        self.assertIsNone(bundle.completed_at)
        self.assertEqual(bundle.submissions.count(), 2)
        recreated = create_required_document_submissions(team=self.team, respondent=self.respondent)
        self.assertEqual([item.document_type for item in recreated], ['privacy_consent'])
        bundle.refresh_from_db()
        self.assertEqual(bundle.bundle_uuid, original_uuid)
        self.assertIsNotNone(bundle.completed_at)
        self.assertEqual(bundle.submissions.count(), 3)

    def test_backfill_uses_template_content_and_marks_static_content_unavailable(self):
        template = DocumentTemplate.objects.create(
            team=self.team,
            document_type='privacy_consent',
            title='기존 동의서',
            content_html='<p>기존 본문</p>\r\n',
            version=2,
            is_active=True,
        )
        templated = DocumentSubmission.objects.create(
            team=self.team,
            respondent=self.respondent,
            template=template,
            document_type='privacy_consent',
            template_version=2,
            template_hash_at_submit=template.body_hash,
            signer_name=self.respondent.name,
            signature_method='checkbox',
            agreed_at=timezone.now(),
        )
        other = Respondent.objects.create(
            team=self.team,
            name='김참가',
            birth_date=date(1991, 2, 3),
            phone='01099998888',
            gender='F',
            normalized_name='김참가',
            normalized_phone='01099998888',
        )
        static = DocumentSubmission.objects.create(
            team=self.team,
            respondent=other,
            template=None,
            document_type='safety_pledge',
            template_version=1,
            template_hash_at_submit='0' * 64,
            signer_name=other.name,
            signature_method='checkbox',
            agreed_at=timezone.now(),
        )
        migration = importlib.import_module(
            'surveys.migrations.0008_required_document_consent_evidence'
        )
        migration.backfill_consent_evidence(django_apps, None)
        templated.refresh_from_db()
        static.refresh_from_db()
        self.assertEqual(templated.snapshot_source, 'legacy_template_backfill')
        self.assertEqual(templated.document_title_snapshot, '기존 동의서')
        self.assertEqual(templated.document_content_snapshot, '<p>기존 본문</p>')
        self.assertEqual(
            templated.document_content_hash_at_submit,
            hashlib.sha256('<p>기존 본문</p>'.encode('utf-8')).hexdigest(),
        )
        self.assertEqual(static.snapshot_source, 'unavailable')
        self.assertEqual(static.document_content_snapshot, '')
        self.assertNotEqual(templated.bundle_id, static.bundle_id)
        first_bundle_ids = (templated.bundle_id, static.bundle_id)
        migration.backfill_consent_evidence(django_apps, None)
        templated.refresh_from_db()
        static.refresh_from_db()
        self.assertEqual((templated.bundle_id, static.bundle_id), first_bundle_ids)
        self.assertEqual(RequiredDocumentConsentBundle.objects.count(), 2)

    def test_admin_scopes_detail_permissions_and_hides_forbidden_fields(self):
        create_required_document_submissions(team=self.team, respondent=self.respondent)
        submission = DocumentSubmission.objects.first()
        User = get_user_model()
        staff = User.objects.create_user(username='team-reader', password='pw', is_staff=True)
        other_staff = User.objects.create_user(username='other-reader', password='pw', is_staff=True)
        AdminTeamPermission.objects.create(user=staff, team=self.team)
        request = RequestFactory().get('/admin/surveys/documentsubmission/')
        request.user = staff
        admin_instance = DocumentSubmissionAdmin(DocumentSubmission, AdminSite())
        # This assertion checks permission scope, not unspecified DB row order.
        self.assertCountEqual(admin_instance.get_queryset(request), DocumentSubmission.objects.all())
        self.assertTrue(admin_instance.has_view_permission(request, submission))
        self.assertFalse(admin_instance.has_add_permission(request))
        self.assertFalse(admin_instance.has_change_permission(request, submission))
        self.assertTrue(admin_instance.has_delete_permission(request, submission))
        request.user = other_staff
        self.assertFalse(admin_instance.get_queryset(request).exists())
        self.assertFalse(admin_instance.has_view_permission(request, submission))
        self.assertFalse(admin_instance.has_delete_permission(request, submission))
        self.assertNotIn('ip_address', admin_instance.fields)
        self.assertNotIn('user_agent', admin_instance.fields)

        bundle_admin = RequiredDocumentConsentBundleAdmin(
            RequiredDocumentConsentBundle,
            AdminSite(),
        )
        request.user = staff
        self.assertTrue(bundle_admin.has_view_permission(request, submission.bundle))
        self.assertFalse(bundle_admin.has_add_permission(request))
        self.assertFalse(bundle_admin.has_change_permission(request, submission.bundle))
        self.assertFalse(bundle_admin.has_delete_permission(request, submission.bundle))

    def test_team_admin_can_see_submitted_people_and_document_statuses(self):
        create_required_document_submissions(team=self.team, respondent=self.respondent)
        other_team = Team.objects.create(
            name='다른 팀',
            organization_name='다른 기관',
            organization_name_en='OTHER ORGANIZATION',
            code='OTHER-CONSENT-TEAM',
            is_active=True,
            start_date=date.today(),
            max_day_no=1,
        )
        other_respondent = Respondent.objects.create(
            team=other_team,
            name='다른 참가자',
            birth_date=date(1992, 3, 4),
            phone='01077776666',
            gender='F',
            normalized_name='다른참가자',
            normalized_phone='01077776666',
        )
        TeamFeatureSetting.objects.create(
            team=other_team,
            enable_privacy_consent=True,
            enable_third_party_consent=True,
            enable_safety_pledge=True,
        )
        create_required_document_submissions(
            team=other_team,
            respondent=other_respondent,
        )
        User = get_user_model()
        staff = User.objects.create_user(
            username='consent-team-admin',
            password='pw',
            is_staff=True,
        )
        AdminTeamPermission.objects.create(user=staff, team=self.team)
        self.client.force_login(staff)

        response = self.client.get(
            reverse('admin:surveys_requireddocumentconsentbundle_changelist')
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '동의서 제출자 현황')
        self.assertContains(response, '홍길동')
        self.assertContains(response, '1990-01-02')
        self.assertContains(response, '01012345678')
        self.assertContains(response, '독도재단')
        self.assertContains(response, '1차 원정대')
        self.assertContains(response, '개인정보 동의')
        self.assertContains(response, '제3자 제공 동의')
        self.assertContains(response, '안전수칙 서약')
        self.assertContains(response, '전체 제출 완료')
        self.assertNotContains(response, '다른 참가자')
        self.assertNotContains(response, '01077776666')


class RequiredDocumentBrowserHistoryTest(SimpleTestCase):
    def test_document_javascript_keeps_browser_back_inside_required_document_flow(self):
        script_path = finders.find('surveys/v2_document.js')
        self.assertIsNotNone(script_path)
        with open(script_path, encoding='utf-8') as script_file:
            script = script_file.read()
        self.assertIn("window.history.pushState(", script)
        self.assertIn("window.history.replaceState(", script)
        self.assertIn("window.addEventListener('popstate'", script)
        self.assertIn("const fallbackIndex = Math.max(0, activeIndex - 1)", script)
        self.assertIn("window.fetch(documentUrl", script)
        self.assertNotIn('Date.now(', script)
        self.assertNotIn('new Date(', script)
        self.assertNotIn('agreed_at', script)
        for forbidden in ('birth_date', 'phone', 'gender', 'resident_registration'):
            self.assertNotIn(forbidden, script)
