from datetime import date
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.forms import ModelForm
from django.test import RequestFactory
from django.test import TestCase
from django.urls import reverse

from .admin import (
    ParticipantAdmin,
    TeamAdmin,
    TeamFeatureSettingInline,
    iter_upload_rows,
    normalize_upload_gender,
    normalize_upload_row,
    parse_birth_date,
)
from .team_deletion import get_team_purge_counts, purge_team
from .respondent_deletion import purge_respondents
from .models import Participant
from .models import Team
from .services import calculate_team_day_no
from .validators import validate_reflection_image
from surveys.models import (
    CommonQuestionSet,
    CommonQuestionSetDeployment,
    DocumentSubmission,
    DocumentTemplate,
    RequiredDocumentConsentBundle,
    SurveyAnswer,
    SurveyForm,
    SurveyQuestion,
    SurveySection,
    SurveySubmission,
)
from teams.models import Respondent
from django.utils import timezone


class ReflectionImageValidatorTest(TestCase):
    def test_accepts_supported_image_signature(self):
        upload = SimpleUploadedFile(
            'guide.png',
            b'\x89PNG\r\n\x1a\n' + b'content',
            content_type='image/png',
        )
        validate_reflection_image(upload)

    def test_rejects_extension_mime_and_signature_mismatch(self):
        invalid_uploads = (
            SimpleUploadedFile('guide.gif', b'GIF89a', content_type='image/gif'),
            SimpleUploadedFile('guide.png', b'\x89PNG\r\n\x1a\n', content_type='text/plain'),
            SimpleUploadedFile('guide.jpg', b'not-an-image', content_type='image/jpeg'),
            SimpleUploadedFile(
                'guide.jpg',
                b'\x89PNG\r\n\x1a\n',
                content_type='image/jpeg',
            ),
        )
        for upload in invalid_uploads:
            with self.subTest(name=upload.name), self.assertRaises(ValidationError):
                validate_reflection_image(upload)


class TeamDayNoPolicyTest(TestCase):
    def test_day_no_is_computed_from_start_date(self) -> None:
        team = Team(name="A", code="A001", start_date=date(2026, 4, 12), max_day_no=3)
        day_no = calculate_team_day_no(team, today=date(2026, 4, 13))
        self.assertEqual(day_no, 2)

    def test_day_no_uses_localdate_when_today_is_missing(self) -> None:
        team = Team(name="A", code="A001", start_date=date(2026, 4, 12), max_day_no=3)
        with patch("teams.services.timezone.localdate", return_value=date(2026, 4, 14)):
            day_no = calculate_team_day_no(team)
        self.assertEqual(day_no, 3)

    def test_team_code_and_manager_code_remain_manual_required_fields(self):
        team = Team.objects.create(
            name='운영팀',
            code='manual-code',
            manager_entry_code='manual-manager-code',
            start_date=date.today(),
            max_day_no=5,
            is_active=True,
        )
        self.assertEqual(team.code, 'manual-code')
        self.assertEqual(team.manager_entry_code, 'manual-manager-code')

    def test_team_name_and_organization_name_are_stored_separately(self):
        team = Team.objects.create(
            name='1차 원정대',
            organization_name='독도재단',
            organization_name_en='DOKDO FOUNDATION',
            code='expedition-1',
            start_date=date.today(),
            max_day_no=3,
        )

        team.refresh_from_db()
        self.assertEqual(team.name, '1차 원정대')
        self.assertEqual(team.organization_name, '독도재단')
        self.assertEqual(team.organization_name_en, 'DOKDO FOUNDATION')

    def test_team_admin_requires_organization_name(self):
        team_admin = TeamAdmin(Team, AdminSite())
        form_class: type[ModelForm] = team_admin.get_form(RequestFactory().get('/admin/teams/team/add/'))

        self.assertTrue(form_class.base_fields['organization_name'].required)
        self.assertEqual(form_class.base_fields['organization_name'].label, '운영기관')
        self.assertTrue(form_class.base_fields['organization_name_en'].required)
        self.assertEqual(form_class.base_fields['organization_name_en'].label, '운영기관(영문)')


