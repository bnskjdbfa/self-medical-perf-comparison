import json
from io import BytesIO

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import authenticate
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db.models import Count, F
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django_otp import login as otp_login
import segno
from urllib.parse import parse_qsl, urlencode, urlsplit

from .permissions import allowed_team_ids
from .forms import TwoFactorTokenForm
from .two_factor import confirmed_devices, consume_recovery_code, generate_recovery_codes, get_pending_device
from surveys.models import (
    ExitSurveySubmission,
    ReflectionSubmission,
    RequiredDocumentConsentBundle,
    SurveySubmission,
)
from surveys.services import normalize_name, normalize_phone
from teams.services import calculate_team_day_no
from teams.models import GenderChoices, Participant, Team

OPS_LOGIN_TEAM_ID = 'ops_login_team_id'
OPS_LOGIN_VERIFIED_TEAM_ID = 'ops_login_verified_team_id'
RISK_LEVEL_ORDER = {'최상': 0, '정상': 1, '주의': 2, '경고': 3, '위험': 4}
SUBMISSION_STATUS_OPTIONS = [
    {'value': 'all', 'label': '제출자/미제출자'},
    {'value': 'submitted', 'label': '제출'},
    {'value': 'pending', 'label': '미제출'},
]
MANAGER_LOOKUP_PAGE_SIZE = 20
RISK_LEVEL_OPTIONS = [
    {'value': 'all', 'label': '위험군'},
    {'value': '최상', 'label': '최상'},
    {'value': '정상', 'label': '정상'},
    {'value': '주의', 'label': '주의'},
    {'value': '경고', 'label': '경고'},
    {'value': '위험', 'label': '위험'},
]
MANAGER_TAB_HEALTH = 'health'
MANAGER_TAB_OPTIONS = (
    {'key': 'documents', 'label': '동의서', 'theme': 'documents'},
    {'key': MANAGER_TAB_HEALTH, 'label': '건강', 'theme': 'health'},
    {'key': 'reflection', 'label': '소감문', 'theme': 'reflection'},
    {'key': 'survey', 'label': '설문지', 'theme': 'survey'},
)


def _staff_2fa_required(view_func):
    @login_required
    def wrapped(request, *args, **kwargs):
        if not (request.user.is_staff or request.user.is_superuser):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return wrapped


