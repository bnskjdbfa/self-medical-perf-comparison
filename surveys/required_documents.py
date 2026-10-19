from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib

from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.utils import timezone
from config.db_limits import bounded_atomic, policy_row

from teams.models import Participant, Respondent, Team
from teams.age_policy import (
    is_eligible_participant_age,
    participant_has_required_guardian_consent,
)
from .models import (
    DocumentSubmission,
    DocumentTemplate,
    RequiredDocumentConsentBundle,
    TeamFeatureSetting,
)


REQUIRED_DOCUMENT_ORDER = (
    DocumentTemplate.DocumentType.PRIVACY_CONSENT,
    DocumentTemplate.DocumentType.THIRD_PARTY_CONSENT,
    DocumentTemplate.DocumentType.SAFETY_PLEDGE,
)

FEATURE_FIELD_BY_DOCUMENT_TYPE = {
    DocumentTemplate.DocumentType.PRIVACY_CONSENT: 'enable_privacy_consent',
    DocumentTemplate.DocumentType.THIRD_PARTY_CONSENT: 'enable_third_party_consent',
    DocumentTemplate.DocumentType.SAFETY_PLEDGE: 'enable_safety_pledge',
}

STATIC_REQUIRED_DOCUMENT_VERSION = 3
REQUIRED_DOCUMENT_UI = {
    DocumentTemplate.DocumentType.PRIVACY_CONSENT: {
        'hero_title': '개인정보 수집 및 이용 동의',
        'hero_title_en': 'Consent to Collection and Use of Personal Information',
        'section_title': '개인정보 수집·이용 동의서',
        'section_title_en': 'Consent to Collection and Use of Personal Information',
        'document_heading': '개인정보 수집·이용 동의서',
        'document_heading_en': 'Consent to Collection and Use of Personal Information',
        'agreement_label': '개인정보 수집 및 이용 내용을 확인했으며 이에 동의합니다.',
        'submit_label': '동의하고 계속하기',
        'submit_label_en': 'Agree and Continue',
    },
    DocumentTemplate.DocumentType.THIRD_PARTY_CONSENT: {
        'hero_title': '제 3자 정보제공 동의서',
        'hero_title_en': 'Consent to Provision of Information to a Third Party',
        'section_title': '제 3자 정보제공 동의서',
        'section_title_en': 'Consent to Provision of Personal Information',
        'document_heading': '제 3자 정보제공 동의서',
        'document_heading_en': 'Consent to Provision of Personal Information to Third Party',
        'agreement_label': '제 3자 정보제공 내용을 확인했으며 이에 동의합니다.',
        'submit_label': '동의하고 계속하기',
        'submit_label_en': 'Agree and Continue',
    },
    DocumentTemplate.DocumentType.SAFETY_PLEDGE: {
        'hero_title': '여행 안전 수칙 서약서',
        'hero_title_en': 'Travel Safety Guidelines Pledge',
        'section_title': '여행 안전수칙 서약서',
        'section_title_en': 'Pledge on Travel Safety Guidelines',
        'document_heading': '여행 안전수칙 서약서',
        'document_heading_en': 'Travel Safety Guidelines Pledge',
        'agreement_label': '여행 안전수칙을 확인했으며 성실히 준수할 것을 서약합니다.',
        'submit_label': '동의하고 계속하기',
        'submit_label_en': 'Agree and Continue',
    },
}


class RequiredDocumentError(Exception):
    pass


class RequiredDocumentAlreadySubmitted(RequiredDocumentError):
    pass


class RequiredDocumentTemplateInvalid(RequiredDocumentError):
    pass


class RequiredDocumentEvidenceChanged(RequiredDocumentTemplateInvalid):
    def __init__(self, document_type: str):
        self.document_type = document_type
        super().__init__('동의서 내용이 변경되어 다시 확인이 필요합니다.')


class ParticipantIdentityMismatch(RequiredDocumentError):
    pass


class GuardianConsentNotVerified(ParticipantIdentityMismatch):
    pass


@dataclass(frozen=True)
class RequiredDocumentProgress:
    enabled_types: tuple[str, ...]
    submitted_types: frozenset[str]

    @property
    def total(self) -> int:
        return len(self.enabled_types)

    @property
    def completed(self) -> int:
        return sum(document_type in self.submitted_types for document_type in self.enabled_types)

    @property
    def is_complete(self) -> bool:
        return self.completed == self.total


