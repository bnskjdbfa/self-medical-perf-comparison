from __future__ import annotations

from copy import copy
from types import MethodType

from django.contrib.admin import AdminSite


SURVEYS_ADMIN_GROUPS = (
    (
        '동의서 관리',
        'surveys_consents',
        {
            'DocumentSubmission',
            'RequiredDocumentConsentBundle',
        },
    ),
    (
        '설문 및 검진 관리',
        'surveys_questionnaires_and_health_checks',
        {
            'SurveyForm',
            'SurveySection',
            'SurveyQuestion',
            'SurveySubmission',
            'CommonQuestionSet',
            'CommonQuestionSection',
            'CommonQuestion',
            'CommonQuestionSetDeployment',
            'ExitSurveySubmission',
        },
    ),
    (
        '소감문 관리',
        'surveys_reflections',
        {
            'ReflectionSubmission',
        },
    ),
    (
        '운영 기록',
        'surveys_operations',
        {
            'AdminDownloadAudit',
        },
    ),
)


def split_surveys_admin_app(app: dict) -> list[dict]:
    models_by_name = {
        model['object_name']: model
        for model in app.get('models', ())
    }
    grouped_apps = []
    for group_name, group_label, model_names in SURVEYS_ADMIN_GROUPS:
        models = [
            {
                **model,
                'name': (
                    '동의서 제출자 현황'
                    if model['object_name'] == 'RequiredDocumentConsentBundle'
                    else '문서별 동의 내역'
                    if model['object_name'] == 'DocumentSubmission'
                    else '검진 제출'
                    if model['object_name'] == 'SurveySubmission'
                    else '설문 제출'
                    if model['object_name'] == 'ExitSurveySubmission'
                    else model['name']
                ),
            }
            for model in app.get('models', ())
            if model['object_name'] in model_names
        ]
        if not models:
            continue
        grouped_app = copy(app)
        grouped_app.update(
            {
                'name': group_name,
                'app_label': group_label,
                'models': models,
            }
        )
        grouped_apps.append(grouped_app)

    assigned_names = {
        model_name
        for _, _, model_names in SURVEYS_ADMIN_GROUPS
        for model_name in model_names
    }
    unassigned_models = [
        model
        for model_name, model in models_by_name.items()
        if model_name not in assigned_names
    ]
    if unassigned_models:
        fallback_app = copy(app)
        fallback_app['models'] = unassigned_models
        grouped_apps.append(fallback_app)
    return grouped_apps


def grouped_get_app_list(self, request, app_label=None):
    app_list = AdminSite.get_app_list(self, request, app_label)
    grouped_list = []
    for app in app_list:
        if app.get('app_label') == 'surveys':
            grouped_list.extend(split_surveys_admin_app(app))
        else:
            grouped_list.append(app)
    return grouped_list


def configure_admin_navigation(site: AdminSite) -> None:
    if getattr(site, '_v2_grouped_navigation', False):
        return
    site.get_app_list = MethodType(grouped_get_app_list, site)
    site._v2_grouped_navigation = True