def _two_factor_next(request) -> str:
    value = request.POST.get("next") or request.GET.get("next")
    if value and url_has_allowed_host_and_scheme(value, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        if value.startswith("/admin/") or value.startswith("/ops/"):
            return value
    return reverse("admin:index")


def _otp_failure_key(user_id: int) -> str:
    return f"admin-2fa-failures:{user_id}"


@_staff_2fa_required
def two_factor_setup_view(request):
    next_url = _two_factor_next(request)
    existing = confirmed_devices(request.user).first()
    if existing:
        return redirect(f"{reverse('admins:two-factor-verify')}?{urlencode({'next': next_url})}")
    device = get_pending_device(request.user)
    form = TwoFactorTokenForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            token = int(form.cleaned_data["token"])
        except ValueError:
            token = -1
        if device.verify_token(token):
            device.confirmed = True
            device.save(update_fields=["confirmed"])
            otp_login(request, device)
            request.session["admin_2fa_new_recovery_codes"] = generate_recovery_codes(request.user)
            request.session["admin_2fa_next"] = next_url
            return redirect("admins:two-factor-recovery-codes")
        form.add_error("token", "인증 코드가 올바르지 않습니다.")
    return render(request, "admins/two_factor_setup.html", {"form": form, "next": next_url})


@_staff_2fa_required
def two_factor_qr_view(request):
    if confirmed_devices(request.user).exists():
        return HttpResponse(status=404)
    device = get_pending_device(request.user)
    output = BytesIO()
    segno.make(device.config_url).save(output, kind="svg", scale=5, xmldecl=False)
    return HttpResponse(output.getvalue(), content_type="image/svg+xml")


@_staff_2fa_required
def two_factor_verify_view(request):
    next_url = _two_factor_next(request)
    if request.user.is_verified():
        return redirect(next_url)
    devices = list(confirmed_devices(request.user))
    if not devices:
        return redirect(f"{reverse('admins:two-factor-setup')}?{urlencode({'next': next_url})}")
    form = TwoFactorTokenForm(request.POST or None)
    failure_key = _otp_failure_key(request.user.id)
    if request.method == "POST" and form.is_valid():
        failures = cache.get(failure_key, 0)
        if failures >= 5:
            form.add_error(None, "인증 시도가 잠겼습니다. 5분 뒤 다시 시도해주세요.")
        else:
            raw_token = form.cleaned_data["token"]
            verified_device = None
            if raw_token.isdigit():
                numeric_token = int(raw_token)
                verified_device = next((device for device in devices if device.verify_token(numeric_token)), None)
            recovery_verified = verified_device is None and consume_recovery_code(request.user, raw_token)
            if verified_device or recovery_verified:
                otp_login(request, verified_device or devices[0])
                cache.delete(failure_key)
                return redirect(next_url)
            cache.set(failure_key, failures + 1, 300)
            form.add_error("token", "인증 코드 또는 복구 코드가 올바르지 않습니다.")
    return render(request, "admins/two_factor_verify.html", {"form": form, "next": next_url})


@_staff_2fa_required
def two_factor_recovery_codes_view(request):
    if not request.user.is_verified():
        return redirect("admins:two-factor-verify")
    codes = request.session.pop("admin_2fa_new_recovery_codes", None)
    if not codes:
        return redirect(reverse("admin:index"))
    next_url = request.session.pop("admin_2fa_next", reverse("admin:index"))
    return render(request, "admins/two_factor_recovery_codes.html", {"codes": codes, "next": next_url})


def _allowed_teams_for_user(user):
    if user.is_superuser:
        return Team.objects.filter(is_active=True).order_by('name')
    return Team.objects.filter(id__in=allowed_team_ids(user), is_active=True).order_by('name')


def _resolve_team_from_request(request, teams):
    selected_team_id = request.GET.get('team_id')
    if selected_team_id:
        selected_team = teams.filter(id=selected_team_id).first()
        if not selected_team:
            raise PermissionDenied('허용되지 않은 팀입니다.')
        return selected_team
    return teams.first()


def _get_scoped_login_team(request):
    scoped_team_id = request.session.get(OPS_LOGIN_TEAM_ID)
    if not scoped_team_id:
        return None
    return Team.objects.filter(id=scoped_team_id, is_active=True).first()


def _get_verified_login_team_id(request):
    return request.session.get(OPS_LOGIN_VERIFIED_TEAM_ID)


def _effective_team_ids_for_request(request):
    scoped_team = _get_scoped_login_team(request)
    if scoped_team:
        if request.user.is_superuser:
            return [scoped_team.id]
        if scoped_team.id in allowed_team_ids(request.user):
            return [scoped_team.id]
        return []
    return list(_allowed_teams_for_user(request.user).values_list('id', flat=True))


def _user_can_access_team(user, team: Team | None) -> bool:
    if not user.is_authenticated or not team:
        return False
    if user.is_superuser:
        return True
    if not user.is_staff:
        return False
    return user.team_permissions.filter(team=team).exists()


def _safe_internal_return_to(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return None
    if not value.startswith('/'):
        return None
    if value.startswith('//'):
        return None
    return value


def _coerce_day_no(*, team: Team | None, value: str | None) -> int:
    if not team:
        return 1
    current_day_no = min(max(calculate_team_day_no(team), 1), team.max_day_no)
    if value is None:
        return current_day_no
    try:
        day_no = int(value)
    except (TypeError, ValueError):
        return current_day_no
    return min(max(day_no, 1), team.max_day_no)


def _section_map(submission: SurveySubmission | dict | None) -> dict:
    if not submission:
        return {}
    if isinstance(submission, dict):
        if 'result_json' not in submission:
            return {
                'body': {
                    'status_label': submission.get('body_status_label'),
                    'average_score': submission.get('body_average_score'),
                },
                'emotion': {
                    'status_label': submission.get('emotion_status_label'),
                    'average_score': submission.get('emotion_average_score'),
                },
            }
        return ((submission.get('result_json') or {}).get('sections') or {})
    return ((submission.result_json or {}).get('sections') or {})


def _worst_risk_label(section_map: dict) -> str | None:
    labels = []
    for section_key in ('body', 'emotion'):
        label = (section_map.get(section_key) or {}).get('status_label')
        if label in RISK_LEVEL_ORDER:
            labels.append(label)
    if not labels:
        return None
    return max(labels, key=lambda label: RISK_LEVEL_ORDER[label])


def _average_score(section_map: dict) -> int | None:
    scores = [
        int((section_map.get(section_key) or {}).get('average_score', 0) or 0)
        for section_key in ('body', 'emotion')
        if section_map.get(section_key) is not None
    ]
    if not scores:
        return None
    return round(sum(scores) / len(scores))


def _build_manager_lookup_context(
    *,
    team: Team | None,
    day_no: int,
    submission_status: str,
    risk_level: str,
    name_keyword: str,
    reset_url: str,
    current_full_path: str,
) -> dict:
    requested_page = _requested_manager_page(current_full_path)
    page_start = (requested_page - 1) * MANAGER_LOOKUP_PAGE_SIZE
    page_end = page_start + MANAGER_LOOKUP_PAGE_SIZE
    participants = (
        Participant.objects.filter(team=team, is_active=True)
        .values('id', 'name', 'birth_date', 'phone', 'gender', 'normalized_name')
        .order_by('name')
        if team else []
    )
    submissions = (
        SurveySubmission.objects.filter(team=team, day_no=day_no)
        .select_related('respondent')
        .annotate(
            body_status_label=F('result_json__sections__body__status_label'),
            body_average_score=F('result_json__sections__body__average_score'),
            emotion_status_label=F('result_json__sections__emotion__status_label'),
            emotion_average_score=F('result_json__sections__emotion__average_score'),
        )
        .values(
            'id',
            'day_no',
            'submitted_at',
            'respondent__birth_date',
            'respondent__gender',
            'respondent__name',
            'respondent__normalized_name',
            'body_status_label',
            'body_average_score',
            'emotion_status_label',
            'emotion_average_score',
        )
        if team else []
    )
    submission_map = {
        (
            sub['respondent__normalized_name'] or normalize_name(sub['respondent__name']),
            sub['respondent__birth_date'],
            sub['respondent__gender'],
        ): sub
        for sub in submissions.iterator(chunk_size=200)
    }
    gender_labels = dict(GenderChoices.choices)

    total_count = 0
    submitted_count = 0
    danger_count = 0
    page_rows = []
    last_page_rows = []
    normalized_keyword = name_keyword.strip()

    for participant in participants.iterator(chunk_size=200) if team else []:
        key = (
            participant['normalized_name'] or normalize_name(participant['name']),
            participant['birth_date'],
            participant['gender'],
        )
        submission = submission_map.get(key)

        section_map = _section_map(submission)
        overall_risk = _worst_risk_label(section_map)
        score = _average_score(section_map)
        is_submitted = submission is not None
        status_key = 'submitted' if is_submitted else 'pending'
        status_label = '제출' if is_submitted else '미제출'
        is_danger = overall_risk in {'경고', '위험'}
        phone_value = participant['phone'] or '-'
        gender_label = gender_labels.get(participant['gender'], GenderChoices.UNSPECIFIED.label)

        if submission_status == 'submitted' and status_key != 'submitted':
            continue
        if submission_status == 'pending' and status_key != 'pending':
            continue
        if risk_level != 'all' and overall_risk != risk_level:
            continue
        if normalized_keyword and normalized_keyword not in participant['name']:
            continue

        row_index = total_count
        total_count += 1
        if is_submitted:
            submitted_count += 1
        if is_danger:
            danger_count += 1
        should_keep_row = page_start <= row_index < page_end or requested_page > 1
        if should_keep_row:
            row = {
                'participant_id': participant['id'],
                'name': participant['name'],
                'phone': phone_value,
                'phone_href': f'tel:{normalize_phone(phone_value)}' if normalize_phone(phone_value) else '',
                'gender': gender_label,
                'score': score,
                'score_label': f'{score}점' if score is not None else '-',
                'status_key': status_key,
                'status_label': status_label,
                'risk_label': overall_risk,
                'is_danger': is_danger,
                'submitted_at': submission['submitted_at'].strftime('%H:%M') if submission else '없음',
                'result_url': (
                    reverse('surveys:result', kwargs={'day_no': submission['day_no']})
                    + '?'
                    + urlencode(
                        {
                            'source': 'manager',
                            'submission_id': submission['id'],
                            'return_to': current_full_path,
                        }
                    )
                ) if submission else '',
            }
            if page_start <= row_index < page_end:
                page_rows.append(row)
            if requested_page > 1:
                last_page_rows.append(row)
                if len(last_page_rows) > MANAGER_LOOKUP_PAGE_SIZE:
                    last_page_rows.pop(0)

    pending_count = total_count - submitted_count
    page_number, page_count, pagination = _build_manager_pagination(
        requested_page=requested_page,
        total_count=total_count,
        rendered_count=len(page_rows),
        current_full_path=current_full_path,
    )
    if requested_page > page_count:
        page_rows = last_page_rows
        pagination['rendered_count'] = len(page_rows)

    team_name = team.name if team else '조회 가능한 팀이 없습니다'
    current_day_no = min(max(calculate_team_day_no(team), 1), team.max_day_no) if team else 1
    return {
        'team': team,
        'team_name': team_name,
        'has_team_permission': team is not None,
        'current_day_no': current_day_no,
        'day_options': [
            {'value': value, 'label': f'{value}일차', 'selected': value == day_no}
            for value in range(1, (team.max_day_no if team else 1) + 1)
        ],
        'submission_status_options': [
            {**option, 'selected': option['value'] == submission_status}
            for option in SUBMISSION_STATUS_OPTIONS
        ],
        'risk_level_options': [
            {**option, 'selected': option['value'] == risk_level}
            for option in RISK_LEVEL_OPTIONS
        ],
        'filters': {
            'day_no': day_no,
            'submission_status': submission_status,
            'risk_level': risk_level,
            'name_keyword': name_keyword,
        },
        'summary': {
            'total_count': total_count,
            'submitted_count': submitted_count,
            'pending_count': pending_count,
            'danger_count': danger_count,
        },
        'rows': page_rows,
        'list_count': total_count,
        'pagination': pagination,
        'search_url': '',
        'reset_url': reset_url,
    }


def _participant_identity_key(values: dict, *, prefix: str = '') -> tuple:
    return (
        values.get(f'{prefix}normalized_name') or normalize_name(values.get(f'{prefix}name') or ''),
        values.get(f'{prefix}birth_date'),
        values.get(f'{prefix}gender'),
    )


def _build_submission_status_context(
    *,
    team: Team | None,
    tab: str,
    submission_status: str,
    name_keyword: str,
    reset_url: str,
    current_full_path: str,
) -> dict:
    requested_page = _requested_manager_page(current_full_path)
    page_start = (requested_page - 1) * MANAGER_LOOKUP_PAGE_SIZE
    page_end = page_start + MANAGER_LOOKUP_PAGE_SIZE
    participants = (
        Participant.objects.filter(team=team, is_active=True)
        .values('id', 'name', 'birth_date', 'phone', 'gender', 'normalized_name')
        .order_by('name')
        if team else []
    )

    if not team:
        submitted_identity_keys = set()
    elif tab == 'documents':
        submitted_rows = (
            RequiredDocumentConsentBundle.objects.filter(team=team, completed_at__isnull=False)
            .select_related('respondent')
            .values(
                'respondent__name',
                'respondent__birth_date',
                'respondent__normalized_name',
                'respondent__gender',
            )
        )
        submitted_identity_keys = {
            _participant_identity_key(row, prefix='respondent__')
            for row in submitted_rows.iterator(chunk_size=200)
        }
    elif tab == 'reflection':
        submitted_rows = (
            ReflectionSubmission.objects.filter(team=team)
            .select_related('respondent')
            .values(
                'respondent__name',
                'respondent__birth_date',
                'respondent__normalized_name',
                'respondent__gender',
            )
        )
        submitted_identity_keys = {
            _participant_identity_key(row, prefix='respondent__')
            for row in submitted_rows.iterator(chunk_size=200)
        }
    else:
        submitted_rows = (
            ExitSurveySubmission.objects.filter(team=team)
            .select_related('respondent')
            .values(
                'respondent__name',
                'respondent__birth_date',
                'respondent__normalized_name',
                'respondent__gender',
            )
        )
        submitted_identity_keys = {
            _participant_identity_key(row, prefix='respondent__')
            for row in submitted_rows.iterator(chunk_size=200)
        }

    gender_labels = dict(GenderChoices.choices)
    normalized_keyword = name_keyword.strip()
    total_count = 0
    submitted_count = 0
    page_rows = []
    last_page_rows = []

    for participant in participants.iterator(chunk_size=200) if team else []:
        is_submitted = _participant_identity_key(participant) in submitted_identity_keys
        status_key = 'submitted' if is_submitted else 'pending'
        if submission_status in {'submitted', 'pending'} and submission_status != status_key:
            continue
        if normalized_keyword and normalized_keyword not in participant['name']:
            continue

        row_index = total_count
        total_count += 1
        submitted_count += int(is_submitted)
        if page_start <= row_index < page_end or requested_page > 1:
            phone_value = participant['phone'] or '-'
            row = {
                'participant_id': participant['id'],
                'name': participant['name'],
                'phone': phone_value,
                'phone_href': f'tel:{normalize_phone(phone_value)}' if normalize_phone(phone_value) else '',
                'gender': gender_labels.get(participant['gender'], GenderChoices.UNSPECIFIED.label),
                'status_key': status_key,
                'status_label': '제출' if is_submitted else '미제출',
                'is_danger': False,
                'score_label': '',
                'result_url': '',
            }
            if page_start <= row_index < page_end:
                page_rows.append(row)
            if requested_page > 1:
                last_page_rows.append(row)
                if len(last_page_rows) > MANAGER_LOOKUP_PAGE_SIZE:
                    last_page_rows.pop(0)

    page_number, page_count, pagination = _build_manager_pagination(
        requested_page=requested_page,
        total_count=total_count,
        rendered_count=len(page_rows),
        current_full_path=current_full_path,
    )
    if requested_page > page_count:
        page_rows = last_page_rows
        pagination['rendered_count'] = len(page_rows)

    return {
        'team': team,
        'team_name': team.name if team else '조회 가능한 팀이 없습니다',
        'has_team_permission': team is not None,
        'submission_status_options': [
            {**option, 'selected': option['value'] == submission_status}
            for option in SUBMISSION_STATUS_OPTIONS
        ],
        'filters': {
            'submission_status': submission_status,
            'name_keyword': name_keyword,
        },
        'summary': {
            'total_count': total_count,
            'submitted_count': submitted_count,
            'pending_count': total_count - submitted_count,
            'danger_count': 0,
        },
        'rows': page_rows,
        'list_count': total_count,
        'pagination': pagination,
        'search_url': '',
        'reset_url': reset_url,
    }


def _requested_manager_page(current_full_path: str) -> int:
    parsed = urlsplit(current_full_path)
    query_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    try:
        return max(int(query_params.get('page', '1')), 1)
    except (TypeError, ValueError):
        return 1


def _build_manager_pagination(
    *,
    requested_page: int,
    total_count: int,
    rendered_count: int,
    current_full_path: str,
) -> tuple[int, int, dict]:
    parsed = urlsplit(current_full_path)
    query_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    page_count = max(1, (total_count + MANAGER_LOOKUP_PAGE_SIZE - 1) // MANAGER_LOOKUP_PAGE_SIZE)
    page_number = min(max(requested_page, 1), page_count)

    def page_url(target_page: int) -> str:
        params = {key: value for key, value in query_params.items() if key != 'page'}
        params['page'] = str(target_page)
        return parsed.path + '?' + urlencode(params)

    return page_number, page_count, {
        'page': page_number,
        'page_count': page_count,
        'page_size': MANAGER_LOOKUP_PAGE_SIZE,
        'total_count': total_count,
        'rendered_count': rendered_count,
        'has_previous': page_number > 1,
        'has_next': page_number < page_count,
        'previous_url': page_url(page_number - 1) if page_number > 1 else '',
        'next_url': page_url(page_number + 1) if page_number < page_count else '',
    }

def build_day_summary(team_ids: list[int] | None = None) -> list[dict]:
    submissions = SurveySubmission.objects.all()
    if team_ids is not None:
        submissions = submissions.filter(team_id__in=team_ids)
    rows = (
        submissions.values('day_no')
        .annotate(total=Count('id'))
        .order_by('day_no')
    )
    return list(rows)


def build_submission_rows(team_ids: list[int] | None = None) -> list[dict]:
    submissions = (
        SurveySubmission.objects
        .select_related('team', 'respondent')
        .only(
            'id',
            'team_id',
            'respondent_id',
            'day_no',
            'submitted_at',
            'is_locked',
            'team__id',
            'team__code',
            'respondent__id',
            'respondent__name',
        )
        .order_by('-submitted_at')
    )
    if team_ids is not None:
        submissions = submissions.filter(team_id__in=team_ids)
    return [
        {
            'submission_id': submission.id,
            'team_id': submission.team_id,
            'team_code': submission.team.code,
            'respondent_id': submission.respondent_id,
            'respondent_name': submission.respondent.name,
            'day_no': submission.day_no,
            'submitted_at': submission.submitted_at,
            'is_locked': submission.is_locked,
        }
        for submission in submissions.iterator(chunk_size=200)
    ]


def build_submission_detail(submission_id: int, team_ids: list[int] | None = None) -> dict:
    submission = get_object_or_404(
        SurveySubmission.objects
        .select_related('team', 'respondent')
        .only(
            'id',
            'team_id',
            'respondent_id',
            'day_no',
            'submitted_at',
            'answers_json',
            'result_json',
            'form_snapshot_json',
            'team__id',
            'team__name',
            'team__code',
            'respondent__id',
            'respondent__name',
            'respondent__gender',
        ),
        id=submission_id,
    )
    if team_ids is not None and submission.team_id not in team_ids:
        raise PermissionDenied('허용되지 않은 팀입니다.')

    def display_answer(value):
        if value in (None, '', []):
            return '미입력'
        if isinstance(value, list):
            return ', '.join(str(item) for item in value) or '미입력'
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    answers_json = submission.answers_json if isinstance(submission.answers_json, dict) else {}
    snapshot = submission.form_snapshot_json if isinstance(submission.form_snapshot_json, dict) else {}
    snapshot_sections = snapshot.get('sections') if isinstance(snapshot.get('sections'), list) else []
    answers = []
    subjective_answers = []
    handled_keys = set()

    for section in snapshot_sections:
        if not isinstance(section, dict):
            continue
        section_title = section.get('title') or ''
        questions = section.get('questions') if isinstance(section.get('questions'), list) else []
        for question in questions:
            if not isinstance(question, dict):
                continue
            question_key = str(question.get('key') or '').strip()
            if not question_key:
                continue
            handled_keys.add(question_key)
            answer_value = answers_json.get(question_key)
            answer_text = answer_value if isinstance(answer_value, str) else ''
            question_type = question.get('question_type') or ''
            item = {
                'question_id': question.get('id'),
                'question_key': question_key,
                'question_label': question.get('label') or question_key,
                'question_type': question_type,
                'section_title': section_title,
                'answer_text': answer_text,
                'answer_json': {'value': answer_value},
                'display_answer': display_answer(answer_value),
            }
            answers.append(item)
            if question_type == 'text':
                subjective_answers.append(item)

    missing_keys = set(answers_json) - handled_keys
    if missing_keys:
        legacy_answers = {
            answer.question.key: answer
            for answer in submission.answers.select_related('question__section').filter(
                question__key__in=missing_keys,
            )
        }
        for question_key in answers_json:
            if question_key not in missing_keys:
                continue
            answer_value = answers_json.get(question_key)
            legacy_answer = legacy_answers.get(question_key)
            question_type = legacy_answer.question.question_type if legacy_answer else ''
            answer_text = answer_value if isinstance(answer_value, str) else ''
            item = {
                'question_id': legacy_answer.question_id if legacy_answer else None,
                'question_key': question_key,
                'question_label': (
                    legacy_answer.question.label
                    if legacy_answer and legacy_answer.question.label
                    else question_key
                ),
                'question_type': question_type,
                'section_title': legacy_answer.question.section.title if legacy_answer else '',
                'answer_text': answer_text,
                'answer_json': {'value': answer_value},
                'display_answer': display_answer(answer_value),
            }
            answers.append(item)
            if question_type == 'text':
                subjective_answers.append(item)

    if not answers and not answers_json:
        for answer in submission.answers.select_related('question__section').order_by(
            'question__section__display_order',
            'question__display_order',
            'id',
        ):
            answer_value = answer.answer_text
            if not answer_value:
                answer_value = answer.answer_json.get('value') if isinstance(answer.answer_json, dict) else ''
            item = {
                'question_id': answer.question_id,
                'question_key': answer.question.key,
                'question_label': answer.question.label or answer.question.key,
                'question_type': answer.question.question_type,
                'section_title': answer.question.section.title,
                'answer_text': answer.answer_text,
                'answer_json': answer.answer_json,
                'display_answer': display_answer(answer_value),
            }
            answers.append(item)
            if answer.question.question_type == answer.question.QuestionType.TEXT:
                subjective_answers.append(item)

    sections = (submission.result_json or {}).get('sections') or {}
    return {
        'submission_id': submission.id,
        'team': {'id': submission.team_id, 'name': submission.team.name, 'code': submission.team.code},
        'respondent': {
            'id': submission.respondent_id,
            'name': submission.respondent.name,
            'phone': submission.respondent.phone,
            'gender': submission.respondent.get_gender_display() if submission.respondent.gender else GenderChoices.UNSPECIFIED.label,
        },
        'day_no': submission.day_no,
        'submitted_at': submission.submitted_at,
        'answers': answers,
        'subjective_answers': subjective_answers,
        'sections': sections,
        'result_json': submission.result_json,
    }


def manager_login_view(request):
    scoped_team = _get_scoped_login_team(request)
    if not scoped_team:
        messages.error(request, '유효한 관리자 팀 진입 경로가 아닙니다. 팀 관리자 코드를 다시 입력해주세요.')
        return redirect('surveys:preview-v2-code')

    username_value = ''
    error_message = ''
    if request.method == 'POST':
        username_value = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        user = authenticate(request, username=username_value, password=password)
        if not user:
            error_message = '아이디 또는 비밀번호가 올바르지 않습니다.'
        elif not (user.is_staff or user.is_superuser):
            error_message = '조회용 관리자 권한이 없습니다.'
        elif not _user_can_access_team(user, scoped_team):
            error_message = '해당 팀 조회 권한이 없습니다.'
        else:
            login(request, user)
            request.session[OPS_LOGIN_TEAM_ID] = scoped_team.id
            request.session[OPS_LOGIN_VERIFIED_TEAM_ID] = scoped_team.id
            return redirect(reverse('admins:prototype-manager'))

    return render(
        request,
        'admins/login.html',
        {
            'team': scoped_team,
            'username_value': username_value,
            'error_message': error_message,
        },
    )


@require_POST
def manager_logout_view(request):
    # A revoked bearer must not authenticate, but a CSRF-protected logout can
    # still revoke this browser's verified family while a rotation is in flight.
    logout(request)
    request.session.pop(OPS_LOGIN_TEAM_ID, None)
    request.session.pop(OPS_LOGIN_VERIFIED_TEAM_ID, None)
    return redirect('surveys:preview-v2-code')


@staff_member_required
def dashboard_api_view(request):
    team_ids = _effective_team_ids_for_request(request)
    return JsonResponse({'success': True, 'data': {'summary': build_day_summary(team_ids)}})


@staff_member_required
def respondent_list_api_view(request):
    team_ids = _effective_team_ids_for_request(request)
    return JsonResponse({'success': True, 'data': {'submissions': build_submission_rows(team_ids)}})


@staff_member_required
def response_detail_api_view(request, submission_id: int):
    team_ids = _effective_team_ids_for_request(request)
    return JsonResponse({'success': True, 'data': build_submission_detail(submission_id, team_ids)})


@staff_member_required
def dashboard_debug_view(request):
    team_ids = _effective_team_ids_for_request(request)
    return render(request, 'admins/dashboard.html', {'summary': build_day_summary(team_ids)})


@staff_member_required
def respondent_list_debug_view(request):
    team_ids = _effective_team_ids_for_request(request)
    return render(request, 'admins/respondent_list.html', {'submissions': build_submission_rows(team_ids)})


@staff_member_required
def response_detail_debug_view(request, submission_id: int):
    team_ids = _effective_team_ids_for_request(request)
    return render(request, 'admins/response_detail.html', {'submission': build_submission_detail(submission_id, team_ids)})

def prototype_manager_view(request):
    scoped_team = _get_scoped_login_team(request)
    verified_team_id = _get_verified_login_team_id(request)
    if not scoped_team:
        return redirect('surveys:preview-v2-code')
    if not request.user.is_authenticated or not (request.user.is_staff or request.user.is_superuser):
        return redirect(reverse('admins:login'))
    if not _user_can_access_team(request.user, scoped_team):
        return redirect(reverse('admins:login'))
    if verified_team_id != scoped_team.id:
        return redirect(reverse('admins:login'))
    team = scoped_team
    tab = request.GET.get('tab', MANAGER_TAB_HEALTH)
    if tab not in {option['key'] for option in MANAGER_TAB_OPTIONS}:
        tab = MANAGER_TAB_HEALTH
    day_no = _coerce_day_no(team=team, value=request.GET.get('day_no'))
    submission_status = request.GET.get('submission_status', 'all')
    risk_level = request.GET.get('risk_level', 'all')
    name_keyword = request.GET.get('name_keyword', '').strip()
    reset_url = f'{request.path}?tab={tab}'
    if tab == MANAGER_TAB_HEALTH:
        reset_url += f'&day_no={min(max(calculate_team_day_no(team), 1), team.max_day_no)}'
        context = _build_manager_lookup_context(
            team=team,
            day_no=day_no,
            submission_status=submission_status,
            risk_level=risk_level,
            name_keyword=name_keyword,
            reset_url=reset_url,
            current_full_path=request.get_full_path(),
        )
    else:
        context = _build_submission_status_context(
            team=team,
            tab=tab,
            submission_status=submission_status,
            name_keyword=name_keyword,
            reset_url=reset_url,
            current_full_path=request.get_full_path(),
        )
    context.update(
        {
            'active_tab': tab,
            'is_health_tab': tab == MANAGER_TAB_HEALTH,
            'manager_tabs': MANAGER_TAB_OPTIONS,
        }
    )
    context['search_url'] = request.path
    return render(request, 'admins/manager_lookup.html', context)
