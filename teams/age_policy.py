from datetime import date


MINIMUM_PARTICIPANT_AGE = 14
GUARDIAN_CONSENT_DOCUMENT_VERSION = 'guardian-written-v1'


def age_on_date(birth_date: date, reference_date: date) -> int:
    return reference_date.year - birth_date.year - (
        (reference_date.month, reference_date.day) < (birth_date.month, birth_date.day)
    )


def is_eligible_participant_age(birth_date: date | None, reference_date: date) -> bool:
    return bool(birth_date and age_on_date(birth_date, reference_date) >= MINIMUM_PARTICIPANT_AGE)


def participant_has_required_guardian_consent(participant) -> bool:
    if is_eligible_participant_age(participant.birth_date, participant.team.start_date):
        return True
    return bool(
        participant.guardian_consent_verified_at
        and participant.guardian_consent_method == 'written'
        and participant.guardian_consent_document_version
    )


def underage_birth_date_threshold(reference_date: date) -> date:
    """Latest birth date that still counts as MINIMUM_PARTICIPANT_AGE on ``reference_date``.

    ``birth_date <= threshold`` means eligible; ``birth_date > threshold`` means under age.
    Mirrors ``age_on_date`` so the two can be used interchangeably (Python vs. queryset).
    """
    year = reference_date.year - MINIMUM_PARTICIPANT_AGE
    try:
        return reference_date.replace(year=year)
    except ValueError:  # Feb 29 on a non-leap year
        return reference_date.replace(year=year, day=28)


def underage_participant_filter(reference_date: date):
    """Q object selecting participants who need guardian consent on ``reference_date``."""
    from django.db.models import Q

    threshold = underage_birth_date_threshold(reference_date)
    return Q(birth_date__gt=threshold) | Q(birth_date__isnull=True)


def unverified_underage_participants(team, *, reference_date: date | None = None):
    """Participants of ``team`` who are under age on the reference date and have no verified consent."""
    from .models import Participant

    reference_date = reference_date or team.start_date
    return (
        Participant.objects.filter(team=team)
        .filter(underage_participant_filter(reference_date))
        .filter(guardian_consent_verified_at__isnull=True)
        .order_by('name', 'pk')
    )