class ParticipantAdminNormalizationTest(TestCase):
    def test_participant_admin_save_model_sets_normalized_fields(self):
        team = Team.objects.create(
            name='운영팀',
            code='team-normalize',
            manager_entry_code='team-normalize-manager',
            start_date=date.today(),
            max_day_no=5,
            is_active=True,
        )
        participant = Participant(
            team=team,
            name=' 홍 길 동 ',
            birth_date=date(1990, 1, 1),
            phone='010-1234-5678',
            is_active=True,
        )

        admin_obj = ParticipantAdmin(Participant, AdminSite())
        request = RequestFactory().post('/admin/teams/participant/add/')
        request.user = get_user_model().objects.create_superuser('root', 'root@example.com', 'pass')

        admin_obj.save_model(request, participant, form=None, change=False)
        self.assertEqual(participant.normalized_name, '홍길동')
        self.assertEqual(participant.normalized_phone, '01012345678')

    def test_iter_upload_rows_reads_csv_columns(self):
        raw = "name,birth_date,phone\n홍길동,1990-01-01,01012345678\n".encode('utf-8')
        rows = list(iter_upload_rows('participants.csv', raw))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['name'], '홍길동')

    def test_iter_upload_rows_supports_cp949_csv(self):
        raw = "이름,주민등록번호,연락처,성별\n홍길동,900101,01012345678,남\n".encode('cp949')
        rows = list(iter_upload_rows('participants.csv', raw))
        self.assertEqual(len(rows), 1)
        normalized = normalize_upload_row(rows[0])
        self.assertEqual(normalized['birth_date'], '900101')
        self.assertEqual(normalized['gender'], '남')

    def test_normalize_upload_row_supports_korean_headers(self):
        row = normalize_upload_row({'이름': '홍길동', '생년월일': '661030', '연락처': '01012345678', '성별': '남'})
        self.assertEqual(row['name'], '홍길동')
        self.assertEqual(row['birth_date'], '661030')
        self.assertEqual(row['phone'], '01012345678')
        self.assertEqual(row['gender'], '남')

    def test_parse_birth_date_supports_6_and_8_digit(self):
        self.assertEqual(parse_birth_date('661030'), date(1966, 10, 30))
        self.assertEqual(parse_birth_date('001231'), date(2000, 12, 31))
        self.assertEqual(parse_birth_date('030101'), date(2003, 1, 1))
        self.assertEqual(parse_birth_date('20110413'), date(2011, 4, 13))
        self.assertEqual(parse_birth_date('7907281234567'), date(1979, 7, 28))

    def test_parse_birth_date_supports_2000s_csv_values_without_leading_zero(self):
        self.assertEqual(parse_birth_date('30101'), date(2003, 1, 1))
        self.assertEqual(parse_birth_date('30101.0'), date(2003, 1, 1))
        self.assertEqual(parse_birth_date('91231'), date(2009, 12, 31))

    def test_normalize_upload_gender_supports_korean_and_code_values(self):
        self.assertEqual(normalize_upload_gender('남'), 'M')
        self.assertEqual(normalize_upload_gender('F'), 'F')
        self.assertEqual(normalize_upload_gender('미입력'), 'U')
        self.assertIsNone(normalize_upload_gender(''))

    def test_team_admin_submission_status_link_points_to_manager_page(self):
        team = Team.objects.create(
            name='링크팀',
            code='link-team',
            manager_entry_code='link-manager',
            start_date=date.today(),
            max_day_no=3,
            is_active=True,
        )
        team_admin = TeamAdmin(Team, AdminSite())
        link = team_admin.submission_status_link(team)
        self.assertIn(reverse('admins:prototype-manager'), link)
        self.assertIn(f'team_id={team.id}', link)

    def test_team_admin_lists_and_searches_by_organization_name(self):
        team_admin = TeamAdmin(Team, AdminSite())

        self.assertIn('organization_name', team_admin.list_display)
        self.assertIn('organization_name', team_admin.search_fields)
        self.assertIn('organization_name_en', team_admin.list_display)
        self.assertIn('organization_name_en', team_admin.search_fields)

    def test_team_admin_exposes_required_document_settings_inline(self):
        team_admin = TeamAdmin(Team, AdminSite())

        self.assertIn(TeamFeatureSettingInline, team_admin.inlines)
        self.assertEqual(
            TeamFeatureSettingInline.fields,
            (
                'enable_privacy_consent',
                'enable_third_party_consent',
                'enable_safety_pledge',
            ),
        )