def get_enabled_required_document_types(team: Team) -> tuple[str, ...]:
    feature_settings = TeamFeatureSetting.objects.filter(team=team).first()
    if not feature_settings:
        return ()
    return tuple(
        document_type
        for document_type in REQUIRED_DOCUMENT_ORDER
        if getattr(feature_settings, FEATURE_FIELD_BY_DOCUMENT_TYPE[document_type], False)
    )


def get_active_document_template(*, team: Team, document_type: str) -> DocumentTemplate | None:
    return (
        DocumentTemplate.objects.filter(
            team=team,
            document_type=document_type,
            is_active=True,
        )
        .order_by('-version', '-updated_at', '-id')
        .first()
    )


def get_submitted_required_document_types(*, team: Team, respondent: Respondent) -> frozenset[str]:
    return frozenset(
        DocumentSubmission.objects.filter(team=team, respondent=respondent)
        .values_list('document_type', flat=True)
    )


def get_required_document_progress(*, team: Team, respondent: Respondent) -> RequiredDocumentProgress:
    return RequiredDocumentProgress(
        enabled_types=get_enabled_required_document_types(team),
        submitted_types=get_submitted_required_document_types(team=team, respondent=respondent),
    )


def get_next_pending_required_document(*, team: Team, respondent: Respondent) -> str | None:
    progress = get_required_document_progress(team=team, respondent=respondent)
    return next(
        (
            document_type
            for document_type in progress.enabled_types
            if document_type not in progress.submitted_types
        ),
        None,
    )


def respondent_lacks_guardian_consent(*, team: Team, respondent: Respondent) -> bool:
    """True when the respondent is under age on the team start date and no active roster row
    carries verified guardian consent. Used at every submission so a revocation takes effect
    immediately, not only at the next login."""
    from teams.age_policy import is_eligible_participant_age

    if is_eligible_participant_age(respondent.birth_date, team.start_date):
        return False
    participant = _find_matching_participant(team=team, respondent=respondent)
    return participant is None or not participant_has_required_guardian_consent(participant)


def required_documents_completed(*, team: Team, respondent: Respondent) -> bool:
    return get_next_pending_required_document(team=team, respondent=respondent) is None


def _find_matching_participant(
    *,
    team: Team,
    respondent: Respondent,
    lock: bool = False,
) -> Participant | None:
    queryset = Participant.objects
    if lock:
        queryset = queryset.select_for_update()
    return (
        queryset.filter(
            team=team,
            is_active=True,
            normalized_name__iexact=respondent.normalized_name,
            birth_date=respondent.birth_date,
            gender=respondent.gender,
        )
        .order_by('pk')
        .first()
    )


def build_participant_snapshot(
    respondent: Respondent,
    *,
    participant: Participant | None = None,
) -> dict[str, object]:
    snapshot: dict[str, object] = {
        'name': respondent.name,
        'birth_date': respondent.birth_date.isoformat(),
        'phone': respondent.phone,
        'gender': respondent.gender or '',
    }
    if not is_eligible_participant_age(respondent.birth_date, respondent.team.start_date):
        participant = participant or _find_matching_participant(
            team=respondent.team,
            respondent=respondent,
        )
        if participant and participant_has_required_guardian_consent(participant):
            snapshot['guardian_consent'] = {
                'verified': True,
                'method': participant.guardian_consent_method,
                'verified_at': participant.guardian_consent_verified_at.isoformat(),
                'verified_by_user_id': participant.guardian_consent_verified_by_id,
                'document_version': participant.guardian_consent_document_version,
            }
    return snapshot


def build_team_snapshot(team: Team) -> dict[str, str | int]:
    return {
        'team_id': team.id,
        'team_name': team.name,
        'organization_name': team.organization_name,
        'organization_name_en': team.organization_name_en,
    }


def build_enabled_document_types_snapshot(team: Team) -> list[str]:
    return list(get_enabled_required_document_types(team))


def normalize_document_content(content: str) -> str:
    return content.replace('\r\n', '\n').replace('\r', '\n').strip()


def render_canonical_document(
    *,
    team: Team,
    document_type: str,
    language: str = 'ko',
) -> tuple[str, str]:
    evidence = build_canonical_document_evidence(
        team=team,
        document_type=document_type,
        language=language,
    )
    return evidence['title'], evidence['content']


