from __future__ import annotations

import unicodedata
from collections import defaultdict

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from teams.models import Participant, Respondent, Team
from .models import SurveyForm, SurveyQuestion, SurveySection, SurveySubmission


SECTION_RESULT_META = {
    'body': {'title': 'Body', 'accent': 'lime'},
    'emotion': {'title': 'Emotion', 'accent': 'violet'},
}
SECTION_TYPE_ORDER = {
    SurveySection.SectionType.PHYSICAL: 0,
    SurveySection.SectionType.MENTAL: 1,
    SurveySection.SectionType.SURVEY: 2,
}
_CACHE_MISS = object()


def _team_cache_token(team: Team) -> str:
    return timezone.localtime(team.updated_at).strftime('%Y%m%d%H%M%S%f')


def get_active_form_day_numbers(*, team: Team) -> set[int]:
    cache_ttl = getattr(settings, 'SURVEY_FORM_CACHE_TTL', 60)
    cache_key = f'survey:active_form_days:{team.id}:{team.max_day_no}:{_team_cache_token(team)}'
    cached = cache.get(cache_key, _CACHE_MISS)
    if cached is not _CACHE_MISS:
        return set(cached)

    day_numbers = set(
        SurveyForm.objects.filter(
            team=team,
            form_type=SurveyForm.FormType.DAILY_HEALTH,
            is_anonymous=False,
            is_active=True,
            day_no__gte=1,
            day_no__lte=team.max_day_no,
        ).values_list('day_no', flat=True)
    )
    cache.set(cache_key, tuple(day_numbers), cache_ttl)
    return day_numbers


def active_survey_form_queryset(*, team: Team, day_no: int):
    """Active, non-anonymous daily health forms for the team/day, newest version first.

    Single source of truth for "which form is the participant answering today". The
    cached lookup, the uncached re-check on submit, and the public API all go through
    here, so an exit-survey form deployed for the same day can never shadow the daily
    health form (form versions are numbered per team/day regardless of form type).
    """
    return (
        SurveyForm.objects
        .filter(
            team=team,
            day_no=day_no,
            form_type=SurveyForm.FormType.DAILY_HEALTH,
            is_anonymous=False,
            is_active=True,
        )
        .prefetch_related('sections__questions')
        .order_by('-version')
    )


def get_active_survey_form(*, team: Team, day_no: int) -> SurveyForm | None:
    cache_ttl = getattr(settings, 'SURVEY_FORM_CACHE_TTL', 60)
    cache_key = f'survey:active_form:{team.id}:{day_no}:{_team_cache_token(team)}'
    cached = cache.get(cache_key, _CACHE_MISS)
    if cached is not _CACHE_MISS:
        return cached

    form = active_survey_form_queryset(team=team, day_no=day_no).first()
    if form is not None:
        # A missing form is rare and cheap to re-query. Caching the miss would keep
        # every worker answering "no form" for a full TTL after an admin deploys one.
        cache.set(cache_key, form, cache_ttl)
    return form


def invalidate_survey_form_cache(*, team: Team, day_no: int) -> None:
    team_token = _team_cache_token(team)
    cache.delete(
        f'survey:active_form:{team.id}:{day_no}:{team_token}'
    )
    cache.delete(
        f'survey:active_form_days:{team.id}:{team.max_day_no}:{team_token}'
    )


def _respondent_cache_token(respondent: Respondent) -> str:
    return timezone.localtime(respondent.updated_at).strftime('%Y%m%d%H%M%S%f')


def _submitted_days_cache_key(*, team: Team, respondent: Respondent) -> str:
    return (
        f'survey:submitted_days:{team.id}:{respondent.id}:{team.max_day_no}:'
        f'{_team_cache_token(team)}:{_respondent_cache_token(respondent)}'
    )


def _selected_submission_cache_key(*, team: Team, respondent: Respondent, day_no: int) -> str:
    return (
        f'survey:selected_submission:{team.id}:{respondent.id}:{day_no}:'
        f'{_team_cache_token(team)}:{_respondent_cache_token(respondent)}'
    )


