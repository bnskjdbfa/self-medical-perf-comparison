from __future__ import annotations

import copy
import re
from collections.abc import Iterable

from django.db import transaction
from django.db.models import Max

from teams.models import Team

from .models import (
    CommonQuestion,
    CommonQuestionSection,
    CommonQuestionSet,
    CommonQuestionSetDeployment,
    SurveyForm,
    SurveyQuestion,
    SurveySection,
    TeamCommonQuestionExclusion,
)
from .services import (
    invalidate_survey_form_cache,
    normalize_option_entries,
)


def _canonical_options(options_json) -> list[dict]:
    return [
        {
            'label': option['label'],
            'label_en': option['label_en'],
            'score': option['score'],
        }
        for option in normalize_option_entries(options_json)
    ]


def _common_definition(
    *,
    question_set: CommonQuestionSet,
    excluded_keys: set[str],
) -> dict:
    return {
        'title': question_set.title,
        'title_en': question_set.title_en,
        'is_active': question_set.is_active,
        'sections': [
            {
                'title': section.title,
                'title_en': section.title_en,
                'section_type': section.section_type,
                'display_order': section.display_order,
                'questions': [
                    {
                        'key': question.key,
                        'label': question.label,
                        'label_en': question.label_en,
                        'graph_label': question.graph_label,
                        'graph_label_en': question.graph_label_en,
                        'question_type': question.question_type,
                        'options': _canonical_options(question.options_json),
                        'is_required': question.is_required,
                        'display_order': question.display_order,
                    }
                    for question in section.questions.all()
                    if question.key not in excluded_keys
                ],
            }
            for section in question_set.sections.all()
        ],
    }


def _deployed_definition(form: SurveyForm) -> dict:
    return {
        'title': form.title,
        'title_en': form.title_en,
        'is_active': form.is_active,
        'sections': [
            {
                'title': section.title,
                'title_en': section.title_en,
                'section_type': section.section_type,
                'display_order': section.display_order,
                'questions': [
                    {
                        'key': question.key,
                        'label': question.label,
                        'label_en': question.label_en,
                        'graph_label': question.graph_label,
                        'graph_label_en': question.graph_label_en,
                        'question_type': question.question_type,
                        'options': [
                            {
                                'label': (
                                    str(option.get('label', '')).strip()
                                    if isinstance(option, dict)
                                    else str(option).strip()
                                ),
                                'label_en': (
                                    str(option.get('label_en', '')).strip()
                                    if isinstance(option, dict)
                                    else ''
                                ),
                                'score': (
                                    option.get('score', 0)
                                    if isinstance(option, dict)
                                    else 0
                                ),
                            }
                            for option in (question.options_json or [])
                        ],
                        'is_required': question.is_required,
                        'display_order': question.display_order,
                    }
                    for question in section.questions.all()
                ],
            }
            for section in form.sections.all()
        ],
    }


def deploy_common_question_set_to_teams(
    *,
    question_set: CommonQuestionSet,
    teams: Iterable[Team],
) -> list[CommonQuestionSetDeployment]:
    results = []
    for team in teams:
        try:
            result = deploy_common_question_set_to_team(
                question_set=question_set,
                team=team,
            )
        except Exception as exc:  # pragma: no cover - defensive audit trail
            result = CommonQuestionSetDeployment.objects.create(
                question_set=question_set,
                team=team,
                status=CommonQuestionSetDeployment.Status.FAILED,
                message=str(exc),
            )
        results.append(result)
    return results


