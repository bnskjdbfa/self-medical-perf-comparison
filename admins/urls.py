from django.urls import path

from .views import (
    dashboard_api_view,
    dashboard_debug_view,
    manager_login_view,
    manager_logout_view,
    prototype_manager_view,
    respondent_list_api_view,
    respondent_list_debug_view,
    response_detail_api_view,
    response_detail_debug_view,
    two_factor_qr_view,
    two_factor_recovery_codes_view,
    two_factor_setup_view,
    two_factor_verify_view,
)

app_name = 'admins'

urlpatterns = [
    path('login/', manager_login_view, name='login'),
    path('logout/', manager_logout_view, name='logout'),
    path('2fa/setup/', two_factor_setup_view, name='two-factor-setup'),
    path('2fa/setup/qr/', two_factor_qr_view, name='two-factor-qr'),
    path('2fa/verify/', two_factor_verify_view, name='two-factor-verify'),
    path('2fa/recovery-codes/', two_factor_recovery_codes_view, name='two-factor-recovery-codes'),
    path('api/dashboard/', dashboard_api_view, name='dashboard'),
    path('api/respondents/', respondent_list_api_view, name='respondent-list'),
    path('api/responses/<int:submission_id>/', response_detail_api_view, name='response-detail'),
    path('debug/dashboard/', dashboard_debug_view, name='dashboard-debug'),
    path('debug/respondents/', respondent_list_debug_view, name='respondent-list-debug'),
    path('debug/responses/<int:submission_id>/', response_detail_debug_view, name='response-detail-debug'),
    path('prototype/manager/', prototype_manager_view, name='prototype-manager'),
]