def get_submitted_day_numbers(*, team: Team, respondent: Respondent) -> set[int]:
    cache_ttl = getattr(settings, 'SURVEY_SUBMISSION_CACHE_TTL', 30)
    cache_key = _submitted_days_cache_key(team=team, respondent=respondent)
    cached = cache.get(cache_key, _CACHE_MISS)
    if cached is not _CACHE_MISS:
        return set(cached)

    day_numbers = set(
        SurveySubmission.objects.filter(
            team=team,
            respondent=respondent,
            day_no__gte=1,
            day_no__lte=team.max_day_no,
        ).values_list('day_no', flat=True)
    )
    cache.set(cache_key, tuple(day_numbers), cache_ttl)
    return day_numbers


def get_selected_submission(*, team: Team, respondent: Respondent, day_no: int) -> SurveySubmission | None:
    cache_ttl = getattr(settings, 'SURVEY_SUBMISSION_CACHE_TTL', 30)
    cache_key = _selected_submission_cache_key(team=team, respondent=respondent, day_no=day_no)
    cached = cache.get(cache_key, _CACHE_MISS)
    if cached is not _CACHE_MISS:
        return cached

    submission = (
        SurveySubmission.objects.filter(
            team=team,
            respondent=respondent,
            day_no=day_no,
        )
        .only(
            'id',
            'team_id',
            'respondent_id',
            'day_no',
            'answers_json',
            'result_json',
            'form_snapshot_json',
            'submitted_at',
        )
        .first()
    )
    cache.set(cache_key, submission, cache_ttl)
    return submission


def invalidate_submission_cache(*, team: Team, respondent: Respondent, day_no: int) -> None:
    cache.delete(_submitted_days_cache_key(team=team, respondent=respondent))
    cache.delete(_selected_submission_cache_key(team=team, respondent=respondent, day_no=day_no))


def normalize_name(value: str) -> str:
    """Identity form of a name: compatibility-normalized, accent-stripped, case-folded,
    letters and digits only. So ``Anne-Marie Lee``, ``anne marie/lee`` and ``ＡＮＮＥ MARIE LEE``
    all become ``annemarielee``; ``Nguyễn`` becomes ``nguyen``. Hangul is preserved as
    syllables. Word order is NOT ignored (``Kim Alex`` != ``Alex Kim``)."""
    text = unicodedata.normalize('NFKC', str(value or ''))
    text = unicodedata.normalize('NFD', text)
    text = ''.join(ch for ch in text if unicodedata.category(ch) != 'Mn')
    text = unicodedata.normalize('NFC', text)
    return ''.join(ch for ch in text.casefold() if ch.isalnum())


IDENTITY_FIELDS = ('name', 'birth_date', 'gender')


def diagnose_identity_mismatch(*, team: Team, name: str, birth_date, gender: str | None) -> set[str]:
    """Which identity fields most likely differ from the roster (for highlighting only).

    Rule agreed on 2026-09-13: prefer a roster row with the same name and flag the fields that
    differ from it; otherwise, if someone has the same birth date and gender, flag the name;
    otherwise flag all three. The roster values themselves are never returned.
    """
    roster = Participant.objects.filter(team=team, is_active=True)
    same_name = list(roster.filter(normalized_name__iexact=normalize_name(name)).only('birth_date', 'gender'))
    if same_name:
        best = max(same_name, key=lambda row: (row.birth_date == birth_date) + (row.gender == gender))
        fields = set()
        if best.birth_date != birth_date:
            fields.add('birth_date')
        if best.gender != gender:
            fields.add('gender')
        return fields
    if roster.filter(birth_date=birth_date, gender=gender).exists():
        return {'name'}
    return set(IDENTITY_FIELDS)


def normalize_phone(value: str) -> str:
    return ''.join(ch for ch in value if ch.isdigit())


