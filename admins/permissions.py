from .models import AdminTeamPermission


def allowed_team_ids(user) -> list[int]:
    if not getattr(user, "is_authenticated", False):
        return []
    if user.is_superuser:
        return []
    return list(AdminTeamPermission.objects.filter(user=user).values_list("team_id", flat=True))
