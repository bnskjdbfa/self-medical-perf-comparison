from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from urllib.parse import urlencode, urlsplit

from django.contrib import messages
from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from config.session_middleware import login_event
from config.db_limits import bounded_atomic, policy_row, retryable_database_error

from .status_payload import build_result_page_payload
from teams.services import calculate_team_day_no, is_team_entry_open
from teams.models import Participant, Respondent, Team
from teams.age_policy import participant_has_required_guardian_consent
from .forms import (
    ReflectionSubmissionForm,
    RequiredDocumentAgreementForm,
    TeamCodeForm,
    V2RespondentInfoForm,
)
from .models import (
    ExitSurveySubmission,
    ReflectionSubmission,
    SurveyForm,
    SurveyQuestion,
    SurveySection,
    SurveySubmission,
)
from .services import (
    build_result_from_answers,
    build_survey_form_snapshot,
    active_survey_form_queryset,
    diagnose_identity_mismatch,
    get_active_survey_form,
    get_or_create_respondent,
    invalidate_submission_cache,
    normalize_name,
    normalize_option_entries,
    ordered_questions_for_form,
)
from .required_documents import (
    REQUIRED_DOCUMENT_ORDER,
    REQUIRED_DOCUMENT_UI,
    STATIC_REQUIRED_DOCUMENT_VERSION,
    RequiredDocumentAlreadySubmitted,
    RequiredDocumentEvidenceChanged,
    RequiredDocumentTemplateInvalid,
    GuardianConsentNotVerified,
    ParticipantIdentityMismatch,
    build_canonical_document_evidence,
    create_respondent_with_privacy_consent,
    create_required_document_submissions_from_consents,
    get_enabled_required_document_types,
    get_next_pending_required_document,
    get_required_document_progress,
    get_submitted_required_document_types,
    required_documents_completed,
    respondent_lacks_guardian_consent,
)


SESSION_TEAM_ID = 'survey_team_id'
SESSION_TEAM_CODE = 'survey_team_code'
SESSION_RESPONDENT_ID = 'survey_respondent_id'
SESSION_DAY_NO = 'survey_day_no'
SESSION_REQUIRED_DOCUMENT_CONSENTS = 'v2_required_document_consents'
SESSION_PRE_IDENTITY_PRIVACY_CONSENT = 'v2_pre_identity_privacy_consent'
SESSION_PUBLIC_LANGUAGE = 'v2_public_language'
SESSION_LOGIN_EXPIRES_AT = 'v2_login_expires_at'
DEFAULT_REQUIRED_DOCUMENT_CONSENT_SESSION_MAX_AGE_SECONDS = 3600
OPS_LOGIN_TEAM_ID = 'ops_login_team_id'
OPS_LOGIN_VERIFIED_TEAM_ID = 'ops_login_verified_team_id'
_CACHE_MISS = object()
logger = logging.getLogger(__name__)


def _public_language(request: HttpRequest) -> str:
    requested = request.GET.get('lang')
    if requested in {'ko', 'en'} and request.session.get(SESSION_PUBLIC_LANGUAGE, 'ko') != requested:
        request.session[SESSION_PUBLIC_LANGUAGE] = requested
    return request.session.get(SESSION_PUBLIC_LANGUAGE, 'ko')

V2_COMPLETION_SCREENS = {
    'documents': {
        'eyebrow': '개인정보동의서',
        'eyebrow_en': 'Consent Forms',
        'message': '동의서 등 작성 및 제출이',
        'message_en': 'Your consent forms have been',
        'accent': '#9f7df3',
        'asset_key': 'document',
        'button_label': '홈으로 돌아가기',
        'button_label_en': 'Return Home',
    },
    'health': {
        'eyebrow': '자가 검진',
        'eyebrow_en': 'Health Check',
        'message': '자가 검진 확인서 작성 및 제출이',
        'message_en': 'Your health check has been',
        'accent': '#84d895',
        'asset_key': 'health',
        'button_label': '결과 보기',
        'button_label_en': 'View Results',
    },
    'reflection': {
        'eyebrow': '소감문작성',
        'eyebrow_en': 'Reflection',
        'message': '소감문 작성 및 제출이',
        'message_en': 'Your reflection has been',
        'accent': '#ff9230',
        'asset_key': 'reflection',
        'button_label': '홈으로 돌아가기',
        'button_label_en': 'Return Home',
    },
    'survey': {
        'eyebrow': '설문지',
        'eyebrow_en': 'Survey',
        'message': '탐방 평가 설문지 작성 및 제출이',
        'message_en': 'Your survey has been',
        'accent': '#76a7fd',
        'asset_key': 'survey',
        'button_label': '홈으로 돌아가기',
        'button_label_en': 'Return Home',
    },
}


def _set_team_session(request: HttpRequest, team: Team) -> None:
    request.session[SESSION_TEAM_ID] = team.id
    request.session[SESSION_TEAM_CODE] = team.code
    request.session.pop(SESSION_RESPONDENT_ID, None)
    request.session.pop(SESSION_DAY_NO, None)
    request.session.pop('survey_answers_draft', None)
    request.session.pop(SESSION_REQUIRED_DOCUMENT_CONSENTS, None)
    request.session.pop(SESSION_PRE_IDENTITY_PRIVACY_CONSENT, None)
    request.session.pop(SESSION_LOGIN_EXPIRES_AT, None)
    _cache_active_team(team)


def _clear_team_session(request: HttpRequest) -> None:
    request.session.pop(SESSION_TEAM_ID, None)
    request.session.pop(SESSION_TEAM_CODE, None)
    request.session.pop(SESSION_RESPONDENT_ID, None)
    request.session.pop(SESSION_DAY_NO, None)
    request.session.pop('survey_answers_draft', None)
    request.session.pop(SESSION_REQUIRED_DOCUMENT_CONSENTS, None)
    request.session.pop(SESSION_PRE_IDENTITY_PRIVACY_CONSENT, None)
    request.session.pop(SESSION_LOGIN_EXPIRES_AT, None)


def _invalid_login_context(request, reason):
    login_event(request, reason)
    _clear_team_session(request)
    return None


def _team_login_expiry(team: Team) -> datetime:
    end_date = team.start_date + timedelta(days=team.max_day_no - 1)
    return timezone.make_aware(
        datetime.combine(end_date, time(23, 59, 59)),
        timezone.get_current_timezone(),
    )


def _configure_persistent_login(request: HttpRequest, team: Team) -> None:
    expires_at = _team_login_expiry(team)
    request.session[SESSION_LOGIN_EXPIRES_AT] = expires_at.isoformat()
    request.session.set_expiry(expires_at)


def _get_persisted_respondent_context(
    request: HttpRequest,
) -> tuple[Team, Respondent] | None:
    expires_raw = request.session.get(SESSION_LOGIN_EXPIRES_AT)
    if not expires_raw:
        return None
    try:
        expires_at = datetime.fromisoformat(expires_raw)
    except (TypeError, ValueError):
        return _invalid_login_context(request, 'invalid_expiry')
    if timezone.is_naive(expires_at):
        return _invalid_login_context(request, 'naive_expiry')
    if expires_at <= timezone.now():
        return _invalid_login_context(request, 'login_expired')

    team_id = request.session.get(SESSION_TEAM_ID)
    team_code = request.session.get(SESSION_TEAM_CODE)
    respondent_id = request.session.get(SESSION_RESPONDENT_ID)
    if not team_id or not team_code or not respondent_id:
        return _invalid_login_context(request, 'incomplete_login_context')

    respondent = (
        Respondent.objects.select_related('team').annotate(
            active_roster_exists=Exists(Participant.objects.filter(
                team_id=OuterRef('team_id'), is_active=True,
                normalized_name=OuterRef('normalized_name'),
                birth_date=OuterRef('birth_date'), gender=OuterRef('gender'),
            ))
        )
        .filter(
            pk=respondent_id,
            team_id=team_id,
            team__code=team_code,
            team__is_active=True,
        )
        .first()
    )
    if respondent is None:
        return _invalid_login_context(request, 'respondent_or_team_inactive')
    team = respondent.team
    if not is_team_entry_open(team):
        return _invalid_login_context(request, 'team_entry_closed')
    if expires_at != _team_login_expiry(team):
        return _invalid_login_context(request, 'team_schedule_changed')
    if not respondent.active_roster_exists:
        return _invalid_login_context(request, 'participant_inactive_or_missing')

    current_day_no = calculate_team_day_no(team)
    if request.session.get(SESSION_DAY_NO) != current_day_no:
        request.session[SESSION_DAY_NO] = current_day_no
    _cache_active_team(team)
    _cache_active_respondent_context(team, respondent)
    return team, respondent


def _survey_context_cache_ttl() -> int:
    return getattr(settings, 'SURVEY_CONTEXT_CACHE_TTL', 60)


def _team_context_cache_key(*, team_id=None, team_code: str | None = None) -> str:
    return f'survey:team_context:{team_id or ""}:{team_code or ""}'


def _respondent_context_cache_key(*, respondent_id, team_id=None, team_code: str | None = None) -> str:
    return f'survey:respondent_context:{respondent_id}:{team_id or ""}:{team_code or ""}'


def _cache_active_team(team: Team) -> None:
    cache_ttl = _survey_context_cache_ttl()
    cache.set(_team_context_cache_key(team_id=team.id, team_code=team.code), team, cache_ttl)
    cache.set(_team_context_cache_key(team_id=team.id), team, cache_ttl)
    cache.set(_team_context_cache_key(team_code=team.code), team, cache_ttl)


def _cache_active_respondent_context(team: Team, respondent: Respondent) -> None:
    cache_ttl = _survey_context_cache_ttl()
    context = (team, respondent)
    cache.set(
        _respondent_context_cache_key(respondent_id=respondent.id, team_id=team.id, team_code=team.code),
        context,
        cache_ttl,
    )
    cache.set(_respondent_context_cache_key(respondent_id=respondent.id, team_id=team.id), context, cache_ttl)
    cache.set(_respondent_context_cache_key(respondent_id=respondent.id, team_code=team.code), context, cache_ttl)
    cache.set(_respondent_context_cache_key(respondent_id=respondent.id), context, cache_ttl)