def find_respondent_by_identity(
    *,
    team: Team,
    name: str,
    birth_date,
    gender: str | None,
    lock: bool = False,
) -> Respondent | None:
    """Locate the respondent for (team, name, birth_date, gender).

    Respondents created before gender became part of the identity key may have no gender;
    such a row is adopted (and its gender filled in) so earlier submissions stay linked.
    """
    normalized_name = normalize_name(name)
    queryset = Respondent.objects.select_for_update() if lock else Respondent.objects
    base = queryset.filter(team=team, normalized_name__iexact=normalized_name, birth_date=birth_date)
    respondent = base.filter(gender=gender).order_by('pk').first() if gender else None
    if respondent is None:
        respondent = base.filter(gender__isnull=True).order_by('pk').first()
        if respondent is not None and gender:
            respondent.gender = gender
            respondent.save(update_fields=['gender', 'updated_at'])
    return respondent


def get_or_create_respondent(
    team: Team, *, name: str, birth_date, gender: str | None = None, phone: str = '',
) -> Respondent:
    """Find or create the respondent for (name, birth_date, gender). ``phone`` is contact
    information copied from the roster; it takes no part in identification."""
    normalized_name = normalize_name(name)
    respondent = find_respondent_by_identity(team=team, name=name, birth_date=birth_date, gender=gender)
    if respondent is None:
        try:
            with transaction.atomic():
                respondent = Respondent.objects.create(
                    team=team,
                    name=''.join(name.split()),
                    gender=gender or None,
                    normalized_name=normalized_name,
                    birth_date=birth_date,
                    phone=phone or '',
                    normalized_phone=normalize_phone(phone or ''),
                )
        except IntegrityError:
            respondent = Respondent.objects.get(
                team=team,
                normalized_name__iexact=normalized_name,
                birth_date=birth_date,
                gender=gender or None,
            )
    if phone and respondent.phone != phone:
        respondent.phone = phone
        respondent.normalized_phone = normalize_phone(phone)
        respondent.save(update_fields=['phone', 'normalized_phone', 'updated_at'])
    return respondent


def get_matching_participants(team: Team, *, name: str, birth_date, gender: str | None):
    return Participant.objects.filter(
        team=team,
        is_active=True,
        normalized_name__iexact=normalize_name(name),
        birth_date=birth_date,
        gender=gender,
    )


def is_gender_compatible(*, participants, input_gender: str | None) -> bool:
    participants_with_gender = participants.exclude(gender__isnull=True)
    if not participants_with_gender.exists() or not input_gender:
        return True
    return participants_with_gender.filter(gender=input_gender).exists()


def normalize_option_label_en(value) -> str:
    """Return an English display label without a mistakenly repeated score."""
    label = str(value or '').strip()
    if '|' not in label:
        return label
    candidate, separator, raw_score = label.rpartition('|')
    if separator and candidate.strip():
        try:
            int(raw_score.strip())
        except ValueError:
            return label
        return candidate.strip()
    return label


def normalize_option_entries(options_json) -> list[dict]:
    normalized = []
    for item in options_json or []:
        if isinstance(item, dict):
            label = str(item.get('label', '')).strip()
            label_en = normalize_option_label_en(item.get('label_en', ''))
            raw_score = item.get('score', 0)
        else:
            label = str(item).strip()
            label_en = ''
            raw_score = 0
        if not label:
            continue
        try:
            score = int(raw_score)
        except (TypeError, ValueError):
            score = 0
        normalized.append({'label': label, 'label_en': label_en, 'score': score})
    return normalized


def result_section_key(section_type: str) -> str | None:
    if section_type == SurveySection.SectionType.PHYSICAL:
        return 'body'
    if section_type == SurveySection.SectionType.MENTAL:
        return 'emotion'
    return None


def section_type_sort_key(section_type: str) -> int:
    return SECTION_TYPE_ORDER.get(section_type, 99)


def ordered_questions_for_form(survey_form: SurveyForm | None) -> list[SurveyQuestion]:
    if not survey_form:
        return []

    prefetched_sections = getattr(survey_form, '_prefetched_objects_cache', {}).get('sections')
    if prefetched_sections is not None:
        questions = [
            question
            for section in prefetched_sections
            for question in list(section.questions.all())
        ]
    else:
        questions = list(
            SurveyQuestion.objects
            .filter(section__form=survey_form)
            .select_related('section')
        )

    return sorted(
        questions,
        key=lambda question: (
            section_type_sort_key(question.section.section_type),
            question.section.display_order,
            question.display_order,
            question.id,
        ),
    )