class RespondentPurgeAdminTest(TestCase):
    def setUp(self):
        self.superuser = get_user_model().objects.create_superuser(
            username='respondent-purge-admin',
            email='admin@example.com',
            password='password',
        )
        self.team = Team.objects.create(
            name='응답자 삭제 팀',
            organization_name='운영기관',
            organization_name_en='Organization',
            code='RESP-PURGE',
            manager_entry_code='RESP-PURGE-MANAGER',
            start_date=date.today(),
            max_day_no=1,
        )
        self.respondent = Respondent.objects.create(
            team=self.team,
            name='삭제 응답자',
            birth_date=date(1990, 1, 1),
            phone='01012345678',
            normalized_name='삭제응답자',
            normalized_phone='01012345678',
        )
        self.bundle = RequiredDocumentConsentBundle.objects.create(
            team=self.team,
            respondent=self.respondent,
            participant_snapshot_json={},
            team_snapshot_json={},
            enabled_document_types_snapshot=['privacy_consent'],
        )
        self.template = DocumentTemplate.objects.create(
            team=self.team,
            document_type=DocumentTemplate.DocumentType.PRIVACY_CONSENT,
            title='개인정보 동의',
            content_html='동의 내용',
            version=1,
        )
        self.document_submission = DocumentSubmission.objects.create(
            team=self.team,
            respondent=self.respondent,
            bundle=self.bundle,
            template=self.template,
            document_type=DocumentTemplate.DocumentType.PRIVACY_CONSENT,
            template_version=1,
            template_hash_at_submit=self.template.body_hash,
            document_title_snapshot='개인정보 동의',
            document_content_snapshot='동의 내용',
            document_content_hash_at_submit=self.template.body_hash,
            agreed=True,
            signer_name=self.respondent.name,
            agreed_at=timezone.now(),
        )

    def test_purge_respondent_deletes_document_evidence_without_deleting_template(self):
        deleted_count = purge_respondents(respondent_ids=[self.respondent.pk])

        self.assertEqual(deleted_count, 1)
        self.assertFalse(Respondent.objects.filter(pk=self.respondent.pk).exists())
        self.assertFalse(
            DocumentSubmission.objects.filter(pk=self.document_submission.pk).exists()
        )
        self.assertFalse(
            RequiredDocumentConsentBundle.objects.filter(pk=self.bundle.pk).exists()
        )
        self.assertTrue(DocumentTemplate.objects.filter(pk=self.template.pk).exists())

    def test_admin_delete_view_removes_protected_document_relations(self):
        self.client.force_login(self.superuser)
        delete_url = reverse(
            'admin:teams_respondent_delete',
            args=[self.respondent.pk],
        )

        confirmation = self.client.get(delete_url)
        self.assertEqual(confirmation.status_code, 200)

        response = self.client.post(
            delete_url,
            {'post': 'yes'},
        )

        self.assertRedirects(
            response,
            reverse('admin:teams_respondent_changelist'),
            fetch_redirect_response=False,
        )
        self.assertFalse(Respondent.objects.filter(pk=self.respondent.pk).exists())
        self.assertFalse(
            DocumentSubmission.objects.filter(pk=self.document_submission.pk).exists()
        )


