from datetime import date

from django.conf import settings
from django.db import models
from django.db.models import Q
from .validators import validate_reflection_image


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class GenderChoices(models.TextChoices):
    MALE = 'M', '남'
    FEMALE = 'F', '여'
    UNSPECIFIED = 'U', '미입력'


class Team(TimeStampedModel):
    name = models.CharField(max_length=100, verbose_name='팀명')
    organization_name = models.CharField(
        max_length=100,
        verbose_name='운영기관',
        help_text='국문 동의서와 서약서 등 공식 문서에 표시할 기관명입니다.',
    )
    organization_name_en = models.CharField(
        max_length=150,
        verbose_name='운영기관(영문)',
        help_text='영문 설문지와 공식 문서에 표시할 영문 기관명입니다.',
    )
    code = models.CharField(max_length=50, unique=True)
    manager_entry_code = models.CharField(max_length=100, unique=True, null=True, blank=True)
    slug = models.SlugField(max_length=100, unique=True, null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    start_date = models.DateField()
    max_day_no = models.PositiveSmallIntegerField()
    description = models.TextField(blank=True)
    reflection_image = models.FileField(
        '소감문 안내 이미지',
        upload_to='teams/reflection/',
        blank=True,
        validators=[validate_reflection_image],
        help_text='선택 사항 · JPG, PNG, WebP · 최대 5MB',
    )

    class Meta:
        db_table = "teams"
        constraints = [
            models.CheckConstraint(check=Q(max_day_no__gte=1), name="teams_max_day_no_gte_1"),
        ]
        verbose_name = '팀'
        verbose_name_plural = '팀'

    def __str__(self) -> str:
        return f"{self.name}({self.code})"

    def calculate_day_no(self, today: date | None = None) -> int:
        today = today or date.today()
        return (today - self.start_date).days + 1


class Respondent(TimeStampedModel):
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="respondents", db_index=True)
    name = models.CharField(max_length=50)
    birth_date = models.DateField()
    # 2026-09-11: 전화번호는 식별 키에서 제외됐다. 컬럼은 과거 데이터 보존용으로만 남고 새 코드는 쓰지 않는다.
    phone = models.CharField(max_length=20, blank=True)
    gender = models.CharField(max_length=1, choices=GenderChoices.choices, null=True, blank=True)
    normalized_name = models.CharField(max_length=50)
    normalized_phone = models.CharField(max_length=20, blank=True)

    class Meta:
        db_table = "respondents"
        constraints = [
            models.UniqueConstraint(
                fields=["team", "normalized_name", "birth_date", "gender"],
                name="respondents_team_identity_v2_uq",
            )
        ]
        indexes = [models.Index(fields=["team"], name="respondents_team_idx")]
        verbose_name = '응답자'
        verbose_name_plural = '응답자'

    def __str__(self) -> str:
        return f"{self.name} ({self.team_id})"


class Participant(TimeStampedModel):
    class GuardianConsentMethod(models.TextChoices):
        WRITTEN = 'written', '서면 동의서'

    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='participants', db_index=True)
    name = models.CharField(max_length=50)
    birth_date = models.DateField(null=True, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    gender = models.CharField(max_length=1, choices=GenderChoices.choices, null=True, blank=True)
    normalized_name = models.CharField(max_length=50)
    normalized_phone = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    guardian_consent_verified_at = models.DateTimeField(null=True, blank=True, verbose_name='법정대리인 동의 확인일시')
    guardian_consent_verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='verified_guardian_consents',
        verbose_name='법정대리인 동의 확인자',
    )
    guardian_consent_method = models.CharField(
        max_length=20,
        choices=GuardianConsentMethod.choices,
        blank=True,
        verbose_name='법정대리인 동의 확인방법',
    )
    guardian_consent_document_version = models.CharField(
        max_length=40,
        blank=True,
        verbose_name='법정대리인 동의서 버전',
    )

    class Meta:
        db_table = 'participants'
        constraints = [
            models.UniqueConstraint(
                fields=['team', 'normalized_name', 'birth_date', 'gender'],
                name='participants_team_identity_v2_uq',
            )
        ]
        indexes = [models.Index(fields=['team', 'is_active'], name='participants_team_active_idx')]
        verbose_name = '대상자 명단'
        verbose_name_plural = '대상자 명단'

    def __str__(self) -> str:
        return f'{self.name} ({self.team_id})'


class GuardianConsentEvent(TimeStampedModel):
    """Audit trail for guardian (법정대리인) consent verification and revocation."""

    class Action(models.TextChoices):
        VERIFIED = 'verified', '확인'
        REVOKED = 'revoked', '취소'

    class Source(models.TextChoices):
        ADMIN_FORM = 'admin_form', '개별 등록 화면'
        BULK_UPLOAD = 'bulk_upload', '명단 일괄 등록'
        ADMIN_REVOKE = 'admin_revoke', '관리자 취소'
        BACKFILL = 'backfill', '기존 기록 이관'

    participant = models.ForeignKey(
        Participant,
        on_delete=models.CASCADE,
        related_name='guardian_consent_events',
        verbose_name='참가자',
    )
    action = models.CharField(max_length=20, choices=Action.choices, verbose_name='행위')
    source = models.CharField(max_length=20, choices=Source.choices, verbose_name='경로')
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='guardian_consent_events',
        verbose_name='처리자',
    )
    document_version = models.CharField(max_length=40, blank=True, verbose_name='동의서 버전')
    note = models.TextField(blank=True, verbose_name='사유·메모')
    occurred_at = models.DateTimeField(verbose_name='처리 일시')

    class Meta:
        db_table = 'participant_guardian_consent_events'
        ordering = ['-occurred_at', '-id']
        indexes = [models.Index(fields=['participant', 'occurred_at'], name='guardian_evt_part_at_idx')]
        verbose_name = '법정대리인 동의 이력'
        verbose_name_plural = '법정대리인 동의 이력'

    def __str__(self) -> str:
        return f'{self.participant_id}:{self.action}@{self.occurred_at:%Y-%m-%d %H:%M}'


class ParticipantUploadAudit(TimeStampedModel):
    """One row per confirmed bulk roster upload."""

    admin_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='participant_upload_audits',
        verbose_name='업로드 관리자',
    )
    team = models.ForeignKey(
        Team,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='participant_upload_audits',
        verbose_name='팀',
    )
    team_name = models.CharField(max_length=100, verbose_name='팀명')
    file_name = models.CharField(max_length=255, blank=True, verbose_name='파일명')
    total_rows = models.PositiveIntegerField(default=0, verbose_name='전체 행')
    created_count = models.PositiveIntegerField(default=0, verbose_name='생성')
    updated_count = models.PositiveIntegerField(default=0, verbose_name='갱신')
    skipped_count = models.PositiveIntegerField(default=0, verbose_name='건너뜀')
    error_count = models.PositiveIntegerField(default=0, verbose_name='오류')
    pending_consent_count = models.PositiveIntegerField(default=0, verbose_name='동의 대기')
    requested_at = models.DateTimeField(verbose_name='업로드 일시')

    class Meta:
        db_table = 'participant_upload_audits'
        ordering = ['-requested_at', '-id']
        verbose_name = '명단 일괄 등록 이력'
        verbose_name_plural = '명단 일괄 등록 이력'

    def __str__(self) -> str:
        return f'{self.team_name} {self.requested_at:%Y-%m-%d %H:%M} (+{self.created_count})'
