"""Guardian (법정대리인) consent state changes with an audit trail.

Participant keeps the *current* state in four cached fields; every change is also
recorded as a GuardianConsentEvent so verification history survives revocation.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .age_policy import GUARDIAN_CONSENT_DOCUMENT_VERSION
from .models import GuardianConsentEvent, Participant


class GuardianConsentNotActive(ValueError):
    """Raised when revoking a participant that has no verified consent."""


def mark_guardian_consent_verified(participant: Participant, *, actor, now=None) -> None:
    """Set the cached consent fields without saving. Pair with record_guardian_consent_event()."""
    now = now or timezone.now()
    participant.guardian_consent_verified_at = now
    participant.guardian_consent_verified_by = actor
    participant.guardian_consent_method = Participant.GuardianConsentMethod.WRITTEN
    participant.guardian_consent_document_version = GUARDIAN_CONSENT_DOCUMENT_VERSION


def record_guardian_consent_event(
    participant: Participant,
    *,
    action: str,
    actor,
    source: str,
    note: str = '',
    occurred_at=None,
) -> GuardianConsentEvent:
    return GuardianConsentEvent.objects.create(
        participant=participant,
        action=action,
        source=source,
        actor=actor if getattr(actor, 'pk', None) else None,
        document_version=GUARDIAN_CONSENT_DOCUMENT_VERSION,
        note=note,
        occurred_at=occurred_at or timezone.now(),
    )


def verify_guardian_consent(*, participant: Participant, actor, source: str, note: str = '') -> GuardianConsentEvent:
    """Mark consent verified (idempotent for the cached state) and log the event."""
    now = timezone.now()
    with transaction.atomic():
        mark_guardian_consent_verified(participant, actor=actor, now=now)
        participant.save(
            update_fields=[
                'guardian_consent_verified_at',
                'guardian_consent_verified_by',
                'guardian_consent_method',
                'guardian_consent_document_version',
                'updated_at',
            ]
        )
        return record_guardian_consent_event(
            participant,
            action=GuardianConsentEvent.Action.VERIFIED,
            actor=actor,
            source=source,
            note=note,
            occurred_at=now,
        )


def revoke_guardian_consent(*, participant: Participant, actor, reason: str) -> GuardianConsentEvent:
    """Clear the cached consent state and log a revocation. Past submissions are untouched."""
    if not participant.guardian_consent_verified_at:
        raise GuardianConsentNotActive('확인된 법정대리인 동의가 없어 취소할 수 없습니다.')
    now = timezone.now()
    with transaction.atomic():
        participant.guardian_consent_verified_at = None
        participant.guardian_consent_verified_by = None
        participant.guardian_consent_method = ''
        participant.guardian_consent_document_version = ''
        participant.save(
            update_fields=[
                'guardian_consent_verified_at',
                'guardian_consent_verified_by',
                'guardian_consent_method',
                'guardian_consent_document_version',
                'updated_at',
            ]
        )
        return record_guardian_consent_event(
            participant,
            action=GuardianConsentEvent.Action.REVOKED,
            actor=actor,
            source=GuardianConsentEvent.Source.ADMIN_REVOKE,
            note=reason,
            occurred_at=now,
        )
