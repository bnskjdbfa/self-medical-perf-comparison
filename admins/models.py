from django.conf import settings
from django.db import models

from teams.models import Team, TimeStampedModel


class AdminTeamPermission(TimeStampedModel):
    class Roles(models.TextChoices):
        READ_ONLY_TEAM_ADMIN = "read_only_team_admin", "조회전용 팀 관리자"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="team_permissions")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="admin_permissions")
    role = models.CharField(max_length=30, choices=Roles.choices, default=Roles.READ_ONLY_TEAM_ADMIN)

    class Meta:
        db_table = "admin_team_permissions"
        constraints = [models.UniqueConstraint(fields=["user", "team"], name="adm_team_perm_user_team_uq")]
        indexes = [models.Index(fields=["team"], name="adm_team_perm_team_idx")]
        verbose_name = '관리자 팀 권한'
        verbose_name_plural = '관리자 팀 권한'

    def __str__(self) -> str:
        return f"{self.user_id}:{self.team_id}:{self.role}"


class AdminRecoveryCode(TimeStampedModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="admin_recovery_codes",
    )
    token_hash = models.CharField(max_length=128)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "admin_recovery_codes"
        indexes = [models.Index(fields=["user", "used_at"], name="adm_recovery_user_used_idx")]
        verbose_name = "관리자 2단계 인증 복구 코드"
        verbose_name_plural = "관리자 2단계 인증 복구 코드"
