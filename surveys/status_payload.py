from __future__ import annotations


from django.urls import reverse

from teams.models import Respondent, Team

from .models import SurveyForm, SurveyQuestion, SurveySubmission
from .services import (
    build_section_detail_items_from_form,
    build_section_detail_items_from_snapshot,
    build_section_results_from_result_json,
    build_section_results_from_form,
    build_section_results_from_snapshot,
    get_active_survey_form,
    get_selected_submission,
    ordered_questions_for_form,
)


def build_result_page_payload(
    *,
    team: Team,
    respondent: Respondent,
    current_day_no: int,
    selected_day_no: int,
    query_string: str = '',
    back_url: str | None = None,
    is_detail_view: bool = False,
    page_language: str = 'ko',
) -> dict:
    selected_submission = get_selected_submission(team=team, respondent=respondent, day_no=selected_day_no)

    day_tabs = []

    for day_no in range(1, team.max_day_no + 1):
        route_name = 'surveys:result-detail' if is_detail_view else 'surveys:result'
        day_tabs.append(
            {
                'day_no': day_no,
                'label': f'Day {day_no}',
                'is_active': day_no == selected_day_no,
                'url': reverse(route_name, kwargs={'day_no': day_no}) + (f'?{query_string}' if query_string else ''),
            }
        )

    selected_form = None
    snapshot_sections = (selected_submission.form_snapshot_json or {}).get('sections') if selected_submission else None
    result_sections = (selected_submission.result_json or {}).get('sections') if selected_submission else None
    if not selected_submission or not snapshot_sections:
        selected_form = get_active_survey_form(team=team, day_no=selected_day_no)

    sections = _sections_for_selection(
        selected_submission=selected_submission,
        selected_form=selected_form,
        page_language=page_language,
    )
    sections_by_key = {section['key']: section for section in sections}
    detail_items_by_section = (
        _detail_items_for_selection(
            selected_submission=selected_submission,
            selected_form=selected_form,
            page_language=page_language,
        )
        if is_detail_view
        else {'body': [], 'emotion': []}
    )
    special_notes_by_section = (
        _special_notes_for_selection(
            selected_submission=selected_submission,
            selected_form=selected_form,
        )
        if not is_detail_view
        else {}
    )

    return {
        'selected_day_no': selected_day_no,
        'current_day_no': current_day_no,
        'day_tabs': day_tabs,
        'body_result': _result_card_payload(
            section=sections_by_key.get('body'),
            day_no=selected_day_no,
            anchor='body',
            query_string=query_string,
            detail_items=detail_items_by_section.get('body', []),
            has_special_notes=special_notes_by_section.get('body'),
            page_language=page_language,
        ),
        'emotion_result': _result_card_payload(
            section=sections_by_key.get('emotion'),
            day_no=selected_day_no,
            anchor='emotion',
            query_string=query_string,
            detail_items=detail_items_by_section.get('emotion', []),
            has_special_notes=special_notes_by_section.get('emotion'),
            page_language=page_language,
        ),
        'detail_back_url': back_url or reverse('surveys:preview-v2-home'),
        'query_string': query_string,
    }


def _sections_for_selection(
    *,
    selected_submission: SurveySubmission | None,
    selected_form: SurveyForm | None,
    page_language: str = 'ko',
) -> list[dict]:
    snapshot_sections = (selected_submission.form_snapshot_json or {}).get('sections') if selected_submission else None
    result_sections = (selected_submission.result_json or {}).get('sections') if selected_submission else None

    if selected_submission:
        if page_language == 'en' and snapshot_sections:
            return build_section_results_from_snapshot(
                form_snapshot_json=selected_submission.form_snapshot_json,
                answers_json=selected_submission.answers_json,
                submitted=True,
                language='en',
            )
        return (
            build_section_results_from_result_json(
                result_json=selected_submission.result_json,
                submitted=True,
            )
            if result_sections
            else (
                build_section_results_from_snapshot(
                    form_snapshot_json=selected_submission.form_snapshot_json,
                    answers_json=selected_submission.answers_json,
                    submitted=True,
                )
                if snapshot_sections
                else build_section_results_from_form(
                    survey_form=selected_form,
                    answers_json=selected_submission.answers_json,
                    submitted=True,
                )
            )
        )

    if selected_form:
        return build_section_results_from_form(
            survey_form=selected_form,
            answers_json={},
            submitted=False,
        )

    return [
        {'key': 'body', 'title': 'Body', 'accent': 'lime', 'average_score': 0, 'status_label': '제출 전', 'surveys': []},
        {'key': 'emotion', 'title': 'Emotion', 'accent': 'violet', 'average_score': 0, 'status_label': '제출 전', 'surveys': []},
    ]