def _get_active_team_from_session(request: HttpRequest) -> Team | None:
    team_id = request.session.get(SESSION_TEAM_ID)
    team_code = request.session.get(SESSION_TEAM_CODE)

    team = None
    cache_keys = []
    if team_id and team_code:
        cache_keys.append(_team_context_cache_key(team_id=team_id, team_code=team_code))
    if team_id:
        cache_keys.append(_team_context_cache_key(team_id=team_id))
    if team_code:
        cache_keys.append(_team_context_cache_key(team_code=team_code))

    for cache_key in cache_keys:
        cached_team = cache.get(cache_key, _CACHE_MISS)
        if cached_team is not _CACHE_MISS:
            team = cached_team
            break

    if team_id:
        team = team or Team.objects.filter(id=team_id, is_active=True).first()
    if not team and team_code:
        team = Team.objects.filter(code=team_code, is_active=True).first()

    if team and not is_team_entry_open(team):
        _clear_team_session(request)
        return None

    if team:
        if request.session.get(SESSION_TEAM_ID) != team.id:
            request.session[SESSION_TEAM_ID] = team.id
        if request.session.get(SESSION_TEAM_CODE) != team.code:
            request.session[SESSION_TEAM_CODE] = team.code
        _cache_active_team(team)
    return team


def _get_active_respondent_context_from_session(request: HttpRequest) -> tuple[Team, Respondent] | None:
    if request.session.get(SESSION_LOGIN_EXPIRES_AT):
        return _get_persisted_respondent_context(request)
    respondent_id = request.session.get(SESSION_RESPONDENT_ID)
    if not respondent_id:
        login_event(request, 'respondent_session_missing')
        return None

    team_id = request.session.get(SESSION_TEAM_ID)
    team_code = request.session.get(SESSION_TEAM_CODE)
    cache_keys = []
    if team_id and team_code:
        cache_keys.append(_respondent_context_cache_key(respondent_id=respondent_id, team_id=team_id, team_code=team_code))
    if team_id:
        cache_keys.append(_respondent_context_cache_key(respondent_id=respondent_id, team_id=team_id))
    if team_code:
        cache_keys.append(_respondent_context_cache_key(respondent_id=respondent_id, team_code=team_code))
    if not team_id and not team_code:
        cache_keys.append(_respondent_context_cache_key(respondent_id=respondent_id))

    for cache_key in cache_keys:
        cached_context = cache.get(cache_key, _CACHE_MISS)
        if cached_context is not _CACHE_MISS:
            team, respondent = cached_context
            if not is_team_entry_open(team):
                _clear_team_session(request)
                return None
            if request.session.get(SESSION_TEAM_ID) != team.id:
                request.session[SESSION_TEAM_ID] = team.id
            if request.session.get(SESSION_TEAM_CODE) != team.code:
                request.session[SESSION_TEAM_CODE] = team.code
            return team, respondent

    respondents = Respondent.objects.select_related('team').filter(id=respondent_id, team__is_active=True)
    if team_id:
        respondents = respondents.filter(team_id=team_id)
    if team_code:
        respondents = respondents.filter(team__code=team_code)

    respondent = respondents.only(
        'id',
        'team_id',
        'name',
        'birth_date',
        'gender',
        'normalized_name',
        'team__id',
        'team__code',
        'team__name',
        'team__is_active',
        'team__start_date',
        'team__max_day_no',
    ).first()
    if not respondent:
        return None

    team = respondent.team
    if not is_team_entry_open(team):
        _clear_team_session(request)
        return None

    if request.session.get(SESSION_TEAM_ID) != team.id:
        request.session[SESSION_TEAM_ID] = team.id
    if request.session.get(SESSION_TEAM_CODE) != team.code:
        request.session[SESSION_TEAM_CODE] = team.code
    _cache_active_team(team)
    _cache_active_respondent_context(team, respondent)
    return team, respondent


class DaySurveyUnavailable(Exception):
    """The health-check page cannot be served; carries where to send the participant instead."""

    def __init__(self, *, reason: str, redirect_url: str, **log_fields):
        super().__init__(reason)
        self.reason = reason
        self.redirect_url = redirect_url
        self.log_fields = log_fields


# (Korean, English) per reason. Reasons without an entry redirect silently.
DAY_SURVEY_UNAVAILABLE_MESSAGES = {
    'no_session': (
        '로그인이 만료되었거나 탐방 기간이 아닙니다. 팀 코드를 다시 입력해 주세요.',
        'Your login has expired or the tour period is not active. Please enter the team code again.',
    ),
    'day_not_in_schedule': (
        '{day_no}일차는 이 팀의 탐방 일정에 없습니다.',
        "Day {day_no} is not part of this team's schedule.",
    ),
    'not_today': (
        '{day_no}일차 자가검진은 해당 날짜에만 작성할 수 있습니다. 오늘은 {current_day_no}일차입니다.',
        'The Day {day_no} health check can only be completed on that day. Today is Day {current_day_no}.',
    ),
    'no_form': (
        '오늘은 자가검진이 준비되지 않았습니다. 운영자에게 문의해 주세요.',
        "Today's health check is not ready yet. Please contact the organizer.",
    ),
}
DAY_SURVEY_UNAVAILABLE_MESSAGES['no_questions'] = DAY_SURVEY_UNAVAILABLE_MESSAGES['no_form']
GUARDIAN_CONSENT_BLOCK_MESSAGES = (
    '법정대리인 동의 확인이 취소되어 제출할 수 없습니다. 운영자에게 문의해 주세요.',
    'Guardian consent verification was revoked, so this cannot be submitted. Please contact the organizer.',
)


def _guardian_consent_block_message(language: str) -> str:
    return GUARDIAN_CONSENT_BLOCK_MESSAGES[1 if language == 'en' else 0]


HEALTH_CARD_UNAVAILABLE_MESSAGES = (
    '오늘은 자가검진이 준비되지 않았습니다.',
    "Today's health check is not ready yet.",
)


def _day_survey_unavailable_message(reason: str, *, language: str, **fields) -> str:
    korean, english = DAY_SURVEY_UNAVAILABLE_MESSAGES[reason]
    return (english if language == 'en' else korean).format(**fields)


@dataclass(frozen=True)
class HealthCheckState:
    current_day_no: int
    form: SurveyForm | None
    reason: str | None
    completed: bool

    @property
    def available(self) -> bool:
        return self.reason is None


def _health_check_state(*, team: Team, respondent: Respondent) -> HealthCheckState:
    """Whether today's health check can be started: a daily form with questions must exist."""
    current_day_no = calculate_team_day_no(team)
    if not (1 <= current_day_no <= team.max_day_no):
        return HealthCheckState(current_day_no, None, 'day_not_in_schedule', False)
    completed = SurveySubmission.objects.filter(
        team=team,
        respondent=respondent,
        day_no=current_day_no,
    ).exists()
    form = get_active_survey_form(team=team, day_no=current_day_no)
    if form is None:
        return HealthCheckState(current_day_no, None, 'no_form', completed)
    if not ordered_questions_for_form(form):
        return HealthCheckState(current_day_no, form, 'no_questions', completed)
    return HealthCheckState(current_day_no, form, None, completed)


def _day_survey_unavailable_response(
    request: HttpRequest,
    exc: DaySurveyUnavailable,
    *,
    day_no: int,
    is_async_request: bool,
    page_language: str,
) -> HttpResponse:
    logger.warning(
        'day survey unavailable: reason=%s path=%s %s',
        exc.reason,
        request.path,
        ' '.join(f'{key}={value}' for key, value in exc.log_fields.items()
                 if key != 'respondent_id'),
    )
    if is_async_request:
        return JsonResponse({'redirect_url': exc.redirect_url})
    if exc.reason in DAY_SURVEY_UNAVAILABLE_MESSAGES:
        messages.warning(
            request,
            _day_survey_unavailable_message(
                exc.reason,
                language=page_language,
                day_no=day_no,
                current_day_no=exc.log_fields.get('current_day_no'),
            ),
        )
    return redirect(exc.redirect_url)


def _get_context_entities(request: HttpRequest, day_no: int):
    context_entities = _get_active_respondent_context_from_session(request)
    if not context_entities:
        raise DaySurveyUnavailable(
            reason='no_session',
            redirect_url=reverse('surveys:preview-v2-code'),
            requested_day_no=day_no,
        )
    team, respondent = context_entities

    current_day_no = calculate_team_day_no(team)
    home_url = reverse('surveys:preview-v2-home')
    log_fields = {
        'team_id': team.id,
        'respondent_id': respondent.id,
        'requested_day_no': day_no,
        'current_day_no': current_day_no,
    }
    if day_no < 1 or day_no > team.max_day_no:
        raise DaySurveyUnavailable(reason='day_not_in_schedule', redirect_url=home_url, **log_fields)
    if day_no != current_day_no:
        if SurveySubmission.objects.filter(team=team, respondent=respondent, day_no=day_no).exists():
            raise DaySurveyUnavailable(
                reason='already_submitted_other_day',
                redirect_url=(
                    reverse('surveys:result', kwargs={'day_no': day_no})
                    + '?'
                    + urlencode({'source': 'user', 'return_to': home_url})
                ),
                **log_fields,
            )
        raise DaySurveyUnavailable(reason='not_today', redirect_url=home_url, **log_fields)

    form = get_active_survey_form(team=team, day_no=day_no)
    if not form:
        raise DaySurveyUnavailable(reason='no_form', redirect_url=home_url, **log_fields)
    # Question presence is checked in day_survey_view after the required-document gate.
    return team, respondent, form


def _get_result_entities(request: HttpRequest, day_no: int):
    if request.GET.get('source') == 'manager' and request.GET.get('submission_id'):
        return _get_manager_result_entities(request, day_no)

    context_entities = _get_active_respondent_context_from_session(request)
    if not context_entities:
        raise Http404
    team, respondent = context_entities

    if day_no < 1 or day_no > team.max_day_no:
        raise Http404
    return team, respondent


def _get_manager_result_entities(request: HttpRequest, day_no: int):
    if not request.user.is_authenticated or not (request.user.is_staff or request.user.is_superuser):
        raise Http404

    scoped_team_id = request.session.get(OPS_LOGIN_TEAM_ID)
    verified_team_id = request.session.get(OPS_LOGIN_VERIFIED_TEAM_ID)
    if not scoped_team_id or verified_team_id != scoped_team_id:
        raise Http404

    try:
        submission_id = int(request.GET.get('submission_id', ''))
    except (TypeError, ValueError):
        raise Http404

    submission = (
        SurveySubmission.objects
        .select_related('team', 'respondent')
        .filter(id=submission_id, team_id=scoped_team_id, team__is_active=True)
        .only(
            'id',
            'team_id',
            'respondent_id',
            'day_no',
            'team__id',
            'team__code',
            'team__name',
            'team__is_active',
            'team__start_date',
            'team__max_day_no',
            'respondent__id',
            'respondent__team_id',
            'respondent__name',
            'respondent__birth_date',
            'respondent__gender',
            'respondent__normalized_name',
        )
        .first()
    )
    if not submission:
        raise Http404

    if not request.user.is_superuser and not request.user.team_permissions.filter(team_id=submission.team_id).exists():
        raise Http404
    if day_no < 1 or day_no > submission.team.max_day_no:
        raise Http404

    return submission.team, submission.respondent


