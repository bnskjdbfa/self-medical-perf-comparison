from django.urls import path
from django.views.generic import RedirectView

from .views import (
    day_survey_view,
    exit_survey_view,
    preview_v2_code_view,
    preview_v2_completion_view,
    preview_v2_document_view,
    preview_v2_documents_next_view,
    preview_v2_exit_survey_start_view,
    preview_v2_health_start_view,
    preview_v2_home_view,
    preview_v2_identity_view,
    preview_v2_privacy_consent_view,
    preview_v2_reflection_view,
    preview_v2_reset_login_view,
    result_detail_view,
    result_view,
)

app_name = 'surveys'

urlpatterns = [
    # v1 참가자 진입(코드 입력 → 인적사항 → 상태 화면)은 폐기됐다.
    # 옛 링크와 북마크는 v2 코드 입력 화면으로 보낸다.
    path('', RedirectView.as_view(pattern_name='surveys:preview-v2-code'), name='entry'),
    # v2가 사용하는 공유 화면: 자가검진 진행·제출, 종료 설문, 결과 보기
    path('day/<int:day_no>/', day_survey_view, name='day-survey'),
    path('exit/', exit_survey_view, name='exit-survey'),
    path('day/<int:day_no>/result/', result_view, name='result'),
    path('day/<int:day_no>/result/detail/', result_detail_view, name='result-detail'),
    path('preview/v2/code/', preview_v2_code_view, name='preview-v2-code'),
    path('preview/v2/identity/', preview_v2_identity_view, name='preview-v2-identity'),
    path(
        'preview/v2/privacy-consent/',
        preview_v2_privacy_consent_view,
        name='preview-v2-privacy-consent',
    ),
    path('preview/v2/home/', preview_v2_home_view, name='preview-v2-home'),
    path(
        'preview/v2/login/reset/',
        preview_v2_reset_login_view,
        name='preview-v2-reset-login',
    ),
    path(
        'preview/v2/health/start/',
        preview_v2_health_start_view,
        name='preview-v2-health-start',
    ),
    path(
        'preview/v2/exit/start/',
        preview_v2_exit_survey_start_view,
        name='preview-v2-exit-start',
    ),
    path('preview/v2/reflection/', preview_v2_reflection_view, name='preview-v2-reflection'),
    path(
        'preview/v2/completed/<str:completion_type>/',
        preview_v2_completion_view,
        name='preview-v2-completion',
    ),
    path('preview/v2/documents/next/', preview_v2_documents_next_view, name='preview-v2-documents-next'),
    path('preview/v2/documents/<str:document_type>/', preview_v2_document_view, name='preview-v2-document'),
]