def _result_card_payload(
    *,
    section: dict | None,
    day_no: int,
    anchor: str,
    query_string: str = '',
    detail_items: list[dict] | None = None,
    has_special_notes: bool | None = None,
    page_language: str = 'ko',
) -> dict:
    section = section or {
        'key': anchor,
        'title': 'Body' if anchor == 'body' else 'Emotion',
        'accent': 'lime' if anchor == 'body' else 'violet',
        'average_score': 0,
        'status_label': '제출 전',
        'surveys': [],
    }
    score = int(section.get('average_score', 0) or 0)
    segment_count = 30
    filled_segments = max(0, min(segment_count, round(score / 100 * segment_count)))
    status_label = section.get('status_label') or '제출 전'
    if status_label == '설문 전':
        status_label = '제출 전'
    if page_language == 'en':
        status_label = {
            '제출 전': 'Not submitted',
            '최상': 'Excellent',
            '정상': 'Normal',
            '주의': 'Caution',
            '경고': 'Warning',
            '위험': 'Risk',
        }.get(status_label, status_label)
    detail_items = detail_items or []
    score_by_question_key = {
        str(item.get('question_key')): max(0, min(100, int(item.get('score', 0) or 0)))
        for item in section.get('surveys', [])
        if item.get('question_key')
    }
    detail_items = [
        {
            **item,
            'has_score': str(item.get('question_key')) in score_by_question_key,
            'score': score_by_question_key.get(str(item.get('question_key')), 0),
        }
        for item in detail_items
    ]
    if has_special_notes is None:
        has_special_notes = any(
            item.get('question_type') == SurveyQuestion.QuestionType.TEXT and item.get('has_content')
            for item in detail_items
        )
    return {
        'key': section['key'],
        'title': section['title'],
        'accent': section['accent'],
        'status_label': status_label,
        'score': score,
        'score_percent': score,
        'segments': range(segment_count),
        'filled_segments': filled_segments,
        'surveys': section.get('surveys', []),
        'detail_items': detail_items,
        'special_notes_label': (
            'Additional notes provided'
            if has_special_notes
            else 'No additional notes'
        ) if page_language == 'en' else (
            '기타 특이사항 있음'
            if has_special_notes
            else '기타 특이사항 없음'
        ),
        'detail_url': (
            reverse('surveys:result-detail', kwargs={'day_no': day_no})
            + (f'?{query_string}' if query_string else '')
            + f'#{anchor}'
        ),
    }


def _detail_items_for_selection(
    *,
    selected_submission: SurveySubmission | None,
    selected_form: SurveyForm | None,
    page_language: str = 'ko',
) -> dict[str, list[dict]]:
    snapshot_sections = (selected_submission.form_snapshot_json or {}).get('sections') if selected_submission else None

    if selected_submission:
        if snapshot_sections:
            return build_section_detail_items_from_snapshot(
                form_snapshot_json=selected_submission.form_snapshot_json,
                answers_json=selected_submission.answers_json,
                submitted=True,
                language=page_language,
            )
        return build_section_detail_items_from_form(
            survey_form=selected_form,
            answers_json=selected_submission.answers_json,
            submitted=True,
        )

    return {'body': [], 'emotion': []}


def _special_notes_for_selection(*, selected_submission: SurveySubmission | None, selected_form: SurveyForm | None) -> dict[str, bool]:
    if not selected_submission:
        return {'body': False, 'emotion': False}

    snapshot_sections = (selected_submission.form_snapshot_json or {}).get('sections')
    if snapshot_sections:
        return _special_notes_from_sections(
            sections=snapshot_sections,
            answers_json=selected_submission.answers_json,
        )

    if not selected_form:
        return {'body': False, 'emotion': False}

    notes = {'body': False, 'emotion': False}
    answers_json = selected_submission.answers_json or {}
    for question in ordered_questions_for_form(selected_form):
        if question.question_type != SurveyQuestion.QuestionType.TEXT:
            continue
        section_key = 'body' if question.section.section_type == 'physical' else 'emotion'
        value = answers_json.get(question.key)
        notes[section_key] = notes[section_key] or bool(str(value or '').strip())
    return notes


def _special_notes_from_sections(*, sections: list[dict], answers_json: dict | None) -> dict[str, bool]:
    notes = {'body': False, 'emotion': False}
    answers_json = answers_json or {}
    for section in sections:
        section_type = section.get('section_type')
        if section_type == 'physical':
            section_key = 'body'
        elif section_type == 'mental':
            section_key = 'emotion'
        else:
            continue
        for question in section.get('questions', []):
            if question.get('question_type') != SurveyQuestion.QuestionType.TEXT:
                continue
            value = answers_json.get(question.get('key'))
            if str(value or '').strip():
                notes[section_key] = True
                break
    return notes
