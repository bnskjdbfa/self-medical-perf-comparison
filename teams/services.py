from datetime import date

from django.utils import timezone

from .models import Team


def calculate_team_day_no(team: Team, today: date | None = None) -> int:
    """Policy: day_no = (today - team.start_date) + 1. Final authority is backend."""
    reference_date = today or timezone.localdate()
    return team.calculate_day_no(today=reference_date)


def is_team_entry_open(team: Team, today: date | None = None) -> bool:
    """Allow user entry only during the configured survey day window."""
    day_no = calculate_team_day_no(team, today=today)
    return 1 <= day_no <= team.max_day_no
