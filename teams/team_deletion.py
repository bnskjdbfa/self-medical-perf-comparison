from __future__ import annotations

from collections import OrderedDict

from django.db import transaction

from admins.models import AdminTeamPermission
from surveys.models import (
    AnonymousSurveySubmission,
    CommonQuestionSetDeployment,
    DocumentSubmission,
    DocumentTemplate,
    ExitSurveySubmission,
    ReflectionSubmission,
    RequiredDocumentConsentBundle,
    SurveyAnswer,
    SurveyForm,
    SurveyQuestion,
    SurveySection,
    SurveySubmission,
    TeamCommonQuestionExclusion,
    TeamFeatureSetting,
)

from .models import Participant, Respondent, Team


TEAM_PURGE_LABELS = OrderedDict(
    (
        ('participants', '대상자 명단'),
        ('respondents', '응답자'),
        ('survey_forms', '팀별 설문 폼'),
        ('survey_sections', '팀별 설문 섹션'),
        ('survey_questions', '팀별 설문 문항'),
        ('survey_submissions', '검진 제출'),
        ('survey_answers', '검진 답변'),
        ('exit_survey_submissions', '종료 설문 제출'),
        ('anonymous_survey_submissions', '익명 설문 제출'),
        ('document_templates', '문서 템플릿'),
        ('document_bundles', '전자 동의 묶음'),
        ('document_submissions', '문서 제출'),
        ('reflection_submissions', '소감문 제출'),
        ('deployments', '공통 질문 세트 배포 이력'),
        ('question_exclusions', '팀별 공통 문항 제외'),
        ('feature_settings', '팀 기능 설정'),
        ('admin_permissions', '관리자 팀 권한'),
    )
)


def get_team_purge_counts(team: Team) -> OrderedDict[str, dict[str, int | str]]:
    survey_forms = SurveyForm.objects.filter(team=team)
    survey_sections = SurveySection.objects.filter(form__team=team)
    survey_questions = SurveyQuestion.objects.filter(section__form__team=team)
    counts = {
        'participants': Participant.objects.filter(team=team).count(),
        'respondents': Respondent.objects.filter(team=team).count(),
        'survey_forms': survey_forms.count(),
        'survey_sections': survey_sections.count(),
        'survey_questions': survey_questions.count(),
        'survey_submissions': SurveySubmission.objects.filter(team=team).count(),
        'survey_answers': SurveyAnswer.objects.filter(submission__team=team).count(),
        'exit_survey_submissions': ExitSurveySubmission.objects.filter(team=team).count(),
        'anonymous_survey_submissions': AnonymousSurveySubmission.objects.filter(team=team).count(),
        'document_templates': DocumentTemplate.objects.filter(team=team).count(),
        'document_bundles': RequiredDocumentConsentBundle.objects.filter(team=team).count(),
        'document_submissions': DocumentSubmission.objects.filter(team=team).count(),
        'reflection_submissions': ReflectionSubmission.objects.filter(team=team).count(),
        'deployments': CommonQuestionSetDeployment.objects.filter(team=team).count(),
        'question_exclusions': TeamCommonQuestionExclusion.objects.filter(team=team).count(),
        'feature_settings': TeamFeatureSetting.objects.filter(team=team).count(),
        'admin_permissions': AdminTeamPermission.objects.filter(team=team).count(),
    }
    return OrderedDict(
        (
            key,
            {
                'label': label,
                'count': counts[key],
            },
        )
        for key, label in TEAM_PURGE_LABELS.items()
    )


def purge_team(*, team_id: int) -> dict[str, int]:
    with transaction.atomic():
        team = Team.objects.select_for_update().get(pk=team_id)
        counts = get_team_purge_counts(team)
        reflection_image_name = team.reflection_image.name if team.reflection_image else ''
        reflection_image_storage = team.reflection_image.storage if team.reflection_image else None

        SurveySubmission.objects.filter(team=team).delete()
        ExitSurveySubmission.objects.filter(team=team).delete()
        AnonymousSurveySubmission.objects.filter(team=team).delete()
        DocumentSubmission.objects.filter(team=team).delete()
        ReflectionSubmission.objects.filter(team=team).delete()
        RequiredDocumentConsentBundle.objects.filter(team=team).delete()
        CommonQuestionSetDeployment.objects.filter(team=team).delete()
        DocumentTemplate.objects.filter(team=team).delete()
        SurveyForm.objects.filter(team=team).delete()
        Respondent.objects.filter(team=team).delete()
        TeamCommonQuestionExclusion.objects.filter(team=team).delete()
        TeamFeatureSetting.objects.filter(team=team).delete()
        AdminTeamPermission.objects.filter(team=team).delete()
        Participant.objects.filter(team=team).delete()
        team.delete()

        if reflection_image_name and reflection_image_storage:
            transaction.on_commit(
                lambda: reflection_image_storage.delete(reflection_image_name)
            )

    return {
        key: int(item['count'])
        for key, item in counts.items()
    }
