from __future__ import annotations

from collections.abc import Iterable

from django.db import transaction

from surveys.models import (
    DocumentSubmission,
    ExitSurveySubmission,
    ReflectionSubmission,
    RequiredDocumentConsentBundle,
    SurveySubmission,
)

from .models import Respondent


def purge_respondents(*, respondent_ids: Iterable[int]) -> int:
    """Delete selected respondents and their identified submission records."""
    ids = tuple(dict.fromkeys(respondent_ids))
    if not ids:
        return 0

    with transaction.atomic():
        respondents = Respondent.objects.select_for_update().filter(pk__in=ids)
        locked_ids = tuple(respondents.values_list('pk', flat=True))
        if not locked_ids:
            return 0

        SurveySubmission.objects.filter(respondent_id__in=locked_ids).delete()
        ExitSurveySubmission.objects.filter(respondent_id__in=locked_ids).delete()
        DocumentSubmission.objects.filter(respondent_id__in=locked_ids).delete()
        ReflectionSubmission.objects.filter(respondent_id__in=locked_ids).delete()
        RequiredDocumentConsentBundle.objects.filter(
            respondent_id__in=locked_ids,
        ).delete()
        Respondent.objects.filter(pk__in=locked_ids).delete()

    return len(locked_ids)
