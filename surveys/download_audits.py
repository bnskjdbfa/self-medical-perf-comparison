from django.utils import timezone

from .models import AdminDownloadAudit


def record_admin_download(
    request,
    *,
    resource_type: str,
    export_format: str,
    scope: str,
    result: str,
    team=None,
    record_count: int = 0,
    error_message: str = '',
    filters: dict | None = None,
):
    safe_filters = {
        str(key)[:50]: str(value)[:200]
        for key, value in (filters or {}).items()
        if value not in (None, '')
    }
    return AdminDownloadAudit.objects.create(
        admin_user=request.user if request.user.is_authenticated else None,
        team=team,
        team_name=team.name if team else '',
        resource_type=resource_type[:40],
        export_format=export_format[:10],
        scope=scope[:30],
        record_count=max(0, record_count),
        endpoint=request.path[:255],
        filters_json=safe_filters,
        result=result,
        error_message=(error_message or '')[:500],
        requested_at=timezone.now(),
    )