def build_survey_form_snapshot(*, survey_form: SurveyForm) -> dict:
    questions_by_section_id: dict[int, list[SurveyQuestion]] = defaultdict(list)
    for question in ordered_questions_for_form(survey_form):
        questions_by_section_id[question.section_id].append(question)

    return {
        'title': survey_form.title,
        'title_en': survey_form.title_en,
        'sections': [
            {
                'id': section.id,
                'title': section.title,
                'title_en': section.title_en,
                'section_type': section.section_type,
                'display_order': section.display_order,
                'questions': [
                    {
                        'id': question.id,
                        'key': question.key,
                        'label': question.label,
                        'label_en': question.label_en,
                        'graph_label': question.graph_label,
                        'graph_label_en': question.graph_label_en,
                        'question_type': question.question_type,
                        'options': normalize_option_entries(question.options_json),
                        'required': question.is_required,
                        'display_order': question.display_order,
                    }
                    for question in questions_by_section_id.get(section.id, [])
                ],
            }
            for section in sorted(
                survey_form.sections.all(),
                key=lambda item: (
                    section_type_sort_key(item.section_type),
                    item.display_order,
                    item.id,
                ),
            )
        ],
    }


def graph_label_for_question(*, label: str, graph_label: str | None = None) -> str:
    return (graph_label or '').strip() or label.strip()


def _normalize_single_choice_to_100(*, options_json, selected_value) -> int:
    options = normalize_option_entries(options_json)
    if selected_value in (None, '') or not options:
        return 0

    score_map = {str(option['label']).strip(): option['score'] for option in options}
    raw_score = score_map.get(str(selected_value).strip())
    if raw_score is None:
        return 0

    scores = [option['score'] for option in options]
    min_score = min(scores)
    max_score = max(scores)
    if max_score == min_score:
        return 100

    normalized = round(((raw_score - min_score) / (max_score - min_score)) * 100)
    return max(0, min(100, normalized))


def score_status_label(
    score: int,
    *,
    submitted: bool,
    language: str = 'ko',
) -> str:
    if not submitted:
        return 'Not submitted' if language == 'en' else '설문 전'
    if language == 'en':
        if score >= 80:
            return 'Excellent'
        if score >= 60:
            return 'Normal'
        if score >= 40:
            return 'Caution'
        if score >= 20:
            return 'Warning'
        return 'Risk'
    if score >= 80:
        return '최상'
    if score >= 60:
        return '정상'
    if score >= 40:
        return '주의'
    if score >= 20:
        return '경고'
    return '위험'


def build_section_results_from_form(*, survey_form: SurveyForm, answers_json: dict | None = None, submitted: bool = False) -> list[dict]:
    questions = ordered_questions_for_form(survey_form)
    grouped: dict[str, list[dict]] = defaultdict(list)

    for question in questions:
        section_key = result_section_key(question.section.section_type)
        if not section_key or question.question_type != SurveyQuestion.QuestionType.SINGLE:
            continue
        selected_value = (answers_json or {}).get(question.key)
        grouped[section_key].append(
            {
                'question_id': question.id,
                'question_key': question.key,
                'graph_label': graph_label_for_question(label=question.label, graph_label=question.graph_label),
                'score': _normalize_single_choice_to_100(options_json=question.options_json, selected_value=selected_value) if submitted else 0,
            }
        )

    return _build_section_payloads_from_grouped(grouped=grouped, submitted=submitted)