def day_survey_view(request: HttpRequest, day_no: int) -> HttpResponse:
    page_language = _public_language(request)
    is_async_request = request.headers.get('x-requested-with') == 'XMLHttpRequest'

    def unavailable(exc: DaySurveyUnavailable) -> HttpResponse:
        return _day_survey_unavailable_response(
            request,
            exc,
            day_no=day_no,
            is_async_request=is_async_request,
            page_language=page_language,
        )

    try:
        team, respondent, form = _get_context_entities(request, day_no)
    except DaySurveyUnavailable as exc:
        return unavailable(exc)
    if get_next_pending_required_document(team=team, respondent=respondent):
        return redirect('surveys:preview-v2-documents-next')
    v2_health_completion_url = reverse(
        'surveys:preview-v2-completion',
        kwargs={'completion_type': 'health'},
    )

    existing = SurveySubmission.objects.only('id').filter(team=team, respondent=respondent, day_no=day_no).first()
    if existing:
        redirect_url = v2_health_completion_url
        if is_async_request:
            return JsonResponse({'redirect_url': redirect_url})
        return redirect(redirect_url)

    home_url = reverse('surveys:preview-v2-home')
    if request.method == 'POST':
        fresh_form = active_survey_form_queryset(team=team, day_no=day_no).first()
        if not fresh_form:
            return unavailable(
                DaySurveyUnavailable(
                    reason='no_form',
                    redirect_url=home_url,
                    team_id=team.id,
                    respondent_id=respondent.id,
                    requested_day_no=day_no,
                    current_day_no=day_no,
                )
            )
        form = fresh_form

    questions = ordered_questions_for_form(form)
    if not questions:
        return unavailable(
            DaySurveyUnavailable(
                reason='no_questions',
                redirect_url=home_url,
                team_id=team.id,
                respondent_id=respondent.id,
                requested_day_no=day_no,
                current_day_no=day_no,
                form_id=form.id,
            )
        )

    total_questions = len(questions)
    question_index = int(request.GET.get('q', '1'))
    question_index = min(max(question_index, 1), total_questions)
    current_question = questions[question_index - 1]
    current_section_type = current_question.section.section_type
    section_questions = [question for question in questions if question.section.section_type == current_section_type]
    section_question_count = len(section_questions)
    section_progress_current = next(
        idx
        for idx, question in enumerate(section_questions, start=1)
        if question.id == current_question.id
    )
    section_meta = {
        SurveySection.SectionType.PHYSICAL: ('body', 'Body', '신체 영역'),
        SurveySection.SectionType.MENTAL: ('emotion', 'Emotion', '정신 영역'),
        SurveySection.SectionType.SURVEY: ('survey', 'Survey', '설문 영역'),
    }
    section_key, section_title, section_label_ko = section_meta[
        current_section_type
    ]
    if page_language == 'en':
        section_title = current_question.section.title_en or section_title
        section_label_ko = section_title
    draft_answers = request.session.get('survey_answers_draft', {})

    current_answer = draft_answers.get(current_question.key)
    current_options = normalize_option_entries(current_question.options_json)
    question_positions = {question.id: idx for idx, question in enumerate(questions, start=1)}
    survey_questions_payload = []
    for question in questions:
        section_type = question.section.section_type
        section_key, payload_section_title, payload_section_label = section_meta[
            section_type
        ]
        section_question_ids = [
            section_question.id
            for section_question in questions
            if section_question.section.section_type == section_type
        ]
        section_current = section_question_ids.index(question.id) + 1
        survey_questions_payload.append(
            {
                'id': question.id,
                'key': question.key,
                'label': question.label_en or question.label if page_language == 'en' else question.label,
                'question_type': question.question_type,
                'is_required': question.is_required,
                'options': [
                    {
                        **option,
                        'display_label': (
                            option['label_en'] or option['label']
                            if page_language == 'en'
                            else option['label']
                        ),
                    }
                    for option in normalize_option_entries(question.options_json)
                ],
                'section_key': section_key,
                'section_title': (
                    question.section.title_en or payload_section_title
                    if page_language == 'en'
                    else payload_section_title
                ),
                'section_label_ko': (
                    question.section.title_en or payload_section_title
                    if page_language == 'en'
                    else payload_section_label
                ),
                'section_question_count': len(section_question_ids),
                'section_progress_current': section_current,
                'section_progress_percent': int((section_current / len(section_question_ids)) * 100) if section_question_ids else 0,
                'question_index': question_positions[question.id],
            }
        )

    survey_form_signature = '|'.join(
        f'{question.id}:{question.key}:{question.question_type}:{timezone.localtime(question.updated_at).isoformat()}'
        for question in questions
    )

    def error_response(message: str, *, target_index: int = 1) -> HttpResponse:
        if is_async_request:
            return JsonResponse({'error': message, 'question_index': target_index}, status=400)
        messages.error(request, message)
        return redirect(f"{request.path}?q={target_index}")

    def validate_submitted_form_version() -> HttpResponse | None:
        submitted_form_id = request.POST.get('survey_form_id')
        submitted_form_version = request.POST.get('survey_form_version')
        submitted_form_signature = request.POST.get('survey_form_signature')
        if submitted_form_id is None and submitted_form_version is None and submitted_form_signature is None:
            return None
        if (
            submitted_form_id != str(form.id)
            or submitted_form_version != str(form.version)
            or submitted_form_signature != survey_form_signature
        ):
            return error_response('설문 문항이 변경되었습니다. 새로고침 후 다시 진행해주세요.', target_index=1)
        return None

    def normalize_submitted_answers() -> tuple[dict | None, HttpResponse | None]:
        raw_payload = request.POST.get('answers_payload')
        payload_answers = None
        if raw_payload:
            try:
                payload_answers = json.loads(raw_payload)
            except json.JSONDecodeError:
                return None, error_response('설문 답변 형식이 올바르지 않습니다. 새로고침 후 다시 진행해주세요.', target_index=question_index)
            if not isinstance(payload_answers, dict):
                return None, error_response('설문 답변 형식이 올바르지 않습니다. 새로고침 후 다시 진행해주세요.', target_index=question_index)

        def normalize_question_value(question, raw_value):
            allowed_options = {option['label'] for option in normalize_option_entries(question.options_json)}
            if question.question_type == SurveyQuestion.QuestionType.MULTI:
                if raw_value is None:
                    selected_values = []
                elif isinstance(raw_value, list):
                    selected_values = [str(value).strip() for value in raw_value if str(value).strip()]
                else:
                    selected_values = [str(raw_value).strip()] if str(raw_value).strip() else []
                return [value for value in selected_values if value in allowed_options]
            if question.question_type == SurveyQuestion.QuestionType.SINGLE:
                selected_value = str(raw_value or '').strip()
                return selected_value if selected_value in allowed_options else ''
            return str(raw_value or '').strip()

        answers = {}
        for question in questions:
            field_name = f'q_{question.id}'
            if payload_answers is not None:
                raw_value = payload_answers.get(str(question.id))
                if raw_value is None:
                    raw_value = payload_answers.get(question.key)
                normalized_value = normalize_question_value(question, raw_value)
                if field_name in request.POST:
                    posted_value = request.POST.getlist(field_name) if question.question_type == SurveyQuestion.QuestionType.MULTI else request.POST.get(field_name, '')
                    if normalize_question_value(question, posted_value) != normalized_value:
                        return None, error_response('설문 답변이 일치하지 않습니다. 새로고침 후 다시 제출해주세요.', target_index=question_index)
            elif field_name in request.POST:
                raw_value = request.POST.getlist(field_name) if question.question_type == SurveyQuestion.QuestionType.MULTI else request.POST.get(field_name, '')
                normalized_value = normalize_question_value(question, raw_value)
            else:
                raw_value = draft_answers.get(question.key, [] if question.question_type == SurveyQuestion.QuestionType.MULTI else '')
                normalized_value = normalize_question_value(question, raw_value)
            answers[question.key] = normalized_value
        return answers, None

    def missing_required_question(answers_json: dict):
        for question in questions:
            if not question.is_required:
                continue
            if question.question_type == SurveyQuestion.QuestionType.MULTI:
                if answers_json.get(question.key, []) == []:
                    return question
            elif str(answers_json.get(question.key, '')).strip() == '':
                return question
        return None

    def has_duplicate_question_keys() -> bool:
        seen_keys = set()
        for question in questions:
            if question.key in seen_keys:
                return True
            seen_keys.add(question.key)
        return False

    if request.method == 'POST':
        if 'prev' in request.POST and question_index > 1:
            return redirect(f"{request.path}?q={question_index - 1}")

        if current_question.question_type == SurveyQuestion.QuestionType.MULTI:
            current_value = request.POST.getlist(f'q_{current_question.id}')
            is_empty = len(current_value) == 0
        else:
            current_value = request.POST.get(f'q_{current_question.id}', '').strip()
            is_empty = current_value == ''

        if current_question.is_required and is_empty:
            messages.error(request, f'필수 문항: {current_question.label}')
        else:
            draft_answers[current_question.key] = current_value
            request.session['survey_answers_draft'] = draft_answers
            if 'next' in request.POST and question_index < total_questions:
                return redirect(f"{request.path}?q={question_index + 1}")

        if 'submit' in request.POST:
            if has_duplicate_question_keys():
                return error_response('설문 문항 설정이 올바르지 않습니다. 관리자에게 문의해주세요.', target_index=1)

            if respondent_lacks_guardian_consent(team=team, respondent=respondent):
                logger.warning(
                    'day survey submit blocked: guardian consent not verified team_id=%s',
                    team.id,
                )
                return error_response(_guardian_consent_block_message(page_language), target_index=1)

            version_error = validate_submitted_form_version()
            if version_error:
                return version_error

            answers_json, parse_error = normalize_submitted_answers()
            if parse_error:
                return parse_error

            missing_question = missing_required_question(answers_json)
            if missing_question:
                first_missing_index = next(
                    idx + 1
                    for idx, question in enumerate(questions)
                    if question.id == missing_question.id
                )
                return error_response(f'필수 문항: {missing_question.label}', target_index=first_missing_index)

            snapshot = build_survey_form_snapshot(survey_form=form)
            try:
                with bounded_atomic():
                    submission = SurveySubmission.objects.create(
                        team=team,
                        respondent=respondent,
                        day_no=day_no,
                        answers_json=answers_json,
                        result_json=build_result_from_answers(survey_form=form, answers_json=answers_json),
                        form_version=form.version,
                        form_snapshot_json=snapshot,
                        submission_language=page_language,
                        is_locked=True,
                        submitted_at=timezone.now(),
                    )
            except IntegrityError:
                redirect_url = v2_health_completion_url
                if is_async_request:
                    return JsonResponse({'redirect_url': redirect_url})
                return redirect(redirect_url)
            invalidate_submission_cache(team=team, respondent=respondent, day_no=day_no)
            request.session.pop('survey_answers_draft', None)
            redirect_url = v2_health_completion_url
            if is_async_request:
                return JsonResponse({'redirect_url': redirect_url})
            return redirect(redirect_url)

    return render(
        request,
        'surveys/day_survey.html',
        {
            'team': team,
            'respondent': respondent,
            'day_no': day_no,
            'form': form,
            'question': current_question,
            'question_index': question_index,
            'total_questions': total_questions,
            'progress_percent': int((question_index / total_questions) * 100),
            'section_key': section_key,
            'section_title': section_title,
            'section_label_ko': section_label_ko,
            'section_question_count': section_question_count,
            'section_progress_current': section_progress_current,
            'section_progress_percent': int((section_progress_current / section_question_count) * 100) if section_question_count else 0,
            'is_first_in_section': section_progress_current == 1,
            'is_last_in_section': section_progress_current == section_question_count,
            'current_answer': current_answer,
            'current_options': current_options,
            'survey_questions_payload': survey_questions_payload,
            'initial_answers': draft_answers,
            'initial_question_index': question_index,
            'survey_form_signature': survey_form_signature,
            'page_language': page_language,
        },
    )