class TeamPurgeAdminTest(TestCase):
    def setUp(self):
        self.superuser = get_user_model().objects.create_superuser(
            username='team-purge-admin',
            email='admin@example.com',
            password='password',
        )
        self.team = Team.objects.create(
            name='삭제 대상 팀',
            code='PURGE-TEAM',
            manager_entry_code='PURGE-MANAGER',
            start_date=date.today(),
            max_day_no=3,
            is_active=True,
        )
        self.other_team = Team.objects.create(
            name='보존 팀',
            code='KEEP-TEAM',
            manager_entry_code='KEEP-MANAGER',
            start_date=date.today(),
            max_day_no=3,
            is_active=True,
        )
        self.respondent = Respondent.objects.create(
            team=self.team,
            name='삭제 응답자',
            birth_date=date(1990, 1, 1),
            phone='01012345678',
            normalized_name='삭제응답자',
            normalized_phone='01012345678',
        )
        Participant.objects.create(
            team=self.team,
            name='삭제 대상자',
            birth_date=date(1990, 1, 1),
            phone='01012345678',
            normalized_name='삭제대상자',
            normalized_phone='01012345678',
        )
        self.form = SurveyForm.objects.create(
            team=self.team,
            day_no=1,
            title='삭제 설문',
            version=1,
            is_active=True,
        )
        section = SurveySection.objects.create(
            form=self.form,
            title='신체',
            section_type=SurveySection.SectionType.PHYSICAL,
            display_order=1,
        )
        question = SurveyQuestion.objects.create(
            section=section,
            key='delete_q1',
            label='삭제 문항',
            question_type=SurveyQuestion.QuestionType.SINGLE,
            options_json=[{'label': '예', 'score': 1}],
            display_order=1,
        )
        submission = SurveySubmission.objects.create(
            team=self.team,
            respondent=self.respondent,
            day_no=1,
            answers_json={'delete_q1': '예'},
            result_json={},
            form_version=1,
            form_snapshot_json={},
            submitted_at=timezone.now(),
        )
        SurveyAnswer.objects.create(
            submission=submission,
            question=question,
            answer_text='예',
            answer_json={'value': '예'},
        )
        self.common_set = CommonQuestionSet.objects.create(
            name='보존할 공통 질문',
            title='공통 질문',
            day_no=1,
            is_active=True,
        )
        CommonQuestionSetDeployment.objects.create(
            question_set=self.common_set,
            team=self.team,
            survey_form=self.form,
            status=CommonQuestionSetDeployment.Status.CREATED,
        )
        SurveyForm.objects.create(
            team=self.other_team,
            day_no=1,
            title='보존 설문',
            version=1,
            is_active=True,
        )

    def test_purge_counts_include_team_specific_relations(self):
        counts = get_team_purge_counts(self.team)

        self.assertEqual(counts['participants']['count'], 1)
        self.assertEqual(counts['respondents']['count'], 1)
        self.assertEqual(counts['survey_forms']['count'], 1)
        self.assertEqual(counts['survey_questions']['count'], 1)
        self.assertEqual(counts['survey_submissions']['count'], 1)
        self.assertEqual(counts['survey_answers']['count'], 1)
        self.assertEqual(counts['deployments']['count'], 1)

    def test_admin_purge_requires_exact_team_code(self):
        self.client.force_login(self.superuser)
        delete_url = reverse('admin:teams_team_delete', args=[self.team.pk])

        response = self.client.post(
            delete_url,
            {'team_code': 'WRONG', 'confirm_purge': 'on'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '팀 코드가 일치하지 않습니다.')
        self.assertTrue(Team.objects.filter(pk=self.team.pk).exists())

    def test_admin_purge_rejects_non_superuser(self):
        staff_user = get_user_model().objects.create_user(
            username='ordinary-staff',
            password='password',
            is_staff=True,
        )
        self.client.force_login(staff_user)

        response = self.client.get(
            reverse('admin:teams_team_delete', args=[self.team.pk]),
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Team.objects.filter(pk=self.team.pk).exists())

    def test_admin_purge_deletes_team_graph_but_preserves_global_and_other_team(self):
        self.client.force_login(self.superuser)
        delete_url = reverse('admin:teams_team_delete', args=[self.team.pk])

        response = self.client.post(
            delete_url,
            {'team_code': self.team.code, 'confirm_purge': 'on'},
        )

        self.assertRedirects(
            response,
            reverse('admin:teams_team_changelist'),
            fetch_redirect_response=False,
        )
        self.assertFalse(Team.objects.filter(pk=self.team.pk).exists())
        self.assertFalse(Respondent.objects.filter(team_id=self.team.pk).exists())
        self.assertFalse(SurveyForm.objects.filter(team_id=self.team.pk).exists())
        self.assertFalse(SurveySubmission.objects.filter(team_id=self.team.pk).exists())
        self.assertFalse(CommonQuestionSetDeployment.objects.filter(team_id=self.team.pk).exists())
        self.assertTrue(CommonQuestionSet.objects.filter(pk=self.common_set.pk).exists())
        self.assertTrue(Team.objects.filter(pk=self.other_team.pk).exists())
        self.assertTrue(SurveyForm.objects.filter(team=self.other_team).exists())

    def test_service_purge_is_atomic_when_a_delete_step_fails(self):
        with patch(
            'teams.team_deletion.DocumentTemplate.objects.filter',
            side_effect=RuntimeError('forced failure'),
        ):
            with self.assertRaises(RuntimeError):
                purge_team(team_id=self.team.pk)

        self.assertTrue(Team.objects.filter(pk=self.team.pk).exists())
        self.assertTrue(SurveySubmission.objects.filter(team=self.team).exists())
        self.assertTrue(CommonQuestionSetDeployment.objects.filter(team=self.team).exists())