def deploy_common_question_set_to_team(
    *,
    question_set: CommonQuestionSet,
    team: Team,
) -> CommonQuestionSetDeployment:
    target_form_type = (
        SurveyForm.FormType.EXIT_SURVEY
        if question_set.form_type == CommonQuestionSet.FormType.EXIT_SURVEY
        else SurveyForm.FormType.DAILY_HEALTH
    )
    if (
        target_form_type == SurveyForm.FormType.DAILY_HEALTH
        and question_set.day_no > team.max_day_no
    ):
        return CommonQuestionSetDeployment.objects.create(
            question_set=question_set,
            team=team,
            status=CommonQuestionSetDeployment.Status.SKIPPED,
            message='팀 max_day_no 범위 초과',
        )

    with transaction.atomic():
        active_forms = list(
            SurveyForm.objects.select_for_update()
            .filter(
                team=team,
                day_no=question_set.day_no,
                form_type=target_form_type,
                is_active=True,
            )
            .prefetch_related('sections__questions')
            .order_by('-version', '-id')
        )
        excluded_keys = set(
            TeamCommonQuestionExclusion.objects.filter(
                team=team,
                question_set=question_set,
            ).values_list('question_key', flat=True)
        )
        desired_definition = _common_definition(
            question_set=question_set,
            excluded_keys=excluded_keys,
        )
        if not any(section['questions'] for section in desired_definition['sections']):
            return CommonQuestionSetDeployment.objects.create(
                question_set=question_set,
                team=team,
                status=CommonQuestionSetDeployment.Status.SKIPPED,
                message='문항이 없어 배포하지 않음',
            )
        if (
            len(active_forms) == 1
            and _deployed_definition(active_forms[0]) == desired_definition
        ):
            return CommonQuestionSetDeployment.objects.create(
                question_set=question_set,
                team=team,
                survey_form=active_forms[0],
                status=CommonQuestionSetDeployment.Status.SKIPPED,
                message='현재 활성 설문이 공통 설문과 동일하여 변경 없음',
            )

        max_version = (
            SurveyForm.objects.filter(
                team=team,
                day_no=question_set.day_no,
            ).aggregate(v=Max('version')).get('v')
            or 0
        )
        form = SurveyForm.objects.create(
            team=team,
            day_no=question_set.day_no,
            title=question_set.title,
            title_en=question_set.title_en,
            version=max_version + 1,
            schema_json={},
            form_type=target_form_type,
            is_anonymous=False,
            is_active=question_set.is_active,
        )
        for section in question_set.sections.all().order_by('display_order', 'id'):
            new_section = SurveySection.objects.create(
                form=form,
                title=section.title,
                title_en=section.title_en,
                section_type=section.section_type,
                display_order=section.display_order,
            )
            for question in section.questions.all().order_by('display_order', 'id'):
                if question.key in excluded_keys:
                    continue
                SurveyQuestion.objects.create(
                    section=new_section,
                    key=question.key,
                    label=question.label,
                    label_en=question.label_en,
                    graph_label=question.graph_label,
                    graph_label_en=question.graph_label_en,
                    question_type=question.question_type,
                    options_json=_canonical_options(question.options_json),
                    is_required=question.is_required,
                    display_order=question.display_order,
                )
        if active_forms:
            SurveyForm.objects.filter(
                pk__in=[active_form.pk for active_form in active_forms]
            ).update(is_active=False)
        invalidate_survey_form_cache(
            team=team,
            day_no=question_set.day_no,
        )
        return CommonQuestionSetDeployment.objects.create(
            question_set=question_set,
            team=team,
            survey_form=form,
            status=CommonQuestionSetDeployment.Status.CREATED,
            message=(
                '공통 설문 변경사항으로 새 버전 생성'
                if active_forms
                else '선택 대상 팀에 신규 설문 생성'
            ),
        )


DAY_LABEL_PATTERN = re.compile(r'(\d+)\s*일차')
QUESTION_SET_NAME_MAX_LENGTH = CommonQuestionSet._meta.get_field('name').max_length


def build_cloned_question_set_name(*, source_name: str, day_no: int) -> str:
    """Derive a unique name for a clone of ``source_name`` that targets ``day_no``.

    ``건강_1일차_공통`` becomes ``건강_3일차_공통``. Names without a day label get a
    ``(3일차)`` suffix. While the name is already taken, a numeric suffix is added.
    """
    source_name = source_name.strip()
    base_name, replaced = DAY_LABEL_PATTERN.subn(f'{day_no}일차', source_name, count=1)
    if not replaced:
        base_name = f'{source_name} ({day_no}일차)'
    base_name = base_name[:QUESTION_SET_NAME_MAX_LENGTH]

    candidate = base_name
    suffix = 2
    while CommonQuestionSet.objects.filter(name=candidate).exists():
        tail = f' ({suffix})'
        candidate = f'{base_name[:QUESTION_SET_NAME_MAX_LENGTH - len(tail)]}{tail}'
        suffix += 1
    return candidate


def clone_common_question_set(
    *,
    source: CommonQuestionSet,
    day_no: int,
    name: str | None = None,
) -> CommonQuestionSet:
    """Copy a common question set (sections, questions, team exclusions) to another day.

    The clone is fully independent from the source and is not deployed to any team.
    """
    if day_no < 1:
        raise ValueError('복제할 일차는 1 이상이어야 합니다.')

    with transaction.atomic():
        clone = CommonQuestionSet.objects.create(
            name=name or build_cloned_question_set_name(source_name=source.name, day_no=day_no),
            title=source.title,
            title_en=source.title_en,
            form_type=source.form_type,
            day_no=day_no,
            is_active=source.is_active,
            description=source.description,
        )
        for section in source.sections.all().order_by('display_order', 'id'):
            cloned_section = CommonQuestionSection.objects.create(
                question_set=clone,
                title=section.title,
                title_en=section.title_en,
                section_type=section.section_type,
                display_order=section.display_order,
            )
            for question in section.questions.all().order_by('display_order', 'id'):
                CommonQuestion.objects.create(
                    question_set=clone,
                    section=cloned_section,
                    key=question.key,
                    label=question.label,
                    label_en=question.label_en,
                    graph_label=question.graph_label,
                    graph_label_en=question.graph_label_en,
                    question_type=question.question_type,
                    options_json=copy.deepcopy(question.options_json or []),
                    is_required=question.is_required,
                    display_order=question.display_order,
                )
        for exclusion in TeamCommonQuestionExclusion.objects.filter(question_set=source).order_by('id'):
            TeamCommonQuestionExclusion.objects.create(
                team=exclusion.team,
                question_set=clone,
                question_key=exclusion.question_key,
            )
    return clone


def clone_common_question_set_to_days(
    *,
    source: CommonQuestionSet,
    day_numbers: Iterable[int],
) -> list[CommonQuestionSet]:
    """Create one independent clone per day. Either every clone is created or none is."""
    with transaction.atomic():
        return [
            clone_common_question_set(source=source, day_no=day_no)
            for day_no in day_numbers
        ]
