import hashlib
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.db.models.signals import post_delete
from django.dispatch import receiver

from teams.models import Respondent, Team, TimeStampedModel


class LoginSessionFamily(models.Model):
    """Browser proof and revocation epoch; never an authentication credential."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    secret_hash = models.CharField(max_length=64)
    epoch = models.PositiveBigIntegerField(default=0)
    active_generation = models.UUIDField(null=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = 'login_session_families'


class LoginSessionCredential(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    family = models.ForeignKey(LoginSessionFamily, on_delete=models.CASCADE)
    epoch = models.PositiveBigIntegerField()
    session_key = models.CharField(max_length=40, unique=True)
    # Historical keys may identify a family for REVOCATION ONLY. They must
    # never be used to look up a replacement authentication credential.
    key_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'login_session_credentials'


class SurveyForm(TimeStampedModel):
    class FormType(models.TextChoices):
        DAILY_HEALTH = 'daily_health', '일차별 자가검진'
        EXIT_ANONYMOUS = 'exit_anonymous', '설문'
        EXIT_SURVEY = 'exit_survey', '탐방 종료 설문'

    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="survey_forms")
    day_no = models.PositiveSmallIntegerField()
    title = models.CharField(max_length=200)
    title_en = models.CharField(max_length=200, blank=True)
    version = models.PositiveIntegerField(default=1)
    schema_json = models.JSONField(default=dict)
    form_type = models.CharField(max_length=30, choices=FormType.choices, default=FormType.DAILY_HEALTH)
    is_anonymous = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "survey_forms"
        constraints = [
            models.UniqueConstraint(fields=["team", "day_no", "version"], name="surv_form_team_day_ver_uq"),
            models.CheckConstraint(check=Q(day_no__gte=1), name="survey_forms_day_no_gte_1"),
        ]
        indexes = [
            models.Index(fields=["team", "day_no", "is_active"], name="surv_form_team_day_act_idx"),
            models.Index(fields=["team", "form_type", "is_active"], name="surv_form_team_type_act_idx"),
        ]
        verbose_name = '설문 폼'
        verbose_name_plural = '설문 폼'

    def __str__(self) -> str:
        return f'{self.team_id}-D{self.day_no}-V{self.version}'


class SurveySection(TimeStampedModel):
    class SectionType(models.TextChoices):
        PHYSICAL = 'physical', '신체'
        MENTAL = 'mental', '정신'
        SURVEY = 'survey', '설문'

    form = models.ForeignKey(SurveyForm, on_delete=models.CASCADE, related_name='sections')
    title = models.CharField(max_length=100)
    title_en = models.CharField(max_length=100, blank=True)
    section_type = models.CharField(max_length=20, choices=SectionType.choices)
    display_order = models.PositiveSmallIntegerField(default=1)

    class Meta:
        db_table = 'survey_sections'
        ordering = ['display_order', 'id']
        indexes = [models.Index(fields=['form', 'display_order'], name='surv_sec_form_order_idx')]
        verbose_name = '설문 섹션'
        verbose_name_plural = '설문 섹션'

    def __str__(self) -> str:
        return f'{self.form_id}:{self.section_type}'


class SurveyQuestion(TimeStampedModel):
    class QuestionType(models.TextChoices):
        TEXT = 'text', '주관식'
        SINGLE = 'single_choice', '단일선택'
        MULTI = 'multi_choice', '복수선택'

    section = models.ForeignKey(SurveySection, on_delete=models.CASCADE, related_name='questions')
    key = models.CharField(max_length=100)
    label = models.CharField(max_length=255)
    label_en = models.CharField(max_length=255, blank=True)
    graph_label = models.CharField(max_length=50, blank=True)
    graph_label_en = models.CharField(max_length=50, blank=True)
    question_type = models.CharField(max_length=30, choices=QuestionType.choices)
    options_json = models.JSONField(default=list, blank=True)
    is_required = models.BooleanField(default=True)
    display_order = models.PositiveSmallIntegerField(default=1)

    class Meta:
        db_table = 'survey_questions'
        ordering = ['display_order', 'id']
        constraints = [
            models.UniqueConstraint(fields=['section', 'key'], name='surv_q_section_key_uq'),
        ]
        indexes = [
            models.Index(fields=['section', 'display_order'], name='surv_q_section_order_idx'),
        ]
        verbose_name = '설문 문항'
        verbose_name_plural = '설문 문항'

    def __str__(self) -> str:
        return f'{self.section_id}:{self.key}'


class SurveySubmission(TimeStampedModel):
    class SubmissionLanguage(models.TextChoices):
        KOREAN = 'ko', '한국어'
        ENGLISH = 'en', '영어'

    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="survey_submissions", db_index=True)
    respondent = models.ForeignKey(Respondent, on_delete=models.PROTECT, related_name="survey_submissions", db_index=True)
    day_no = models.PositiveSmallIntegerField()
    answers_json = models.JSONField(default=dict)
    result_json = models.JSONField(default=dict)
    form_version = models.PositiveIntegerField()
    form_snapshot_json = models.JSONField(default=dict)
    submission_language = models.CharField(
        max_length=2,
        choices=SubmissionLanguage.choices,
        default=SubmissionLanguage.KOREAN,
    )
    is_locked = models.BooleanField(default=True)
    submitted_at = models.DateTimeField()

    class Meta:
        db_table = "survey_submissions"
        constraints = [
            models.UniqueConstraint(fields=["team", "respondent", "day_no"], name="surv_sub_team_resp_day_uq"),
            models.CheckConstraint(check=Q(day_no__gte=1), name="surv_sub_day_no_gte_1"),
        ]
        indexes = [
            models.Index(fields=["team", "day_no"], name="surv_sub_team_day_idx"),
            models.Index(fields=["submitted_at"], name="surv_sub_submitted_at_idx"),
        ]
        verbose_name = '설문 제출'
        verbose_name_plural = '설문 제출'

    def __str__(self) -> str:
        return f'submission#{self.id}'


class SurveyAnswer(TimeStampedModel):
    submission = models.ForeignKey(SurveySubmission, on_delete=models.CASCADE, related_name='answers')
    question = models.ForeignKey(SurveyQuestion, on_delete=models.PROTECT, related_name='answers')
    answer_text = models.TextField(blank=True)
    answer_json = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'survey_answers'
        constraints = [
            models.UniqueConstraint(fields=['submission', 'question'], name='surv_ans_sub_q_uq'),
        ]
        indexes = [
            models.Index(fields=['submission'], name='surv_ans_submission_idx'),
            models.Index(fields=['question'], name='surv_ans_question_idx'),
        ]
        verbose_name = '설문 답변'
        verbose_name_plural = '설문 답변'

    def __str__(self) -> str:
        return f'{self.submission_id}:{self.question_id}'


class CommonQuestionSet(TimeStampedModel):
    class FormType(models.TextChoices):
        DAILY_HEALTH = 'daily_health', '자가건강검진'
        EXIT_SURVEY = 'exit_survey', '탐방 종료 설문'

    name = models.CharField(max_length=150, unique=True)
    title = models.CharField(max_length=200)
    title_en = models.CharField(max_length=200, blank=True)
    form_type = models.CharField(
        max_length=30,
        choices=FormType.choices,
        default=FormType.DAILY_HEALTH,
        verbose_name='설문 유형',
    )
    day_no = models.PositiveSmallIntegerField()
    is_active = models.BooleanField(default=True)
    description = models.TextField(blank=True)

    class Meta:
        db_table = 'common_question_sets'
        constraints = [
            models.CheckConstraint(check=Q(day_no__gte=1), name='common_q_set_day_no_gte_1'),
        ]
        verbose_name = '공통 설문'
        verbose_name_plural = '공통 설문'

    def __str__(self) -> str:
        return f'{self.name} · {self.get_form_type_display()} (D{self.day_no})'


class CommonQuestionSection(TimeStampedModel):
    question_set = models.ForeignKey(CommonQuestionSet, on_delete=models.CASCADE, related_name='sections')
    title = models.CharField(max_length=100)
    title_en = models.CharField(max_length=100, blank=True)
    section_type = models.CharField(max_length=20, choices=SurveySection.SectionType.choices)
    display_order = models.PositiveSmallIntegerField(default=1)

    class Meta:
        db_table = 'common_question_sections'
        ordering = ['display_order', 'id']
        verbose_name = '공통 질문 섹션'
        verbose_name_plural = '공통 질문 섹션'

    def __str__(self) -> str:
        return f'{self.question_set_id}:{self.title}'


class CommonQuestion(TimeStampedModel):
    question_set = models.ForeignKey(
        CommonQuestionSet,
        on_delete=models.CASCADE,
        related_name='questions',
    )
    section = models.ForeignKey(CommonQuestionSection, on_delete=models.CASCADE, related_name='questions')
    key = models.CharField(max_length=100)
    label = models.CharField(max_length=255)
    label_en = models.CharField(max_length=255, blank=True)
    graph_label = models.CharField(max_length=50, blank=True)
    graph_label_en = models.CharField(max_length=50, blank=True)
    question_type = models.CharField(max_length=30, choices=SurveyQuestion.QuestionType.choices)
    options_json = models.JSONField(default=list, blank=True)
    is_required = models.BooleanField(default=True)
    display_order = models.PositiveSmallIntegerField(default=1)

    class Meta:
        db_table = 'common_questions'
        ordering = ['display_order', 'id']
        constraints = [
            models.UniqueConstraint(fields=['section', 'key'], name='common_q_section_key_uq'),
        ]
        verbose_name = '공통 질문 문항'
        verbose_name_plural = '공통 질문 문항'

    def __str__(self) -> str:
        return f'{self.section_id}:{self.key}'

    def clean(self):
        super().clean()
        if not self.section_id:
            return
        section_question_set_id = self.section.question_set_id
        if self.question_set_id and self.question_set_id != section_question_set_id:
            raise ValidationError(
                {'section': '같은 공통 설문에 속한 섹션만 선택할 수 있습니다.'}
            )
        self.question_set_id = section_question_set_id

    def save(self, *args, **kwargs):
        if self.section_id:
            self.question_set_id = self.section.question_set_id
        super().save(*args, **kwargs)


class TeamCommonQuestionExclusion(TimeStampedModel):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='common_question_exclusions')
    question_set = models.ForeignKey(CommonQuestionSet, on_delete=models.CASCADE, related_name='team_exclusions')
    question_key = models.CharField(max_length=100)

    class Meta:
        db_table = 'team_common_question_exclusions'
        constraints = [
            models.UniqueConstraint(
                fields=['team', 'question_set', 'question_key'],
                name='team_common_q_excl_uq',
            ),
        ]
        indexes = [
            models.Index(fields=['team', 'question_set'], name='team_cq_excl_tset_idx'),
        ]
        verbose_name = '팀별 공통 문항 제외'
        verbose_name_plural = '팀별 공통 문항 제외'

    def __str__(self) -> str:
        return f'{self.team_id}:{self.question_set_id}:{self.question_key}'


class TeamFeatureSetting(TimeStampedModel):
    team = models.OneToOneField(Team, on_delete=models.CASCADE, related_name='feature_settings')
    enable_privacy_consent = models.BooleanField(default=False, verbose_name='개인정보제공동의서 사용')
    enable_third_party_consent = models.BooleanField(default=False, verbose_name='제 3자 정보제공 동의서 사용')
    enable_safety_pledge = models.BooleanField(default=False, verbose_name='여행안전수칙서약서 사용')
    notes = models.TextField(blank=True)

    class Meta:
        db_table = 'team_feature_settings'
        verbose_name = '팀별 기능 사용 설정'
        verbose_name_plural = '팀별 기능 사용 설정'

    def __str__(self) -> str:
        return f'{self.team} 기능 설정'


class DocumentTemplate(TimeStampedModel):
    class DocumentType(models.TextChoices):
        PRIVACY_CONSENT = 'privacy_consent', '개인정보제공동의서'
        THIRD_PARTY_CONSENT = 'third_party_consent', '제 3자 정보제공 동의서'
        SAFETY_PLEDGE = 'safety_pledge', '여행안전수칙서약서'

    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name='document_templates')
    document_type = models.CharField(max_length=30, choices=DocumentType.choices)
    title = models.CharField(max_length=200)
    title_en = models.CharField(max_length=200, blank=True)
    content_html = models.TextField()
    content_html_en = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=1)
    body_hash = models.CharField(max_length=64, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'document_templates'
        constraints = [
            models.UniqueConstraint(fields=['team', 'document_type', 'version'], name='doc_tpl_team_type_ver_uq'),
        ]
        indexes = [
            models.Index(fields=['team', 'document_type', 'is_active'], name='doc_tpl_team_type_act_idx'),
        ]
        verbose_name = '문서 템플릿'
        verbose_name_plural = '문서 템플릿'

    def save(self, *args, **kwargs):
        canonical_body = self.content_html or ''
        if self.content_html_en:
            canonical_body += f'\n---LANG:EN---\n{self.content_html_en}'
        self.body_hash = hashlib.sha256(canonical_body.encode('utf-8')).hexdigest()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f'{self.team_id}:{self.document_type}:v{self.version}'


class RequiredDocumentConsentBundle(TimeStampedModel):
    class SnapshotSource(models.TextChoices):
        LIVE_SUBMIT = 'live_submit', '실시간 제출'
        LEGACY_CURRENT_STATE = 'legacy_current_state', '기존 데이터 현재 상태'

    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name='required_document_consent_bundles')
    respondent = models.ForeignKey(
        Respondent,
        on_delete=models.PROTECT,
        related_name='required_document_consent_bundles',
    )
    bundle_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    participant_snapshot_json = models.JSONField(default=dict)
    team_snapshot_json = models.JSONField(default=dict)
    enabled_document_types_snapshot = models.JSONField(default=list)
    snapshot_schema_version = models.PositiveSmallIntegerField(default=1)
    snapshot_source = models.CharField(
        max_length=30,
        choices=SnapshotSource.choices,
        default=SnapshotSource.LIVE_SUBMIT,
    )
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'required_document_consent_bundles'
        constraints = [
            models.UniqueConstraint(
                fields=['team', 'respondent'],
                name='req_doc_bundle_team_resp_uq',
            ),
        ]
        indexes = [
            models.Index(fields=['team', 'completed_at'], name='req_doc_bundle_team_done_idx'),
        ]
        verbose_name = '필수문서 전자 동의 묶음'
        verbose_name_plural = '필수문서 전자 동의 묶음'

    def __str__(self) -> str:
        return str(self.bundle_uuid)


class DocumentSubmission(TimeStampedModel):
    class SignatureMethod(models.TextChoices):
        TYPED_NAME = 'typed_name', '성명 입력'
        CHECKBOX = 'checkbox', '체크박스'
        DRAWN = 'drawn', '전자서명'

    class SnapshotSource(models.TextChoices):
        LIVE_SUBMIT = 'live_submit', '실시간 제출'
        LEGACY_TEMPLATE_BACKFILL = 'legacy_template_backfill', '기존 템플릿 복원'
        LEGACY_CURRENT_STATE = 'legacy_current_state', '기존 데이터 현재 상태'
        RECONSTRUCTED = 'reconstructed', '재구성'
        UNAVAILABLE = 'unavailable', '복원 불가'

    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name='document_submissions')
    respondent = models.ForeignKey(Respondent, on_delete=models.PROTECT, related_name='document_submissions')
    bundle = models.ForeignKey(
        RequiredDocumentConsentBundle,
        on_delete=models.PROTECT,
        related_name='submissions',
        null=True,
        blank=True,
    )
    template = models.ForeignKey(
        DocumentTemplate,
        on_delete=models.PROTECT,
        related_name='submissions',
        null=True,
        blank=True,
        help_text='기존 DB 문서 템플릿 제출에만 사용합니다. 정적 문서 제출은 비어 있습니다.',
    )
    document_type = models.CharField(max_length=30, choices=DocumentTemplate.DocumentType.choices)
    template_version = models.PositiveIntegerField()
    template_hash_at_submit = models.CharField(max_length=64)
    document_title_snapshot = models.CharField(max_length=200, blank=True)
    document_content_snapshot = models.TextField(blank=True)
    document_content_hash_at_submit = models.CharField(max_length=64, blank=True)
    snapshot_source = models.CharField(
        max_length=30,
        choices=SnapshotSource.choices,
        default=SnapshotSource.UNAVAILABLE,
    )
    agreed = models.BooleanField(default=True)
    signer_name = models.CharField(max_length=100)
    signature_method = models.CharField(max_length=30, choices=SignatureMethod.choices, default=SignatureMethod.TYPED_NAME)
    agreed_at = models.DateTimeField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    class Meta:
        db_table = 'document_submissions'
        constraints = [
            models.UniqueConstraint(fields=['team', 'respondent', 'document_type'], name='doc_sub_team_resp_type_uq'),
        ]
        indexes = [
            models.Index(fields=['team', 'document_type', 'agreed_at'], name='doc_sub_team_type_at_idx'),
            models.Index(fields=['respondent', 'document_type'], name='doc_sub_resp_type_idx'),
        ]
        verbose_name = '문서 제출'
        verbose_name_plural = '문서 제출'

    def __str__(self) -> str:
        return f'{self.respondent_id}:{self.document_type}'


@receiver(post_delete, sender=DocumentSubmission)
def reset_required_document_bundle_completion(sender, instance, **kwargs):
    if instance.bundle_id:
        RequiredDocumentConsentBundle.objects.filter(
            pk=instance.bundle_id,
            completed_at__isnull=False,
        ).update(completed_at=None)


class ReflectionSubmission(TimeStampedModel):
    class PdfStatus(models.TextChoices):
        PENDING = 'pending', '생성 대기'
        READY = 'ready', '생성 완료'
        FAILED = 'failed', '생성 실패'
        DELETED = 'deleted', '보관기간 만료 삭제'

    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name='reflection_submissions')
    respondent = models.ForeignKey(Respondent, on_delete=models.PROTECT, related_name='reflection_submissions')
    affiliation = models.CharField(max_length=100, default='')
    country = models.CharField(max_length=100, default='')
    title = models.CharField(max_length=200, default='')
    content = models.TextField()
    file_path = models.CharField(max_length=500, blank=True)
    file_name = models.CharField(max_length=255, blank=True)
    file_size = models.PositiveBigIntegerField(default=0)
    file_hash = models.CharField(max_length=64, blank=True)
    generated_at = models.DateTimeField(null=True, blank=True)
    pdf_status = models.CharField(
        max_length=20,
        choices=PdfStatus.choices,
        default=PdfStatus.PENDING,
    )
    pdf_error = models.TextField(blank=True)
    first_downloaded_at = models.DateTimeField(null=True, blank=True)
    purge_after = models.DateTimeField(null=True, blank=True)
    purged_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField()
    is_locked = models.BooleanField(default=True)

    class Meta:
        db_table = 'reflection_submissions'
        constraints = [
            models.UniqueConstraint(fields=['team', 'respondent'], name='reflection_team_resp_uq'),
        ]
        indexes = [models.Index(fields=['team', 'submitted_at'], name='reflection_team_at_idx')]
        verbose_name = '소감문 제출'
        verbose_name_plural = '소감문 제출'

    def __str__(self) -> str:
        return f'{self.respondent_id}:reflection'


class ReflectionDownloadAudit(TimeStampedModel):
    class Result(models.TextChoices):
        SUCCESS = 'success', '성공'
        FAILED = 'failed', '실패'

    admin_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='reflection_download_audits',
    )
    team = models.ForeignKey(
        Team,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='reflection_download_audits',
    )
    team_name = models.CharField(max_length=100)
    submission_count = models.PositiveIntegerField(default=0)
    result = models.CharField(max_length=20, choices=Result.choices)
    error_message = models.CharField(max_length=500, blank=True)
    requested_at = models.DateTimeField()

    class Meta:
        db_table = 'reflection_download_audits'
        indexes = [
            models.Index(fields=['team', 'requested_at'], name='refl_audit_team_at_idx'),
            models.Index(fields=['admin_user', 'requested_at'], name='refl_audit_user_at_idx'),
        ]
        verbose_name = '소감문 다운로드 감사기록'
        verbose_name_plural = '소감문 다운로드 감사기록'

    def __str__(self) -> str:
        return f'{self.team_name}:{self.requested_at}:{self.result}'


class ConsentExportAudit(TimeStampedModel):
    class Result(models.TextChoices):
        SUCCESS = 'success', '성공'
        FAILED = 'failed', '실패'

    class ExportFormat(models.TextChoices):
        EXCEL = 'xlsx', 'Excel'
        PDF = 'pdf', 'PDF'
        ZIP = 'zip', 'ZIP'

    admin_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='consent_export_audits',
    )
    team = models.ForeignKey(
        Team,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='consent_export_audits',
    )
    team_name = models.CharField(max_length=100, blank=True)
    export_format = models.CharField(max_length=10, choices=ExportFormat.choices)
    scope = models.CharField(max_length=30)
    submission_count = models.PositiveIntegerField(default=0)
    result = models.CharField(max_length=20, choices=Result.choices)
    error_message = models.CharField(max_length=500, blank=True)
    requested_at = models.DateTimeField()

    class Meta:
        db_table = 'consent_export_audits'
        indexes = [
            models.Index(fields=['team', 'requested_at'], name='cons_audit_team_at_idx'),
            models.Index(fields=['admin_user', 'requested_at'], name='cons_audit_user_at_idx'),
        ]
        verbose_name = '동의서 다운로드 감사기록'
        verbose_name_plural = '동의서 다운로드 감사기록'

    def __str__(self) -> str:
        return f'{self.team_name}:{self.export_format}:{self.requested_at}'


class AdminDownloadAudit(TimeStampedModel):
    class Result(models.TextChoices):
        SUCCESS = 'success', '성공'
        FAILED = 'failed', '실패'

    admin_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='admin_download_audits',
    )
    team = models.ForeignKey(
        Team,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='admin_download_audits',
    )
    team_name = models.CharField(max_length=100, blank=True)
    resource_type = models.CharField(max_length=40)
    export_format = models.CharField(max_length=10)
    scope = models.CharField(max_length=30)
    record_count = models.PositiveIntegerField(default=0)
    endpoint = models.CharField(max_length=255, blank=True)
    filters_json = models.JSONField(default=dict, blank=True)
    result = models.CharField(max_length=20, choices=Result.choices)
    error_message = models.CharField(max_length=500, blank=True)
    requested_at = models.DateTimeField()

    class Meta:
        db_table = 'admin_download_audits'
        indexes = [
            models.Index(fields=['team', 'requested_at'], name='download_team_at_idx'),
            models.Index(fields=['admin_user', 'requested_at'], name='download_user_at_idx'),
            models.Index(fields=['resource_type', 'requested_at'], name='download_type_at_idx'),
        ]
        verbose_name = '관리자 다운로드 감사기록'
        verbose_name_plural = '관리자 다운로드 감사기록'

    def __str__(self) -> str:
        return f'{self.resource_type}:{self.team_name}:{self.requested_at}'


@receiver(post_delete, sender=ReflectionSubmission)
def delete_reflection_pdf_after_submission_delete(sender, instance, **kwargs):
    if not instance.file_path:
        return
    file_path = instance.file_path

    def delete_file():
        from .reflection_exports import reflection_storage

        storage = reflection_storage()
        if storage.exists(file_path):
            storage.delete(file_path)

    transaction.on_commit(delete_file)


class AnonymousSurveySubmission(TimeStampedModel):
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name='anonymous_survey_submissions')
    survey_form = models.ForeignKey(SurveyForm, on_delete=models.PROTECT, related_name='anonymous_submissions')
    form_version = models.PositiveIntegerField()
    form_snapshot_json = models.JSONField(default=dict)
    answers_json = models.JSONField(default=dict)
    result_json = models.JSONField(default=dict, blank=True)
    submitted_at = models.DateTimeField()

    class Meta:
        db_table = 'anonymous_survey_submissions'
        indexes = [
            models.Index(fields=['team', 'submitted_at'], name='anon_surv_team_at_idx'),
            models.Index(fields=['survey_form', 'submitted_at'], name='anon_surv_form_at_idx'),
        ]
        verbose_name = '설문 제출'
        verbose_name_plural = '설문 제출'

    def __str__(self) -> str:
        return f'anonymous#{self.id or "new"}'


class ExitSurveySubmission(TimeStampedModel):
    team = models.ForeignKey(
        Team,
        on_delete=models.PROTECT,
        related_name='exit_survey_submissions',
    )
    respondent = models.ForeignKey(
        Respondent,
        on_delete=models.PROTECT,
        related_name='exit_survey_submissions',
    )
    survey_form = models.ForeignKey(
        SurveyForm,
        on_delete=models.PROTECT,
        related_name='identified_exit_submissions',
    )
    form_version = models.PositiveIntegerField()
    form_snapshot_json = models.JSONField(default=dict)
    answers_json = models.JSONField(default=dict)
    is_locked = models.BooleanField(default=True)
    submitted_at = models.DateTimeField()

    class Meta:
        db_table = 'exit_survey_submissions'
        constraints = [
            models.UniqueConstraint(
                fields=['team', 'respondent', 'survey_form'],
                name='exit_surv_team_resp_form_uq',
            ),
        ]
        indexes = [
            models.Index(
                fields=['team', 'submitted_at'],
                name='exit_surv_team_at_idx',
            ),
        ]
        verbose_name = '탐방 종료 설문 제출'
        verbose_name_plural = '탐방 종료 설문 제출'

    def __str__(self) -> str:
        return f'{self.respondent_id}:exit-survey:{self.survey_form_id}'


class CommonQuestionSetDeployment(TimeStampedModel):
    class Status(models.TextChoices):
        CREATED = 'created', '생성'
        SKIPPED = 'skipped', '건너뜀'
        FAILED = 'failed', '실패'

    question_set = models.ForeignKey(CommonQuestionSet, on_delete=models.PROTECT, related_name='deployments')
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name='common_question_deployments')
    survey_form = models.ForeignKey(SurveyForm, on_delete=models.SET_NULL, null=True, blank=True, related_name='common_question_deployments')
    status = models.CharField(max_length=20, choices=Status.choices)
    message = models.TextField(blank=True)
    deployed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'common_question_set_deployments'
        indexes = [
            models.Index(fields=['question_set', 'team', 'deployed_at'], name='cq_deploy_set_team_at_idx'),
            models.Index(fields=['team', 'status'], name='cq_deploy_team_status_idx'),
        ]
        verbose_name = '공통 질문 세트 배포 이력'
        verbose_name_plural = '공통 질문 세트 배포 이력'

    def __str__(self) -> str:
        return f'{self.question_set_id}->{self.team_id}:{self.status}'