def build_section_results_from_snapshot(
    *,
    form_snapshot_json: dict | None = None,
    answers_json: dict | None = None,
    submitted: bool = False,
    language: str = 'ko',
) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)

    snapshot_sections = sorted(
        (form_snapshot_json or {}).get('sections', []),
        key=lambda section: (
            section_type_sort_key(section.get('section_type')),
            int(section.get('display_order', 0) or 0),
            int(section.get('id', 0) or 0),
        ),
    )

    for section in snapshot_sections:
        section_key = result_section_key(section.get('section_type'))
        if not section_key:
            continue
        for question in sorted(
            section.get('questions', []),
            key=lambda item: (
                int(item.get('display_order', 0) or 0),
                int(item.get('id', 0) or 0),
            ),
        ):
            if question.get('question_type') != SurveyQuestion.QuestionType.SINGLE:
                continue
            selected_value = (answers_json or {}).get(question.get('key'))
            grouped[section_key].append(
                {
                    'question_id': question.get('id'),
                    'question_key': question.get('key'),
                    'graph_label': graph_label_for_question(
                        label=(
                            question.get('label_en') or question.get('label', '')
                            if language == 'en'
                            else question.get('label', '')
                        ),
                        graph_label=(
                            question.get('graph_label_en')
                            or question.get('graph_label')
                            if language == 'en'
                            else question.get('graph_label')
                        ),
                    ),
                    'score': _normalize_single_choice_to_100(options_json=question.get('options', []), selected_value=selected_value) if submitted else 0,
                }
            )

    return _build_section_payloads_from_grouped(
        grouped=grouped,
        submitted=submitted,
        language=language,
    )


def build_section_results_from_result_json(*, result_json: dict | None = None, submitted: bool = False) -> list[dict]:
    raw_sections = (result_json or {}).get('sections') or {}
    payload = []

    for section_key in ('body', 'emotion'):
        meta = SECTION_RESULT_META[section_key]
        raw_section = raw_sections.get(section_key) or {}
        raw_surveys = raw_section.get('surveys') or []
        surveys = [
            {
                'question_id': item.get('question_id'),
                'question_key': item.get('question_key'),
                'graph_label': graph_label_for_question(
                    label=item.get('graph_label') or item.get('question_key') or '',
                    graph_label=item.get('graph_label'),
                ),
                'score': int(item.get('score', 0) or 0),
            }
            for item in raw_surveys
        ]

        average_score = int(raw_section.get('average_score', 0) or 0)
        payload.append(
            {
                'key': section_key,
                'title': meta['title'],
                'accent': meta['accent'],
                'average_score': average_score,
                'status_label': raw_section.get('status_label') or score_status_label(average_score, submitted=submitted),
                'surveys': surveys,
            }
        )

    return payload


def build_section_detail_items_from_form(*, survey_form: SurveyForm | None, answers_json: dict | None = None, submitted: bool = False) -> dict[str, list[dict]]:
    if not survey_form or not submitted:
        return {'body': [], 'emotion': []}

    questions = ordered_questions_for_form(survey_form)
    return _build_section_detail_items(
        sections=[
            {
                'section_type': question.section.section_type,
                'questions': [
                    {
                        'id': question.id,
                        'key': question.key,
                        'label': question.label,
                        'question_type': question.question_type,
                        'options': question.options_json,
                    }
                ],
            }
            for question in questions
        ],
        answers_json=answers_json,
    )


def build_section_detail_items_from_snapshot(
    *,
    form_snapshot_json: dict | None = None,
    answers_json: dict | None = None,
    submitted: bool = False,
    language: str = 'ko',
) -> dict[str, list[dict]]:
    if not submitted:
        return {'body': [], 'emotion': []}
    return _build_section_detail_items(
        sections=(form_snapshot_json or {}).get('sections', []),
        answers_json=answers_json,
        language=language,
    )


def _build_section_payloads_from_grouped(
    *,
    grouped: dict[str, list[dict]],
    submitted: bool,
    language: str = 'ko',
) -> list[dict]:
    payload = []
    for section_key in ('body', 'emotion'):
        surveys = grouped.get(section_key, [])
        average_score = round(sum(item['score'] for item in surveys) / len(surveys)) if surveys and submitted else 0
        payload.append(
            {
                'key': section_key,
                'title': SECTION_RESULT_META[section_key]['title'],
                'accent': SECTION_RESULT_META[section_key]['accent'],
                'average_score': average_score,
                'status_label': score_status_label(
                    average_score,
                    submitted=submitted,
                    language=language,
                ),
                'surveys': surveys,
            }
        )
    return payload