def build_canonical_document_evidence(
    *,
    team: Team,
    document_type: str,
    language: str = 'ko',
) -> dict[str, str | int]:
    document_ui = REQUIRED_DOCUMENT_UI.get(document_type)
    if not document_ui:
        raise RequiredDocumentTemplateInvalid('지원하지 않는 필수 문서 유형입니다.')
    normalized_language = 'en' if language == 'en' else 'ko'
    content = render_to_string(
        'surveys/public_v2/_canonical_document.html',
        {
            'team': team,
            'document_type': document_type,
            'document_ui': document_ui,
            'page_language': normalized_language,
        },
    )
    normalized_content = normalize_document_content(content)
    if not normalized_content:
        raise RequiredDocumentTemplateInvalid('필수 문서 본문을 생성할 수 없습니다.')
    return {
        'document_type': document_type,
        'language': normalized_language,
        'title': (
            document_ui['document_heading_en']
            if normalized_language == 'en' and document_ui['document_heading_en']
            else document_ui['document_heading']
        ),
        'version': STATIC_REQUIRED_DOCUMENT_VERSION,
        'content': normalized_content,
        'content_hash': get_document_content_hash(normalized_content),
    }


def get_document_content_hash(content: str) -> str:
    return hashlib.sha256(normalize_document_content(content).encode('utf-8')).hexdigest()


def _get_or_create_bundle(
    *,
    team: Team,
    respondent: Respondent,
    participant: Participant | None = None,
    enabled_document_types: list[str] | None = None,
) -> RequiredDocumentConsentBundle:
    bundle, _ = RequiredDocumentConsentBundle.objects.get_or_create(
        team=team,
        respondent=respondent,
        defaults={
            'participant_snapshot_json': build_participant_snapshot(
                respondent,
                participant=participant,
            ),
            'team_snapshot_json': build_team_snapshot(team),
            'enabled_document_types_snapshot': (
                build_enabled_document_types_snapshot(team)
                if enabled_document_types is None else list(enabled_document_types)
            ),
            'snapshot_schema_version': 2,
            'snapshot_source': RequiredDocumentConsentBundle.SnapshotSource.LIVE_SUBMIT,
        },
    )
    return bundle


def _mark_bundle_complete_if_ready(
    bundle: RequiredDocumentConsentBundle,
    *,
    completed_at=None,
) -> None:
    required_types = set(bundle.enabled_document_types_snapshot)
    submitted_types = set(bundle.submissions.values_list('document_type', flat=True))
    new_completed_at = (completed_at or timezone.now()) if required_types.issubset(submitted_types) else None
    if bundle.completed_at != new_completed_at:
        bundle.completed_at = new_completed_at
        bundle.save(update_fields=['completed_at', 'updated_at'])


def create_required_document_submission(
    *,
    team: Team,
    respondent: Respondent,
    document_type: str,
    template: DocumentTemplate,
) -> DocumentSubmission:
    if respondent.team_id != team.id:
        raise RequiredDocumentTemplateInvalid('응답자와 팀 정보가 일치하지 않습니다.')
    if document_type not in get_enabled_required_document_types(team):
        raise RequiredDocumentTemplateInvalid('현재 팀에서 활성화되지 않은 문서입니다.')
    if DocumentSubmission.objects.filter(
        team=team,
        respondent=respondent,
        document_type=document_type,
    ).exists():
        raise RequiredDocumentAlreadySubmitted('이미 제출한 문서입니다.')
    if get_next_pending_required_document(team=team, respondent=respondent) != document_type:
        raise RequiredDocumentTemplateInvalid('필수 문서는 정해진 순서대로 제출해야 합니다.')
    if template.team_id != team.id or template.document_type != document_type or not template.is_active:
        raise RequiredDocumentTemplateInvalid('현재 제출에 사용할 수 없는 문서 템플릿입니다.')
    active_template = get_active_document_template(team=team, document_type=document_type)
    if not active_template or active_template.id != template.id:
        raise RequiredDocumentTemplateInvalid('최신 활성 문서 템플릿이 아닙니다.')
    try:
        with bounded_atomic():
            Respondent.objects.select_for_update(nowait=True).get(pk=respondent.pk, team=team)
            bundle = _get_or_create_bundle(team=team, respondent=respondent)
            content = normalize_document_content(template.content_html)
            if not content:
                raise RequiredDocumentTemplateInvalid('필수 문서 본문을 생성할 수 없습니다.')
            submission = DocumentSubmission.objects.create(
                team=team,
                respondent=respondent,
                bundle=bundle,
                template=template,
                document_type=document_type,
                template_version=template.version,
                template_hash_at_submit=template.body_hash,
                document_title_snapshot=template.title,
                document_content_snapshot=content,
                document_content_hash_at_submit=get_document_content_hash(content),
                snapshot_source=DocumentSubmission.SnapshotSource.LIVE_SUBMIT,
                agreed=True,
                signer_name=respondent.name,
                signature_method=DocumentSubmission.SignatureMethod.CHECKBOX,
                agreed_at=timezone.now(),
            )
            _mark_bundle_complete_if_ready(bundle, completed_at=submission.agreed_at)
            return submission
    except IntegrityError as exc:
        raise RequiredDocumentAlreadySubmitted('이미 제출한 문서입니다.') from exc