def exit_survey_view(request: HttpRequest) -> HttpResponse:
    page_language = _public_language(request)
    context_entities = _get_active_respondent_context_from_session(request)
    if not context_entities:
        return redirect('surveys:preview-v2-code')
    team, respondent = context_entities
    if get_next_pending_required_document(team=team, respondent=respondent):
        return redirect('surveys:preview-v2-documents-next')

    form = (
        SurveyForm.objects.filter(
            team=team,
            form_type=SurveyForm.FormType.EXIT_SURVEY,
            is_anonymous=False,
            is_active=True,
        )
        .prefetch_related('sections__questions')
        .order_by('-version', '-id')
        .first()
    )
    if not form:
        return redirect('surveys:preview-v2-home')

    completion_url = reverse(
        'surveys:preview-v2-completion',
        kwargs={'completion_type': 'survey'},
    )
    if ExitSurveySubmission.objects.filter(
        team=team,
        respondent=respondent,
        survey_form=form,
        is_locked=True,
    ).exists():
        return redirect(completion_url)

    questions = ordered_questions_for_form(form)
    if not questions:
        raise Http404
    try:
        question_index = int(request.GET.get('q', '1'))
    except ValueError:
        question_index = 1
    question_index = min(max(question_index, 1), len(questions))
    current_question = questions[question_index - 1]
    current_section = current_question.section
    section_questions = [
        question for question in questions if question.section_id == current_section.id
    ]
    section_progress_current = next(
        index
        for index, question in enumerate(section_questions, start=1)
        if question.id == current_question.id
    )
    section_meta = {
        SurveySection.SectionType.PHYSICAL: ('body', 'Body', '신체 영역'),
        SurveySection.SectionType.MENTAL: ('emotion', 'Emotion', '정신 영역'),
        SurveySection.SectionType.SURVEY: ('survey', 'Survey', '설문 영역'),
    }
    section_key, section_title, section_label_ko = section_meta[
        current_section.section_type
    ]
    question_positions = {
        question.id: index for index, question in enumerate(questions, start=1)
    }
    survey_questions_payload = []
    for question in questions:
        questions_in_section = [
            item for item in questions if item.section_id == question.section_id
        ]
        section_index = next(
            index
            for index, item in enumerate(questions_in_section, start=1)
            if item.id == question.id
        )
        (
            question_section_key,
            question_section_title,
            question_section_label,
        ) = section_meta[question.section.section_type]
        survey_questions_payload.append(
            {
                'id': question.id,
                'key': question.key,
                'label': question.label_en or question.label if page_language == 'en' else question.label,
                'question_type': question.question_type,
                'is_required': question.is_required,
                'options': [
                    {
                        **option,
                        'display_label': (
                            option['label_en'] or option['label']
                            if page_language == 'en'
                            else option['label']
                        ),
                    }
                    for option in normalize_option_entries(question.options_json)
                ],
                'section_key': question_section_key,
                'section_title': (
                    question.section.title_en or question_section_title
                    if page_language == 'en'
                    else question_section_title
                ),
                'section_label_ko': (
                    question.section.title_en or question_section_title
                    if page_language == 'en'
                    else question_section_label
                ),
                'section_question_count': len(questions_in_section),
                'section_progress_current': section_index,
                'section_progress_percent': int(
                    (section_index / len(questions_in_section)) * 100
                ),
                'question_index': question_positions[question.id],
            }
        )

    survey_form_signature = '|'.join(
        f'{question.id}:{question.key}:{question.question_type}:'
        f'{timezone.localtime(question.updated_at).isoformat()}'
        for question in questions
    )
    is_async_request = request.headers.get('x-requested-with') == 'XMLHttpRequest'

    def error_response(message, target_index=1):
        if is_async_request:
            return JsonResponse(
                {'error': message, 'question_index': target_index},
                status=400,
            )
        messages.error(request, message)
        return redirect(f'{request.path}?q={target_index}')

    if request.method == 'POST':
        if (
            request.POST.get('survey_form_id') != str(form.id)
            or request.POST.get('survey_form_version') != str(form.version)
            or request.POST.get('survey_form_signature') != survey_form_signature
        ):
            return error_response(
                '설문 문항이 변경되었습니다. 새로고침 후 다시 진행해주세요.'
            )
        if len({question.key for question in questions}) != len(questions):
            return error_response(
                '설문 문항 설정이 올바르지 않습니다. 관리자에게 문의해주세요.'
            )
        try:
            submitted_payload = json.loads(
                request.POST.get('answers_payload') or '{}'
            )
        except json.JSONDecodeError:
            return error_response('설문 답변 형식이 올바르지 않습니다.')
        if not isinstance(submitted_payload, dict):
            return error_response('설문 답변 형식이 올바르지 않습니다.')

        answers_json = {}
        for index, question in enumerate(questions, start=1):
            raw_value = submitted_payload.get(str(question.id))
            allowed_options = {
                option['label']
                for option in normalize_option_entries(question.options_json)
            }
            if question.question_type == SurveyQuestion.QuestionType.MULTI:
                values = raw_value if isinstance(raw_value, list) else []
                value = [
                    str(item).strip()
                    for item in values
                    if str(item).strip() in allowed_options
                ]
                is_missing = not value
            elif question.question_type == SurveyQuestion.QuestionType.SINGLE:
                candidate = str(raw_value or '').strip()
                value = candidate if candidate in allowed_options else ''
                is_missing = not value
            else:
                value = str(raw_value or '').strip()
                is_missing = not value
            if question.is_required and is_missing:
                return error_response(
                    f'필수 문항: {question.label}',
                    target_index=index,
                )
            answers_json[question.key] = value

        if respondent_lacks_guardian_consent(team=team, respondent=respondent):
            logger.warning(
                'exit survey submit blocked: guardian consent not verified team_id=%s',
                team.id,
            )
            return error_response(_guardian_consent_block_message(page_language))

        try:
            with transaction.atomic():
                current_form = SurveyForm.objects.select_for_update().get(
                    pk=form.pk,
                    team=team,
                    form_type=SurveyForm.FormType.EXIT_SURVEY,
                    is_anonymous=False,
                    is_active=True,
                )
                if current_form.version != form.version:
                    return error_response(
                        '설문 문항이 변경되었습니다. 새로고침 후 다시 진행해주세요.'
                    )
                snapshot = build_survey_form_snapshot(
                    survey_form=current_form,
                )
                ExitSurveySubmission.objects.create(
                    team=team,
                    respondent=respondent,
                    survey_form=current_form,
                    form_version=current_form.version,
                    form_snapshot_json=snapshot,
                    answers_json=answers_json,
                    is_locked=True,
                    submitted_at=timezone.now(),
                )
        except IntegrityError:
            pass
        if is_async_request:
            return JsonResponse({'redirect_url': completion_url})
        return redirect(completion_url)

    current_options = normalize_option_entries(current_question.options_json)
    return render(
        request,
        'surveys/day_survey.html',
        {
            'team': team,
            'respondent': respondent,
            'day_no': form.day_no,
            'form': form,
            'question': current_question,
            'question_index': question_index,
            'total_questions': len(questions),
            'progress_percent': int((question_index / len(questions)) * 100),
            'section_key': section_key,
            'section_title': section_title,
            'section_label_ko': section_label_ko,
            'section_question_count': len(section_questions),
            'section_progress_current': section_progress_current,
            'section_progress_percent': int(
                (section_progress_current / len(section_questions)) * 100
            ),
            'is_first_in_section': section_progress_current == 1,
            'is_last_in_section': section_progress_current == len(section_questions),
            'current_answer': '',
            'current_options': current_options,
            'survey_questions_payload': survey_questions_payload,
            'initial_answers': {},
            'initial_question_index': question_index,
            'survey_form_signature': survey_form_signature,
            'page_language': page_language,
        },
    )


def result_view(request: HttpRequest, day_no: int) -> HttpResponse:
    team, respondent = _get_result_entities(request, day_no)
    current_day_no = calculate_team_day_no(team)
    source, return_to, query_string = _result_navigation_context(request, day_no=day_no)
    page_language = _result_page_language(
        request,
        source=source,
        team=team,
        respondent=respondent,
        day_no=day_no,
    )
    payload = build_result_page_payload(
        team=team,
        respondent=respondent,
        current_day_no=current_day_no,
        selected_day_no=day_no,
        query_string=query_string,
        back_url=return_to,
        is_detail_view=False,
        page_language=page_language,
    )
    return render(
        request,
        'surveys/result.html',
        {
            'team': team,
            'respondent': respondent,
            'day_no': day_no,
            'source': source,
            'return_to': return_to,
            'page_language': page_language,
            **payload,
        },
    )


def result_detail_view(request: HttpRequest, day_no: int) -> HttpResponse:
    team, respondent = _get_result_entities(request, day_no)
    current_day_no = calculate_team_day_no(team)
    source, return_to, query_string = _result_navigation_context(request, day_no=day_no)
    page_language = _result_page_language(
        request,
        source=source,
        team=team,
        respondent=respondent,
        day_no=day_no,
    )
    payload = build_result_page_payload(
        team=team,
        respondent=respondent,
        current_day_no=current_day_no,
        selected_day_no=day_no,
        query_string=query_string,
        back_url=return_to,
        is_detail_view=True,
        page_language=page_language,
    )
    return render(
        request,
        'surveys/result_detail.html',
        {
            'team': team,
            'respondent': respondent,
            'day_no': day_no,
            'source': source,
            'return_to': return_to,
            'page_language': page_language,
            'result_main_url': reverse('surveys:result', kwargs={'day_no': day_no}) + (f'?{query_string}' if query_string else ''),
            **payload,
        },
    )