def _display_answer_value(
    *,
    question_type: str,
    raw_answer,
    options,
    language: str = 'ko',
) -> str:
    missing_label = 'Not entered' if language == 'en' else '미입력'
    option_labels = {
        option['label']: option['label_en'] or option['label']
        for option in normalize_option_entries(options)
    }
    if question_type == SurveyQuestion.QuestionType.MULTI:
        if not raw_answer:
            return missing_label
        if isinstance(raw_answer, list):
            values = [str(item).strip() for item in raw_answer if str(item).strip()]
        else:
            values = [str(raw_answer).strip()] if str(raw_answer).strip() else []
        if language == 'en':
            values = [option_labels.get(value, value) for value in values]
        return ', '.join(values) if values else missing_label

    if raw_answer is None:
        return missing_label

    value = str(raw_answer).strip()
    if language == 'en':
        value = option_labels.get(value, value)
    return value or missing_label


def _build_section_detail_items(
    *,
    sections: list[dict],
    answers_json: dict | None = None,
    language: str = 'ko',
) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {'body': [], 'emotion': []}
    answers_json = answers_json or {}

    ordered_sections = sorted(
        sections,
        key=lambda section: (
            section_type_sort_key(section.get('section_type')),
            int(section.get('display_order', 0) or 0),
            int(section.get('id', 0) or 0),
        ),
    )

    for section in ordered_sections:
        section_key = result_section_key(section.get('section_type'))
        if not section_key:
            continue
        for question in sorted(
            section.get('questions', []),
            key=lambda item: (
                int(item.get('display_order', 0) or 0),
                int(item.get('id', 0) or 0),
            ),
        ):
            label = (
                str(question.get('label_en') or question.get('label', '')).strip()
                if language == 'en'
                else str(question.get('label', '')).strip()
            ) or str(question.get('key', '')).strip()
            if not label:
                continue
            raw_answer = answers_json.get(question.get('key'))
            answer_display = _display_answer_value(
                question_type=question.get('question_type', SurveyQuestion.QuestionType.TEXT),
                raw_answer=raw_answer,
                options=question.get('options', []),
                language=language,
            )
            grouped[section_key].append(
                {
                    'question_id': question.get('id'),
                    'question_key': question.get('key'),
                    'question_label': label,
                    'question_type': question.get('question_type'),
                    'answer_display': answer_display,
                    'has_content': answer_display not in {'미입력', 'Not entered'},
                }
            )

    return grouped


def total_question_count_for_form(*, survey_form: SurveyForm | None) -> int:
    if not survey_form:
        return 0
    prefetched_sections = getattr(survey_form, '_prefetched_objects_cache', {}).get('sections')
    if prefetched_sections is not None:
        return sum(len(list(section.questions.all())) for section in prefetched_sections)
    return SurveyQuestion.objects.filter(section__form=survey_form).count()


RESULT_SCORING_RULE = 'section_average_v2'
RESULT_SECTION_LABELS_KO = {'body': '신체', 'emotion': '정신'}
STATUS_LABEL_SEVERITY = {
    '최상': 0, '정상': 1, '주의': 2, '경고': 3, '위험': 4,
    'Excellent': 0, 'Normal': 1, 'Caution': 2, 'Warning': 3, 'Risk': 4,
}
RISK_LEVEL_BY_SEVERITY = {0: 'low', 1: 'low', 2: 'medium', 3: 'high', 4: 'high'}


def summarize_section_results(sections: list[dict] | None) -> dict:
    """Overall score, risk level and one-line summary derived from section results.

    Uses the same section averages and status labels the participant result page shows
    (0~100 per section, 최상/정상/주의/경고/위험). Risk follows the worst section:
    주의 → medium, 경고·위험 → high. Sections without scored questions are ignored.
    """
    scored = [section for section in (sections or []) if section.get('surveys')]
    if not scored:
        return {'score': 0, 'risk_level': 'unknown', 'summary': '채점할 문항이 없습니다.', 'worst_label': ''}
    worst_severity = max(STATUS_LABEL_SEVERITY.get(section.get('status_label'), 0) for section in scored)
    worst_label = next(
        section['status_label'] for section in scored
        if STATUS_LABEL_SEVERITY.get(section.get('status_label'), 0) == worst_severity
    )
    score = round(sum(int(section.get('average_score', 0) or 0) for section in scored) / len(scored))
    summary = ' · '.join(
        f"{RESULT_SECTION_LABELS_KO.get(section['key'], section['key'])} {section['status_label']}"
        for section in scored
    )
    return {
        'score': score,
        'risk_level': RISK_LEVEL_BY_SEVERITY[worst_severity],
        'summary': summary,
        'worst_label': worst_label,
    }