def create_required_document_submissions(*, team: Team, respondent: Respondent) -> list[DocumentSubmission]:
    pending_types = [
        document_type
        for document_type in get_enabled_required_document_types(team)
        if document_type not in get_submitted_required_document_types(team=team, respondent=respondent)
    ]
    agreed_at = timezone.now()
    evidence_by_type = {
        document_type: build_canonical_document_evidence(
            team=team,
            document_type=document_type,
        )
        for document_type in pending_types
    }
    return create_required_document_submissions_from_consents(
        team=team,
        respondent=respondent,
        agreed_at_by_document_type={
            document_type: agreed_at
            for document_type in pending_types
        },
        expected_fingerprints_by_document_type={
            document_type: {
                'document_type': document_type,
                'document_version': evidence['version'],
                'document_content_hash': evidence['content_hash'],
            }
            for document_type, evidence in evidence_by_type.items()
        },
    )


def create_respondent_with_privacy_consent(
    *,
    team: Team,
    name: str,
    birth_date,
    gender: str,
    agreed_at: datetime,
    expected_fingerprint: dict,
) -> Respondent:
    from .services import find_respondent_by_identity, normalize_name

    if (
        not isinstance(agreed_at, datetime)
        or not timezone.is_aware(agreed_at)
    ):
        raise RequiredDocumentTemplateInvalid('개인정보 동의 시각이 올바르지 않습니다.')

    document_type = DocumentTemplate.DocumentType.PRIVACY_CONSENT
    try:
        with bounded_atomic():
            locked_team = policy_row(Team, pk=team.pk, is_active=True)
            if locked_team is None:
                raise ParticipantIdentityMismatch('현재 운영 중인 팀이 아닙니다.')
            feature_settings = policy_row(TeamFeatureSetting, team=locked_team)
            if (
                not feature_settings
                or not feature_settings.enable_privacy_consent
            ):
                raise RequiredDocumentTemplateInvalid(
                    '현재 팀에서 개인정보 동의를 사용하지 않습니다.'
                )

            participant = (
                Participant.objects.select_for_update(nowait=True)
                .filter(
                    team=locked_team,
                    is_active=True,
                    normalized_name__iexact=normalize_name(name),
                    birth_date=birth_date,
                    gender=gender,
                )
                .order_by('pk')
                .first()
            )
            if participant is None:
                raise ParticipantIdentityMismatch(
                    '입력한 정보가 참가자 명단과 일치하지 않습니다.'
                )
            if not participant_has_required_guardian_consent(participant):
                raise GuardianConsentNotVerified(
                    '법정대리인 서면 동의 확인이 완료되지 않았습니다.'
                )

            evidence = build_canonical_document_evidence(
                team=locked_team,
                document_type=document_type,
                language=(
                    'en'
                    if expected_fingerprint.get('language') == 'en'
                    else 'ko'
                ),
            )
            if (
                expected_fingerprint.get('document_type') != document_type
                or expected_fingerprint.get('language', 'ko') != evidence['language']
                or expected_fingerprint.get('document_version') != evidence['version']
                or expected_fingerprint.get('document_content_hash') != evidence['content_hash']
            ):
                raise RequiredDocumentEvidenceChanged(document_type)

            respondent = find_respondent_by_identity(
                team=locked_team,
                name=participant.name,
                birth_date=participant.birth_date,
                gender=participant.gender,
                lock=True,
            )
            if respondent is None:
                respondent = Respondent.objects.create(
                    team=locked_team,
                    normalized_name=participant.normalized_name,
                    birth_date=participant.birth_date,
                    name=participant.name,
                    gender=participant.gender,
                    phone=participant.phone,
                    normalized_phone=participant.normalized_phone,
                )
            changed_fields = []
            # 전화는 식별에 쓰지 않지만 연락처로 명단 값을 따라간다.
            for field_name in ('name', 'gender', 'phone', 'normalized_phone'):
                participant_value = getattr(participant, field_name)
                if getattr(respondent, field_name) != participant_value:
                    setattr(respondent, field_name, participant_value)
                    changed_fields.append(field_name)
            if changed_fields:
                respondent.save(update_fields=[*changed_fields, 'updated_at'])

            existing_submission = (
                DocumentSubmission.objects.select_for_update()
                .filter(
                    team=locked_team,
                    respondent=respondent,
                    document_type=document_type,
                )
                .first()
            )
            if existing_submission is not None:
                return respondent

            bundle = _get_or_create_bundle(
                team=locked_team,
                respondent=respondent,
                participant=participant,
                enabled_document_types=[
                    kind for kind in REQUIRED_DOCUMENT_ORDER
                    if getattr(feature_settings, FEATURE_FIELD_BY_DOCUMENT_TYPE[kind], False)
                ],
            )
            submission = DocumentSubmission.objects.create(
                team=locked_team,
                respondent=respondent,
                bundle=bundle,
                template=None,
                document_type=document_type,
                template_version=evidence['version'],
                template_hash_at_submit=evidence['content_hash'],
                document_title_snapshot=evidence['title'],
                document_content_snapshot=evidence['content'],
                document_content_hash_at_submit=evidence['content_hash'],
                snapshot_source=DocumentSubmission.SnapshotSource.LIVE_SUBMIT,
                agreed=True,
                signer_name=respondent.name,
                signature_method=DocumentSubmission.SignatureMethod.CHECKBOX,
                agreed_at=agreed_at,
            )
            _mark_bundle_complete_if_ready(bundle, completed_at=submission.agreed_at)
            return respondent
    except IntegrityError:
        respondent = find_respondent_by_identity(
            team=team,
            name=name,
            birth_date=birth_date,
            gender=gender,
        )
        if (
            respondent is not None
            and DocumentSubmission.objects.filter(
                team_id=team.id,
                respondent=respondent,
                document_type=document_type,
            ).exists()
        ):
            return respondent
        raise