def _safe_internal_return_to(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return None
    if not parsed.path.startswith('/') or value.startswith('//'):
        return None
    return parsed.path + (f'?{parsed.query}' if parsed.query else '')


def _result_page_language(
    request: HttpRequest,
    *,
    source: str,
    team: Team,
    respondent: Respondent,
    day_no: int,
) -> str:
    if source == 'manager':
        return SurveySubmission.SubmissionLanguage.KOREAN
    return _public_language(request)


def _result_navigation_context(request: HttpRequest, *, day_no: int) -> tuple[str, str, str]:
    source = request.GET.get('source')
    if source not in {'user', 'manager'}:
        source = 'user'

    return_to = _safe_internal_return_to(request.GET.get('return_to'))
    if not return_to:
        if source == 'manager':
            return_to = reverse('admins:prototype-manager') + f'?day_no={day_no}'
        else:
            return_to = reverse('surveys:preview-v2-home')

    query_params = {'source': source, 'return_to': return_to}
    if source == 'manager':
        submission_id = request.GET.get('submission_id')
        if submission_id and submission_id.isdigit():
            query_params['submission_id'] = submission_id

    query_string = urlencode(query_params)
    return source, return_to, query_string




def preview_v2_code_view(request: HttpRequest) -> HttpResponse:
    page_language = _public_language(request)
    if request.session.get(SESSION_LOGIN_EXPIRES_AT):
        persisted_context = _get_active_respondent_context_from_session(request)
        if persisted_context:
            login_event(request, 'authenticated_login_replay')
            team, respondent = persisted_context
            if get_next_pending_required_document(
                team=team,
                respondent=respondent,
            ):
                return redirect('surveys:preview-v2-documents-next')
            return redirect('surveys:preview-v2-home')
    # Establish a shared attempt before the first POST, so repeated clicks use
    # the same DB row and can be serialized across workers.
    reset_required = getattr(getattr(request.session, 'context', None), 'stale', False)
    if request.method == 'GET' and not reset_required and not request.session.get('login_attempt_id'):
        request.session['login_attempt_id'] = secrets.token_urlsafe(24)
    form = TeamCodeForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        team_code = form.cleaned_data['team_code'].strip()
        team = Team.objects.filter(code=team_code, is_active=True).first()
        if team and not is_team_entry_open(team):
            form.add_error('team_code', '현재 운영 기간이 아닌 팀입니다.')
        elif not team:
            manager_team = Team.objects.filter(manager_entry_code=team_code, is_active=True).first()
            if manager_team:
                request.session[OPS_LOGIN_TEAM_ID] = manager_team.id
                request.session.pop(OPS_LOGIN_VERIFIED_TEAM_ID, None)
                return redirect('admins:login')
            form.add_error('team_code', '유효하지 않은 팀 코드 또는 관리자 코드입니다.')
        else:
            if (request.session.get(SESSION_TEAM_ID) == team.id
                    and request.session.get(SESSION_TEAM_CODE) == team.code):
                login_event(request, 'code_replay')
            else:
                _set_team_session(request, team)
                login_event(request, 'code_accepted')
            if (
                REQUIRED_DOCUMENT_ORDER[0]
                in get_enabled_required_document_types(team)
            ):
                if _validated_pre_identity_privacy_consent(
                    request, team=team, language=page_language,
                )[0] is not None:
                    return redirect('surveys:preview-v2-identity')
                return redirect('surveys:preview-v2-privacy-consent')
            return redirect('surveys:preview-v2-identity')
    code_url = reverse('surveys:preview-v2-code')
    return render(
        request,
        'surveys/public_v2/code.html',
        {
            'form': form,
            'login_reset_required': reset_required,
            'preview_mode': True,
            'page_language': page_language,
            'language_url_ko': f'{code_url}?lang=ko',
            'language_url_en': f'{code_url}?lang=en',
        },
    )


def _build_pre_identity_privacy_consent_state(
    *,
    team: Team,
    language: str,
) -> dict:
    language = 'en' if language == 'en' else 'ko'
    evidence = build_canonical_document_evidence(
        team=team,
        document_type=REQUIRED_DOCUMENT_ORDER[0],
        language=language,
    )
    agreed_at = _required_document_server_now()
    return {
        'team_id': team.id,
        'language': language,
        'document_type': REQUIRED_DOCUMENT_ORDER[0],
        'document_version': evidence['version'],
        'document_content_hash': evidence['content_hash'],
        'agreed': True,
        'agreed_at': agreed_at.isoformat(),
    }


def _validated_pre_identity_privacy_consent(
    request: HttpRequest,
    *,
    team: Team,
    language: str,
) -> tuple[dict | None, str | None]:
    language = 'en' if language == 'en' else 'ko'
    state = request.session.get(SESSION_PRE_IDENTITY_PRIVACY_CONSENT)
    if not isinstance(state, dict):
        return None, 'missing'
    agreed_at = _parse_server_consent_time(state.get('agreed_at'))
    if (
        state.get('team_id') != team.id
        or state.get('language') != language
        or state.get('document_type') != REQUIRED_DOCUMENT_ORDER[0]
        or state.get('agreed') is not True
        or agreed_at is None
        or agreed_at > _required_document_server_now()
        or (
            _required_document_server_now() - agreed_at
        ).total_seconds() > _required_document_consent_max_age_seconds()
    ):
        return None, 'expired_or_invalid'
    try:
        evidence = build_canonical_document_evidence(
            team=team,
            document_type=REQUIRED_DOCUMENT_ORDER[0],
            language=language,
        )
    except RequiredDocumentTemplateInvalid:
        return None, 'document_changed'
    if (
        state.get('document_version') != evidence['version']
        or state.get('document_content_hash') != evidence['content_hash']
    ):
        return None, 'document_changed'
    return state, None


def preview_v2_privacy_consent_view(request: HttpRequest) -> HttpResponse:
    page_language = _public_language(request)
    team = _get_active_team_from_session(request)
    if not team:
        return redirect('surveys:preview-v2-code')
    if (
        REQUIRED_DOCUMENT_ORDER[0]
        not in get_enabled_required_document_types(team)
    ):
        request.session.pop(SESSION_PRE_IDENTITY_PRIVACY_CONSENT, None)
        return redirect('surveys:preview-v2-identity')

    form = RequiredDocumentAgreementForm(
        request.POST if request.method == 'POST' else None
    )
    if request.method == 'POST' and form.is_valid():
        try:
            state = _build_pre_identity_privacy_consent_state(
                team=team,
                language=page_language,
            )
        except RequiredDocumentTemplateInvalid as exc:
            form.add_error(None, str(exc))
        else:
            request.session[SESSION_PRE_IDENTITY_PRIVACY_CONSENT] = state
            request.session.modified = True
            return redirect('surveys:preview-v2-identity')

    document_type = REQUIRED_DOCUMENT_ORDER[0]
    document_ui = REQUIRED_DOCUMENT_UI[document_type]
    return render(
        request,
        'surveys/public_v2/pre_identity_privacy_consent.html',
        {
            'form': form,
            'team': team,
            'document_type': document_type,
            'document_ui': document_ui,
            'page_language': page_language,
            'preview_mode': True,
        },
    )


def preview_v2_identity_view(request: HttpRequest) -> HttpResponse:
    page_language = _public_language(request)
    if request.session.get(SESSION_LOGIN_EXPIRES_AT):
        if _get_active_respondent_context_from_session(request):
            login_event(request, 'authenticated_identity_replay')
            return redirect('surveys:preview-v2-documents-next')
    team = _get_active_team_from_session(request)
    if not team:
        login_event(request, 'identity_team_session_missing')
        return redirect('surveys:preview-v2-code')
    privacy_enabled = (
        REQUIRED_DOCUMENT_ORDER[0]
        in get_enabled_required_document_types(team)
    )
    privacy_state = None
    if privacy_enabled:
        privacy_state, privacy_state_error = _validated_pre_identity_privacy_consent(
            request,
            team=team,
            language=page_language,
        )
        if privacy_state is None:
            if privacy_state_error == 'document_changed':
                messages.warning(
                    request,
                    '개인정보 동의서 내용이 변경되어 다시 확인이 필요합니다.',
                )
            elif privacy_state_error == 'expired_or_invalid':
                messages.warning(
                    request,
                    '개인정보 동의 시간이 만료되어 다시 확인이 필요합니다.',
                )
            request.session.pop(SESSION_PRE_IDENTITY_PRIVACY_CONSENT, None)
            return redirect('surveys:preview-v2-privacy-consent')

    form = V2RespondentInfoForm(request.POST or None)
    mismatched_fields: set[str] = set()
    if request.method == 'POST' and form.is_valid():
        input_gender = form.cleaned_data.get('gender') or None
        try:
            if privacy_enabled:
                agreed_at = _parse_server_consent_time(
                    privacy_state.get('agreed_at')
                )
                respondent = create_respondent_with_privacy_consent(
                    team=team,
                    name=form.cleaned_data['name'],
                    birth_date=form.cleaned_data['birth_date'],
                    gender=input_gender,
                    agreed_at=agreed_at,
                    expected_fingerprint={
                        'document_type': privacy_state['document_type'],
                        'language': privacy_state['language'],
                        'document_version': privacy_state['document_version'],
                        'document_content_hash': privacy_state['document_content_hash'],
                    },
                )
            else:
                with bounded_atomic():
                    locked_team = policy_row(Team, pk=team.pk, is_active=True)
                    if locked_team is None:
                        raise ParticipantIdentityMismatch
                    participant = (
                        Participant.objects.select_for_update(nowait=True)
                        .filter(
                            team=locked_team,
                            is_active=True,
                            normalized_name__iexact=normalize_name(form.cleaned_data['name']),
                            birth_date=form.cleaned_data['birth_date'],
                            gender=input_gender,
                        )
                        .first()
                    )
                    if participant is None:
                        raise ParticipantIdentityMismatch
                    if not participant_has_required_guardian_consent(participant):
                        raise GuardianConsentNotVerified
                    respondent = get_or_create_respondent(
                        locked_team,
                        name=participant.name,
                        birth_date=participant.birth_date,
                        gender=participant.gender,
                        phone=participant.phone,
                    )
        except GuardianConsentNotVerified:
            login_event(request, 'guardian_consent_missing')
            form.add_error(
                None,
                '법정대리인 서면 동의 확인이 완료되지 않았습니다. '
                '운영 담당자에게 문의해 주세요.',
            )
        except ParticipantIdentityMismatch:
            login_event(request, 'identity_mismatch')
            # 문구는 그대로 두고, 어느 칸이 틀렸는지만 테두리로 표시한다.
            mismatched_fields = diagnose_identity_mismatch(
                team=team,
                name=form.cleaned_data['name'],
                birth_date=form.cleaned_data['birth_date'],
                gender=input_gender,
            )
            form.add_error(
                None,
                '입력한 정보가 참가자 명단과 일치하지 않습니다. '
                '정보를 다시 확인하거나 운영 담당자에게 문의해 주세요.',
            )
        except RequiredDocumentEvidenceChanged:
            request.session.pop(SESSION_PRE_IDENTITY_PRIVACY_CONSENT, None)
            messages.warning(
                request,
                '개인정보 동의서 내용이 변경되어 다시 확인이 필요합니다.',
            )
            return redirect('surveys:preview-v2-privacy-consent')
        except (RequiredDocumentTemplateInvalid, RequiredDocumentAlreadySubmitted):
            form.add_error(
                None,
                '본인확인과 개인정보 동의 처리 중 오류가 발생했습니다. '
                '처음부터 다시 진행해 주세요.',
            )
        except DatabaseError:
            login_event(request, 'identity_database_busy')
            return JsonResponse({'error': 'temporarily_unavailable'}, status=503)
        else:
            request.session.cycle_key()
            if request.session.get(SESSION_RESPONDENT_ID) != respondent.id:
                request.session.pop(SESSION_REQUIRED_DOCUMENT_CONSENTS, None)
            request.session[SESSION_RESPONDENT_ID] = respondent.id
            request.session[SESSION_DAY_NO] = calculate_team_day_no(team)
            _configure_persistent_login(request, team)
            request.session.pop('survey_answers_draft', None)
            request.session.pop(SESSION_PRE_IDENTITY_PRIVACY_CONSENT, None)
            _cache_active_respondent_context(team, respondent)
            login_event(request, 'identity_authenticated')
            return redirect('surveys:preview-v2-documents-next')
    return render(
        request,
        'surveys/public_v2/identity.html',
        {
            'form': form,
            'team': team,
            'preview_mode': True,
            'page_language': page_language,
            'mismatched_fields': mismatched_fields,
        },
    )


@require_POST
def preview_v2_reset_login_view(request: HttpRequest) -> HttpResponse:
    page_language = _public_language(request)
    request.session.flush()
    request.session[SESSION_PUBLIC_LANGUAGE] = page_language
    messages.success(
        request,
        (
            'Login information has been cleared.'
            if page_language == 'en'
            else '로그인 정보가 초기화되었습니다.'
        ),
    )
    return redirect('surveys:preview-v2-code')


REQUIRED_DOCUMENT_STEP_LABELS = {
    'privacy_consent': ('개인정보', '제공 동의서'),
    'third_party_consent': ('제 3자', '정보제공 동의서'),
    'safety_pledge': ('여행 안전수칙', '서약서'),
}
REQUIRED_DOCUMENT_STEP_LABELS_EN = {
    'privacy_consent': ('Personal Information', 'Consent'),
    'third_party_consent': ('Third-party', 'Provision Consent'),
    'safety_pledge': ('Travel Safety', 'Pledge'),
}


def _new_required_document_consent_state(
    *,
    team: Team,
    respondent: Respondent,
    language: str = 'ko',
    enabled_types: tuple[str, ...] | None = None,
) -> dict:
    now_iso = _required_document_server_now().isoformat()
    return {
        'team_id': team.id,
        'respondent_id': respondent.id,
        'language': 'en' if language == 'en' else 'ko',
        'enabled_document_types': list(get_enabled_required_document_types(team) if enabled_types is None else enabled_types),
        'started_at': now_iso,
        'last_updated_at': now_iso,
        'consents': {},
    }


def _parse_server_consent_time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if timezone.is_aware(parsed) else None


def _required_document_server_now() -> datetime:
    return timezone.now()


def _required_document_consent_max_age_seconds() -> int:
    configured = getattr(
        settings,
        'REQUIRED_DOCUMENT_CONSENT_SESSION_MAX_AGE_SECONDS',
        DEFAULT_REQUIRED_DOCUMENT_CONSENT_SESSION_MAX_AGE_SECONDS,
    )
    try:
        value = int(configured)
    except (TypeError, ValueError):
        return DEFAULT_REQUIRED_DOCUMENT_CONSENT_SESSION_MAX_AGE_SECONDS
    return value if value > 0 else DEFAULT_REQUIRED_DOCUMENT_CONSENT_SESSION_MAX_AGE_SECONDS


def _required_document_consent_state_is_valid(
    state,
    *,
    team: Team,
    respondent: Respondent,
    enabled_types: tuple[str, ...],
    language: str,
    now: datetime,
) -> bool:
    if not isinstance(state, dict):
        return False
    if state.get('team_id') != team.id or state.get('respondent_id') != respondent.id:
        return False
    if state.get('enabled_document_types') != list(enabled_types):
        return False
    if state.get('language', 'ko') != language:
        return False
    started_at = _parse_server_consent_time(state.get('started_at'))
    last_updated_at = _parse_server_consent_time(state.get('last_updated_at'))
    if not started_at or not last_updated_at:
        return False
    max_age_seconds = _required_document_consent_max_age_seconds()
    if (
        started_at > now
        or last_updated_at > now
        or last_updated_at < started_at
        or (now - started_at).total_seconds() > max_age_seconds
        or (now - last_updated_at).total_seconds() > max_age_seconds
    ):
        return False
    consents = state.get('consents')
    if not isinstance(consents, dict):
        return False
    previous_agreed_at = started_at
    for document_type in enabled_types:
        consent = consents.get(document_type)
        if consent is None:
            continue
        agreed_at = _parse_server_consent_time(consent.get('agreed_at')) if isinstance(consent, dict) else None
        if (
            consent.get('agreed') is not True
            or consent.get('document_type') != document_type
            or consent.get('language', 'ko') != language
            or consent.get('document_version') != STATIC_REQUIRED_DOCUMENT_VERSION
            or not isinstance(consent.get('document_content_hash'), str)
            or len(consent.get('document_content_hash')) != 64
            or agreed_at is None
            or agreed_at < started_at
            or agreed_at < previous_agreed_at
            or agreed_at > now
            or agreed_at > last_updated_at
            or (agreed_at - started_at).total_seconds() > max_age_seconds
        ):
            return False
        previous_agreed_at = agreed_at
    return set(consents).issubset(set(enabled_types))


def _get_required_document_consent_state(
    request: HttpRequest,
    *,
    team: Team,
    respondent: Respondent,
    language: str = 'ko',
    progress=None,
) -> tuple[dict, str | None]:
    language = 'en' if language == 'en' else 'ko'
    progress = progress if progress is not None else get_required_document_progress(team=team, respondent=respondent)
    enabled_types = progress.enabled_types
    existing_state = request.session.get(SESSION_REQUIRED_DOCUMENT_CONSENTS)
    now = _required_document_server_now()
    reset_reason = None
    if existing_state is not None:
        if not isinstance(existing_state, dict):
            reset_reason = 'invalid'
        elif (
            existing_state.get('team_id') != team.id
            or existing_state.get('respondent_id') != respondent.id
        ):
            reset_reason = 'identity_changed'
        elif existing_state.get('enabled_document_types') != list(enabled_types):
            reset_reason = 'settings_changed'
        elif existing_state.get('language', 'ko') != language:
            reset_reason = 'language_changed'
        elif not _required_document_consent_state_is_valid(
            existing_state,
            team=team,
            respondent=respondent,
            enabled_types=enabled_types,
            language=language,
            now=now,
        ):
            reset_reason = 'expired_or_invalid'
    if existing_state is None or reset_reason:
        state = _new_required_document_consent_state(
            team=team,
            respondent=respondent,
            language=language,
            enabled_types=enabled_types,
        )
        request.session[SESSION_REQUIRED_DOCUMENT_CONSENTS] = state
        return state, reset_reason

    submitted_types = progress.submitted_types
    stale_types = set(existing_state['consents']) & set(submitted_types)
    if stale_types:
        state = {
            **existing_state,
            'consents': {
                document_type: consent
                for document_type, consent in existing_state['consents'].items()
                if document_type not in stale_types
            },
        }
        request.session[SESSION_REQUIRED_DOCUMENT_CONSENTS] = state
        existing_state = state

    pending_types = [
        document_type
        for document_type in enabled_types
        if document_type not in submitted_types
    ]
    recorded_pending_types = [
        document_type
        for document_type in pending_types
        if document_type in existing_state['consents']
    ]
    if recorded_pending_types != pending_types[:len(recorded_pending_types)]:
        state = _new_required_document_consent_state(
            team=team,
            respondent=respondent,
            language=language,
            enabled_types=enabled_types,
        )
        request.session[SESSION_REQUIRED_DOCUMENT_CONSENTS] = state
        return state, 'expired_or_invalid'

    ordered_types = list(enabled_types)
    for document_type in ordered_types:
        consent = existing_state['consents'].get(document_type)
        if not consent:
            continue
        try:
            evidence = build_canonical_document_evidence(
                team=team,
                document_type=document_type,
                language=language,
            )
        except RequiredDocumentTemplateInvalid:
            evidence = None
        if (
            evidence is None
            or consent.get('document_version') != evidence['version']
            or consent.get('document_content_hash') != evidence['content_hash']
        ):
            changed_index = ordered_types.index(document_type)
            state = {
                **existing_state,
                'last_updated_at': now.isoformat(),
                'consents': {
                    recorded_type: recorded_consent
                    for recorded_type, recorded_consent in existing_state['consents'].items()
                    if ordered_types.index(recorded_type) < changed_index
                },
            }
            request.session[SESSION_REQUIRED_DOCUMENT_CONSENTS] = state
            return state, 'document_changed'
    return existing_state, None


def _pending_required_document_types(*, team: Team, respondent: Respondent) -> list[str]:
    enabled_types = get_enabled_required_document_types(team)
    submitted_types = get_submitted_required_document_types(team=team, respondent=respondent)
    return [
        document_type
        for document_type in enabled_types
        if document_type not in submitted_types
    ]


def _next_unrecorded_document_type(state: dict, pending_types: list[str]) -> str | None:
    return next(
        (
            document_type
            for document_type in pending_types
            if document_type not in state['consents']
        ),
        None,
    )


def _document_error_response(message: str, *, status: int = 400, redirect_url: str = '') -> JsonResponse:
    payload = {'ok': False, 'error': message}
    if redirect_url:
        payload['redirect_url'] = redirect_url
    return JsonResponse(payload, status=status)


def preview_v2_documents_next_view(request: HttpRequest) -> HttpResponse:
    context_entities = _get_active_respondent_context_from_session(request)
    if not context_entities:
        return redirect('surveys:preview-v2-code')
    team, respondent = context_entities
    page_language = _public_language(request)
    progress = get_required_document_progress(team=team, respondent=respondent)
    pending_types = [kind for kind in progress.enabled_types if kind not in progress.submitted_types]
    if not pending_types:
        request.session.pop(SESSION_REQUIRED_DOCUMENT_CONSENTS, None)
        return redirect('surveys:preview-v2-home')
    state, _ = _get_required_document_consent_state(
        request,
        team=team,
        respondent=respondent,
        language=page_language,
        progress=progress,
    )
    next_document_type = _next_unrecorded_document_type(state, pending_types) or pending_types[-1]
    return redirect('surveys:preview-v2-document', document_type=next_document_type)


def preview_v2_document_view(request: HttpRequest, document_type: str) -> HttpResponse:
    context_entities = _get_active_respondent_context_from_session(request)
    if not context_entities:
        return redirect('surveys:preview-v2-code')
    team, respondent = context_entities
    page_language = _public_language(request)
    progress = get_required_document_progress(team=team, respondent=respondent)
    pending_types = [kind for kind in progress.enabled_types if kind not in progress.submitted_types]
    if not pending_types:
        request.session.pop(SESSION_REQUIRED_DOCUMENT_CONSENTS, None)
        return redirect('surveys:preview-v2-home')
    state, state_reset_reason = _get_required_document_consent_state(
        request,
        team=team,
        respondent=respondent,
        language=page_language,
        progress=progress,
    )
    next_document_type = _next_unrecorded_document_type(state, pending_types)
    display_document_type = next_document_type or pending_types[-1]
    if request.method != 'POST' and document_type != display_document_type:
        return redirect('surveys:preview-v2-document', document_type=display_document_type)
    if request.method != 'POST' and state_reset_reason:
        if state_reset_reason == 'document_changed':
            messages.warning(request, '동의서 내용이 변경되어 다시 확인이 필요합니다.')
        elif state_reset_reason == 'expired_or_invalid':
            messages.warning(request, '동의 진행 시간이 만료되어 다시 확인이 필요합니다.')
        else:
            messages.warning(request, '필수 문서 설정 또는 로그인 정보가 변경되어 다시 확인이 필요합니다.')

    form = RequiredDocumentAgreementForm(request.POST or None)
    if request.method == 'POST':
        if state_reset_reason:
            error_message = {
                'document_changed': '동의서 내용이 변경되어 다시 확인이 필요합니다.',
                'expired_or_invalid': '동의 진행 시간이 만료되어 다시 확인이 필요합니다.',
            }.get(
                state_reset_reason,
                '필수 문서 설정 또는 로그인 정보가 변경되었습니다. 처음부터 다시 진행해 주세요.',
            )
            return _document_error_response(
                error_message,
                status=409,
                redirect_url=reverse('surveys:preview-v2-documents-next'),
            )
        action = request.POST.get('action')
        requested_document_type = request.POST.get('document_type')
        if action not in {'record_consent', 'finalize'}:
            return _document_error_response('올바르지 않은 제출 요청입니다.')
        if not form.is_valid():
            return _document_error_response('동의 또는 서약 확인이 필요합니다.')

        current_document_type = _next_unrecorded_document_type(state, pending_types)

        final_retry = (
            action == 'finalize'
            and current_document_type is None
            and requested_document_type == pending_types[-1]
        )
        if not final_retry:
            if requested_document_type != current_document_type:
                return _document_error_response('필수 문서는 정해진 순서대로 동의해야 합니다.', status=409)
            try:
                evidence = build_canonical_document_evidence(
                    team=team,
                    document_type=requested_document_type,
                    language=page_language,
                )
            except RequiredDocumentTemplateInvalid as exc:
                return _document_error_response(str(exc), status=409)
            consent_time = _required_document_server_now()
            state = {
                **state,
                'last_updated_at': consent_time.isoformat(),
                'consents': {
                    **state['consents'],
                    requested_document_type: {
                        'document_type': requested_document_type,
                        'language': evidence['language'],
                        'document_title': evidence['title'],
                        'document_version': evidence['version'],
                        'document_content_hash': evidence['content_hash'],
                        'agreed': True,
                        'agreed_at': consent_time.isoformat(),
                    },
                },
            }
            request.session[SESSION_REQUIRED_DOCUMENT_CONSENTS] = state
            request.session.modified = True
            if action == 'finalize':
                request.session.save()

        next_document_type = _next_unrecorded_document_type(state, pending_types)
        if action == 'record_consent':
            if next_document_type is None:
                return _document_error_response('마지막 필수 문서는 최종 제출이 필요합니다.', status=409)
            return JsonResponse(
                {
                    'ok': True,
                    'recorded_document_type': requested_document_type,
                    'next_document_type': next_document_type,
                }
            )

        if next_document_type is not None:
            return _document_error_response('모든 필수 문서의 동의 기록이 필요합니다.', status=409)

        try:
            canonical_evidence_by_document_type = {
                pending_type: build_canonical_document_evidence(
                    team=team,
                    document_type=pending_type,
                    language=page_language,
                )
                for pending_type in pending_types
            }
        except RequiredDocumentTemplateInvalid as exc:
            return _document_error_response(str(exc), status=409)
        changed_document_type = next(
            (
                pending_type
                for pending_type in pending_types
                if (
                    state['consents'][pending_type].get('document_type') != pending_type
                    or state['consents'][pending_type].get('document_version')
                    != canonical_evidence_by_document_type[pending_type]['version']
                    or state['consents'][pending_type].get('document_content_hash')
                    != canonical_evidence_by_document_type[pending_type]['content_hash']
                )
            ),
            None,
        )
        if changed_document_type:
            changed_index = list(get_enabled_required_document_types(team)).index(changed_document_type)
            enabled_order = list(get_enabled_required_document_types(team))
            state = {
                **state,
                'last_updated_at': _required_document_server_now().isoformat(),
                'consents': {
                    recorded_type: consent
                    for recorded_type, consent in state['consents'].items()
                    if enabled_order.index(recorded_type) < changed_index
                },
            }
            request.session[SESSION_REQUIRED_DOCUMENT_CONSENTS] = state
            request.session.save()
            return _document_error_response(
                '동의서 내용이 변경되어 다시 확인이 필요합니다.',
                status=409,
                redirect_url=reverse(
                    'surveys:preview-v2-document',
                    kwargs={'document_type': changed_document_type},
                ),
            )

        agreed_at_by_document_type = {
            pending_type: _parse_server_consent_time(state['consents'][pending_type]['agreed_at'])
            for pending_type in pending_types
            if pending_type in state['consents']
        }
        if (
            set(agreed_at_by_document_type) != set(pending_types)
            or any(value is None for value in agreed_at_by_document_type.values())
        ):
            return _document_error_response('필수 문서 동의 기록을 확인할 수 없습니다.', status=409)
        try:
            create_required_document_submissions_from_consents(
                team=team,
                respondent=respondent,
                agreed_at_by_document_type=agreed_at_by_document_type,
                expected_fingerprints_by_document_type={
                    pending_type: {
                        'document_type': state['consents'][pending_type]['document_type'],
                        'language': state['consents'][pending_type].get('language', page_language),
                        'document_version': state['consents'][pending_type]['document_version'],
                        'document_content_hash': state['consents'][pending_type]['document_content_hash'],
                    }
                    for pending_type in pending_types
                },
            )
        except RequiredDocumentAlreadySubmitted:
            if get_next_pending_required_document(team=team, respondent=respondent) is None:
                request.session.pop(SESSION_REQUIRED_DOCUMENT_CONSENTS, None)
                return JsonResponse(
                    {
                        'ok': True,
                        'completed': True,
                        'redirect_url': reverse(
                            'surveys:preview-v2-completion',
                            kwargs={'completion_type': 'documents'},
                        ),
                    }
                )
            request.session.pop(SESSION_REQUIRED_DOCUMENT_CONSENTS, None)
            return _document_error_response(
                '이미 제출된 문서가 포함되어 있습니다.',
                status=409,
                redirect_url=reverse('surveys:preview-v2-documents-next'),
            )
        except RequiredDocumentEvidenceChanged as exc:
            enabled_order = list(get_enabled_required_document_types(team))
            changed_index = enabled_order.index(exc.document_type)
            state = {
                **state,
                'last_updated_at': _required_document_server_now().isoformat(),
                'consents': {
                    recorded_type: consent
                    for recorded_type, consent in state['consents'].items()
                    if enabled_order.index(recorded_type) < changed_index
                },
            }
            request.session[SESSION_REQUIRED_DOCUMENT_CONSENTS] = state
            request.session.save()
            return _document_error_response(
                str(exc),
                status=409,
                redirect_url=reverse(
                    'surveys:preview-v2-document',
                    kwargs={'document_type': exc.document_type},
                ),
            )
        except RequiredDocumentTemplateInvalid as exc:
            return _document_error_response(str(exc), status=409)
        except DatabaseError as exc:
            return _document_error_response(
                '제출 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.',
                status=503 if retryable_database_error(exc) else 500,
            )
        request.session.pop(SESSION_REQUIRED_DOCUMENT_CONSENTS, None)
        return JsonResponse(
            {
                'ok': True,
                'completed': True,
                'redirect_url': reverse(
                    'surveys:preview-v2-completion',
                    kwargs={'completion_type': 'documents'},
                ),
            }
        )

    recorded_types = set(state['consents'])
    display_types = [
        pending_type
        for pending_type in pending_types
        if pending_type not in recorded_types
    ]
    if not display_types:
        display_types = [pending_types[-1]]
    documents = []
    for pending_type in display_types:
        ui = REQUIRED_DOCUMENT_UI[pending_type]
        documents.append(
            {
                'document_type': pending_type,
                'ui': ui,
                'steps': [
                    {
                        'document_type': step_type,
                        'label_lines': (
                            REQUIRED_DOCUMENT_STEP_LABELS_EN[step_type]
                            if page_language == 'en'
                            else REQUIRED_DOCUMENT_STEP_LABELS[step_type]
                        ),
                        'is_enabled': step_type in progress.enabled_types,
                        'is_completed': (
                            step_type in progress.submitted_types
                            or step_type in recorded_types
                        ),
                        'is_current': step_type == pending_type,
                    }
                    for step_type in REQUIRED_DOCUMENT_ORDER
                ],
            }
        )
    form.fields['agreed'].label = '선택된 필수 문서 내용을 모두 확인했으며 이에 동의합니다.'
    return render(
        request,
        'surveys/public_v2/document.html',
        {
            'team': team,
            'respondent': respondent,
            'document_type': document_type,
            'form': form,
            'total_steps': progress.total,
            'documents': documents,
            'finalize_retry': not bool(_next_unrecorded_document_type(state, pending_types)),
            'preview_mode': True,
            'page_language': page_language,
        },
    )


def preview_v2_home_view(request: HttpRequest) -> HttpResponse:
    context_entities = _get_active_respondent_context_from_session(request)
    if not context_entities:
        return redirect('surveys:preview-v2-code')
    team, respondent = context_entities
    page_language = _public_language(request)
    required_document_progress = get_required_document_progress(team=team, respondent=respondent)
    if not required_document_progress.is_complete:
        return redirect('surveys:preview-v2-documents-next')
    health_state = _health_check_state(team=team, respondent=respondent)
    current_day_no = health_state.current_day_no
    health_completed = health_state.completed
    home_url = reverse('surveys:preview-v2-home')
    reflection_completed = ReflectionSubmission.objects.filter(
        team=team,
        respondent=respondent,
        is_locked=True,
    ).exists()
    active_exit_form = (
        SurveyForm.objects.filter(
            team=team,
            form_type=SurveyForm.FormType.EXIT_SURVEY,
            is_anonymous=False,
            is_active=True,
        )
        .order_by('-version', '-id')
        .first()
    )
    exit_survey_completed = bool(
        active_exit_form
        and ExitSurveySubmission.objects.filter(
            team=team,
            respondent=respondent,
            survey_form=active_exit_form,
            is_locked=True,
        ).exists()
    )
    document_completion_url = reverse(
        'surveys:preview-v2-completion',
        kwargs={'completion_type': 'documents'},
    )
    health_result_url = (
        reverse('surveys:result', kwargs={'day_no': current_day_no})
        + '?'
        + urlencode({'source': 'user', 'return_to': home_url})
    )
    reflection_completion_url = reverse(
        'surveys:preview-v2-completion',
        kwargs={'completion_type': 'reflection'},
    )
    return render(
        request,
        'surveys/public_v2/home.html',
        {
            'team': team,
            'respondent': respondent,
            'current_day_no': current_day_no,
            'required_document_progress': required_document_progress,
            'required_documents_completed': bool(
                required_document_progress.total
                and required_document_progress.completed == required_document_progress.total
            ),
            'home_url': home_url,
            'document_url': document_completion_url,
            'health_url': (
                health_result_url
                if health_completed
                else (
                    reverse('surveys:preview-v2-health-start')
                    if health_state.available
                    else home_url
                )
            ),
            'health_completed': health_completed,
            'health_available': health_state.available,
            'health_unavailable_message': (
                ''
                if health_state.available
                else HEALTH_CARD_UNAVAILABLE_MESSAGES[1 if page_language == 'en' else 0]
            ),
            'reflection_url': (
                reflection_completion_url
                if reflection_completed
                else reverse('surveys:preview-v2-reflection')
            ),
            'reflection_completed': reflection_completed,
            'exit_survey_url': (
                reverse(
                    'surveys:preview-v2-completion',
                    kwargs={'completion_type': 'survey'},
                )
                if exit_survey_completed
                else (
                    reverse('surveys:preview-v2-exit-start')
                    if active_exit_form
                    else home_url
                )
            ),
            'exit_survey_completed': exit_survey_completed,
            'preview_mode': True,
            'page_language': page_language,
            'language_url_ko': f'{home_url}?lang=ko',
            'language_url_en': f'{home_url}?lang=en',
        },
    )


def preview_v2_health_start_view(request: HttpRequest) -> HttpResponse:
    context_entities = _get_active_respondent_context_from_session(request)
    if not context_entities:
        return redirect('surveys:preview-v2-code')
    team, respondent = context_entities
    page_language = _public_language(request)
    if get_next_pending_required_document(team=team, respondent=respondent):
        return redirect('surveys:preview-v2-documents-next')

    health_state = _health_check_state(team=team, respondent=respondent)
    current_day_no = health_state.current_day_no
    if health_state.completed:
        home_url = reverse('surveys:preview-v2-home')
        return redirect(
            reverse('surveys:result', kwargs={'day_no': current_day_no})
            + '?'
            + urlencode({'source': 'user', 'return_to': home_url})
        )
    if not health_state.available:
        logger.warning(
            'health start unavailable: reason=%s team_id=%s current_day_no=%s',
            health_state.reason,
            team.id,
            current_day_no,
        )
        messages.warning(
            request,
            _day_survey_unavailable_message(
                health_state.reason,
                language=page_language,
                day_no=current_day_no,
                current_day_no=current_day_no,
            ),
        )
        return redirect('surveys:preview-v2-home')

    return render(
        request,
        'surveys/public_v2/health_start.html',
        {
            'team': team,
            'respondent': respondent,
            'day_no': current_day_no,
            'home_url': reverse('surveys:preview-v2-home'),
            'survey_url': (
                f'{reverse("surveys:day-survey", kwargs={"day_no": current_day_no})}'
                '?source=v2'
            ),
            'preview_mode': True,
            'page_language': page_language,
        },
    )


def preview_v2_exit_survey_start_view(request: HttpRequest) -> HttpResponse:
    context_entities = _get_active_respondent_context_from_session(request)
    if not context_entities:
        return redirect('surveys:preview-v2-code')
    team, respondent = context_entities
    page_language = _public_language(request)
    if get_next_pending_required_document(team=team, respondent=respondent):
        return redirect('surveys:preview-v2-documents-next')

    active_exit_form = (
        SurveyForm.objects.filter(
            team=team,
            form_type=SurveyForm.FormType.EXIT_SURVEY,
            is_anonymous=False,
            is_active=True,
        )
        .order_by('-version', '-id')
        .first()
    )
    if not active_exit_form:
        return redirect('surveys:preview-v2-home')
    if ExitSurveySubmission.objects.filter(
        team=team,
        respondent=respondent,
        survey_form=active_exit_form,
        is_locked=True,
    ).exists():
        return redirect(
            'surveys:preview-v2-completion',
            completion_type='survey',
        )

    return render(
        request,
        'surveys/public_v2/exit_survey_start.html',
        {
            'team': team,
            'respondent': respondent,
            'home_url': reverse('surveys:preview-v2-home'),
            'survey_url': reverse('surveys:exit-survey'),
            'preview_mode': True,
            'page_language': page_language,
        },
    )


def preview_v2_reflection_view(request: HttpRequest) -> HttpResponse:
    context_entities = _get_active_respondent_context_from_session(request)
    if not context_entities:
        return redirect('surveys:preview-v2-code')
    team, respondent = context_entities
    page_language = _public_language(request)

    if get_next_pending_required_document(team=team, respondent=respondent):
        return redirect('surveys:preview-v2-documents-next')
    if ReflectionSubmission.objects.filter(
        team=team,
        respondent=respondent,
        is_locked=True,
    ).exists():
        return redirect(
            'surveys:preview-v2-completion',
            completion_type='reflection',
        )

    form = ReflectionSubmissionForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                locked_respondent = Respondent.objects.select_for_update().get(
                    pk=respondent.pk,
                    team_id=team.pk,
                )
                current_team = Team.objects.select_for_update().get(pk=team.pk, is_active=True)
                if get_next_pending_required_document(
                    team=current_team,
                    respondent=locked_respondent,
                ):
                    return redirect('surveys:preview-v2-documents-next')
                if ReflectionSubmission.objects.filter(
                    team=current_team,
                    respondent=locked_respondent,
                ).exists():
                    return redirect(
                        'surveys:preview-v2-completion',
                        completion_type='reflection',
                    )

                if respondent_lacks_guardian_consent(team=current_team, respondent=locked_respondent):
                    logger.warning(
                        'reflection submit blocked: guardian consent not verified team_id=%s',
                        current_team.id,
                    )
                    messages.error(request, _guardian_consent_block_message(page_language))
                    return redirect('surveys:preview-v2-home')

                submission = form.save(commit=False)
                submission.team = current_team
                submission.respondent = locked_respondent
                submission.submitted_at = timezone.now()
                submission.is_locked = True
                submission.save()
        except (IntegrityError, Respondent.DoesNotExist, Team.DoesNotExist):
            if ReflectionSubmission.objects.filter(
                team=team,
                respondent=respondent,
            ).exists():
                return redirect(
                    'surveys:preview-v2-completion',
                    completion_type='reflection',
                )
            raise
        return redirect(
            'surveys:preview-v2-completion',
            completion_type='reflection',
        )

    return render(
        request,
        'surveys/public_v2/reflection.html',
        {
            'team': team,
            'respondent': respondent,
            'form': form,
            'home_url': reverse('surveys:preview-v2-home'),
            'preview_mode': True,
            'page_language': page_language,
        },
    )


def preview_v2_completion_view(
    request: HttpRequest,
    completion_type: str,
) -> HttpResponse:
    context_entities = _get_active_respondent_context_from_session(request)
    if not context_entities:
        return redirect('surveys:preview-v2-code')
    team, respondent = context_entities
    page_language = _public_language(request)
    screen = V2_COMPLETION_SCREENS.get(completion_type)
    if not screen:
        raise Http404('지원하지 않는 완료 화면입니다.')

    home_url = reverse('surveys:preview-v2-home')
    button_url = home_url
    if completion_type == 'documents':
        progress = get_required_document_progress(team=team, respondent=respondent)
        if not progress.total:
            return redirect('surveys:preview-v2-home')
        if not required_documents_completed(team=team, respondent=respondent):
            return redirect('surveys:preview-v2-documents-next')
    elif completion_type == 'health':
        current_day_no = calculate_team_day_no(team)
        submission_exists = SurveySubmission.objects.filter(
            team=team,
            respondent=respondent,
            day_no=current_day_no,
            is_locked=True,
        ).exists()
        if not submission_exists:
            return redirect('surveys:preview-v2-home')
        button_url = reverse(
            'surveys:result',
            kwargs={'day_no': current_day_no},
        ) + '?' + urlencode({'source': 'user', 'return_to': home_url})
    elif completion_type == 'reflection':
        if not ReflectionSubmission.objects.filter(
            team=team,
            respondent=respondent,
            is_locked=True,
        ).exists():
            return redirect('surveys:preview-v2-home')
    elif completion_type == 'survey':
        active_exit_form = (
            SurveyForm.objects.filter(
                team=team,
                form_type=SurveyForm.FormType.EXIT_SURVEY,
                is_anonymous=False,
                is_active=True,
            )
            .order_by('-version', '-id')
            .first()
        )
        if not active_exit_form or not ExitSurveySubmission.objects.filter(
            team=team,
            respondent=respondent,
            survey_form=active_exit_form,
            is_locked=True,
        ).exists():
            return redirect('surveys:preview-v2-home')

    return render(
        request,
        'surveys/public_v2/completion.html',
        {
            **screen,
            'completion_type': completion_type,
            'button_url': button_url,
            'preview_mode': True,
            'page_language': page_language,
        },
    )