def build_result_payload(*, sections: list[dict] | None, submitted_at=None) -> dict:
    overview = summarize_section_results(sections)
    return {
        'status': 'submitted',
        'score': overview['score'],
        'risk_level': overview['risk_level'],
        'summary': overview['summary'],
        'scoring_rule': RESULT_SCORING_RULE,
        'submitted_at': (submitted_at or timezone.now()).isoformat(),
        'sections': {
            section['key']: {
                'average_score': section['average_score'],
                'status_label': section['status_label'],
                'surveys': [
                    {
                        'question_id': item.get('question_id'),
                        'question_key': item.get('question_key'),
                        'graph_label': item.get('graph_label'),
                        'score': item.get('score', 0),
                    }
                    for item in section.get('surveys', [])
                ],
            }
            for section in (sections or [])
        },
    }


def build_result_from_answers(*, survey_form: SurveyForm, answers_json: dict) -> dict:
    sections = build_section_results_from_form(survey_form=survey_form, answers_json=answers_json, submitted=True)
    return build_result_payload(sections=sections)


def rebuild_result_json(*, submission: SurveySubmission) -> dict | None:
    """Recompute result_json for a stored submission from its snapshot (or the active form).

    Returns None when neither a snapshot with sections nor an active form is available.
    """
    snapshot_sections = (submission.form_snapshot_json or {}).get('sections')
    if snapshot_sections:
        sections = build_section_results_from_snapshot(
            form_snapshot_json=submission.form_snapshot_json,
            answers_json=submission.answers_json,
            submitted=True,
        )
    else:
        # 유지보수 명령에서 쓰이므로 캐시를 거치지 않고 현재 활성 폼을 직접 조회한다.
        survey_form = active_survey_form_queryset(team=submission.team, day_no=submission.day_no).first()
        if survey_form is None:
            return None
        sections = build_section_results_from_form(
            survey_form=survey_form,
            answers_json=submission.answers_json,
            submitted=True,
        )
    return build_result_payload(sections=sections, submitted_at=submission.submitted_at)


def result_summary_from_result_json(result_json: dict | None) -> str:
    """Admin-facing one-liner. Derived from stored section labels so rows written by the
    retired temporary rule read correctly without a data migration."""
    sections = build_section_results_from_result_json(result_json=result_json, submitted=True)
    overview = summarize_section_results(sections)
    if overview['risk_level'] != 'unknown':
        return overview['summary']
    return (result_json or {}).get('summary') or (result_json or {}).get('status') or '-'


class DuplicateSubmissionError(Exception):
    pass


def get_active_team_by_code(team_code: str) -> Team | None:
    return Team.objects.filter(code=team_code.strip(), is_active=True).first()


def get_active_form(team: Team, day_no: int) -> SurveyForm | None:
    return active_survey_form_queryset(team=team, day_no=day_no).first()


def can_submit_today(team: Team, day_no: int) -> bool:
    return 1 <= day_no <= team.max_day_no


def submit_survey(
    *,
    team: Team,
    respondent: Respondent,
    day_no: int,
    answers_json: dict,
    survey_form: SurveyForm,
    form_version: int,
    form_snapshot_json: dict,
) -> SurveySubmission:
    try:
        with transaction.atomic():
            submission = SurveySubmission.objects.create(
                team=team,
                respondent=respondent,
                day_no=day_no,
                answers_json=answers_json,
                result_json=build_result_from_answers(survey_form=survey_form, answers_json=answers_json),
                form_version=form_version,
                form_snapshot_json=form_snapshot_json,
                is_locked=True,
                submitted_at=timezone.now(),
            )
            transaction.on_commit(
                lambda: invalidate_submission_cache(team=team, respondent=respondent, day_no=day_no)
            )
            return submission
    except IntegrityError as exc:
        raise DuplicateSubmissionError from exc