def create_required_document_submissions_from_consents(
    *,
    team: Team,
    respondent: Respondent,
    agreed_at_by_document_type: dict[str, object],
    expected_fingerprints_by_document_type: dict[str, dict],
) -> list[DocumentSubmission]:
    if respondent.team_id != team.id:
        raise RequiredDocumentTemplateInvalid('응답자와 팀 정보가 일치하지 않습니다.')

    try:
        with bounded_atomic():
            locked_team = policy_row(Team, pk=team.pk)
            feature_settings = policy_row(TeamFeatureSetting, team=locked_team)
            locked_respondent = Respondent.objects.select_for_update(nowait=True).get(
                pk=respondent.pk,
                team=locked_team,
            )
            participant = _find_matching_participant(
                team=locked_team,
                respondent=locked_respondent,
                lock=True,
            )
            if (
                not is_eligible_participant_age(
                    locked_respondent.birth_date,
                    locked_team.start_date,
                )
                and (
                    participant is None
                    or not participant_has_required_guardian_consent(participant)
                )
            ):
                raise GuardianConsentNotVerified(
                    '법정대리인 서면 동의 확인이 완료되지 않았습니다.'
                )
            enabled_types = tuple(
                document_type
                for document_type in REQUIRED_DOCUMENT_ORDER
                if feature_settings
                and getattr(
                    feature_settings,
                    FEATURE_FIELD_BY_DOCUMENT_TYPE[document_type],
                    False,
                )
            )
            submitted_types = frozenset(
                DocumentSubmission.objects.select_for_update()
                .filter(team=locked_team, respondent=locked_respondent)
                .values_list('document_type', flat=True)
            )
            pending_types = [
                document_type
                for document_type in enabled_types
                if document_type not in submitted_types
            ]
            if not pending_types:
                requested_types = set(expected_fingerprints_by_document_type)
                enabled_type_set = set(enabled_types)
                if (
                    requested_types == enabled_type_set
                    and enabled_type_set.issubset(submitted_types)
                ):
                    return []
                raise RequiredDocumentAlreadySubmitted('이미 제출한 문서입니다.')
            if set(agreed_at_by_document_type) != set(pending_types):
                raise RequiredDocumentTemplateInvalid('모든 미제출 필수 문서의 동의 기록이 필요합니다.')
            if set(expected_fingerprints_by_document_type) != set(pending_types):
                raise RequiredDocumentTemplateInvalid('필수 문서 증빙 데이터가 올바르지 않습니다.')
            if any(
                not isinstance(agreed_at_by_document_type[document_type], datetime)
                or not timezone.is_aware(agreed_at_by_document_type[document_type])
                for document_type in pending_types
            ):
                raise RequiredDocumentTemplateInvalid('문서 동의 시각이 올바르지 않습니다.')

            canonical_evidence_by_document_type = {
                document_type: build_canonical_document_evidence(
                    team=locked_team,
                    document_type=document_type,
                    language=(
                        'en'
                        if expected_fingerprints_by_document_type[document_type].get('language') == 'en'
                        else 'ko'
                    ),
                )
                for document_type in pending_types
            }
            for document_type in pending_types:
                evidence = canonical_evidence_by_document_type[document_type]
                expected = expected_fingerprints_by_document_type[document_type]
                if (
                    evidence.get('document_type') != document_type
                    or evidence.get('version') != STATIC_REQUIRED_DOCUMENT_VERSION
                    or not evidence.get('title')
                    or not evidence.get('content')
                    or evidence.get('content_hash')
                    != get_document_content_hash(evidence.get('content', ''))
                ):
                    raise RequiredDocumentTemplateInvalid('필수 문서 증빙 데이터가 올바르지 않습니다.')
                if (
                    expected.get('document_type') != document_type
                    or expected.get('language', 'ko') != evidence['language']
                    or expected.get('document_version') != evidence['version']
                    or expected.get('document_content_hash') != evidence['content_hash']
                ):
                    raise RequiredDocumentEvidenceChanged(document_type)

            bundle = (
                RequiredDocumentConsentBundle.objects.select_for_update()
                .filter(team=locked_team, respondent=locked_respondent)
                .first()
            )
            if bundle is None:
                bundle = _get_or_create_bundle(
                    team=locked_team,
                    respondent=locked_respondent,
                    participant=participant,
                )
            submissions = []
            for document_type in pending_types:
                evidence = canonical_evidence_by_document_type[document_type]
                title = evidence['title']
                content = evidence['content']
                content_hash = evidence['content_hash']
                submissions.append(DocumentSubmission.objects.create(
                    team=locked_team,
                    respondent=locked_respondent,
                    bundle=bundle,
                    template=None,
                    document_type=document_type,
                    template_version=evidence['version'],
                    template_hash_at_submit=content_hash,
                    document_title_snapshot=title,
                    document_content_snapshot=content,
                    document_content_hash_at_submit=content_hash,
                    snapshot_source=DocumentSubmission.SnapshotSource.LIVE_SUBMIT,
                    agreed=True,
                    signer_name=locked_respondent.name,
                    signature_method=DocumentSubmission.SignatureMethod.CHECKBOX,
                    agreed_at=agreed_at_by_document_type[document_type],
                ))
            _mark_bundle_complete_if_ready(bundle, completed_at=timezone.now())
            return submissions
    except IntegrityError as exc:
        requested_types = set(expected_fingerprints_by_document_type)
        submitted_types = set(
            DocumentSubmission.objects.filter(
                team_id=team.id,
                respondent_id=respondent.id,
            ).values_list('document_type', flat=True)
        )
        if requested_types and requested_types.issubset(submitted_types):
            return []
        if requested_types & submitted_types:
            raise RequiredDocumentAlreadySubmitted('이미 제출한 문서입니다.') from exc
        raise
