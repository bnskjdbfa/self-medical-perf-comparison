import csv
import io
import os
import secrets
from datetime import date, datetime
from urllib.parse import quote

from django.contrib import admin
from django import forms
from django.contrib import messages
from django.contrib.admin.utils import NestedObjects, unquote
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db import router
from django.db.models import Prefetch, Q
from django.http import Http404, HttpResponse
from django.template.response import TemplateResponse
from django.utils.html import format_html
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils import timezone

from .models import GenderChoices, GuardianConsentEvent, Participant, ParticipantUploadAudit, Respondent, Team
from .age_policy import (
    is_eligible_participant_age,
    participant_has_required_guardian_consent,
    underage_participant_filter,
    unverified_underage_participants,
)
from .guardian_consent import (
    GuardianConsentNotActive,
    mark_guardian_consent_verified,
    record_guardian_consent_event,
    revoke_guardian_consent,
)
from .participant_upload import (  # noqa: F401 - re-exported for existing imports/tests
    UPLOAD_COLUMN_ALIASES,
    UploadRowPlan,
    apply_participant_upload,
    error_rows_csv,
    iter_upload_rows,
    missing_required_columns,
    normalize_upload_boolean,
    normalize_upload_gender,
    normalize_upload_header,
    normalize_upload_phone,
    normalize_upload_row,
    parse_birth_date,
    plan_participant_upload,
    summarize_plans,
)
from surveys.services import normalize_name, normalize_phone
from surveys.models import SurveySubmission, TeamFeatureSetting
from surveys.models import (
    ConsentExportAudit,
    DocumentSubmission,
    ExitSurveySubmission,
    ReflectionDownloadAudit,
    ReflectionSubmission,
    RequiredDocumentConsentBundle,
)
from surveys.reflection_exports import ReflectionExportError, build_team_reflection_zip
from surveys.download_audits import record_admin_download
from surveys.consent_exports import (
    ConsentExportError,
    build_consent_workbook,
    build_consent_zip,
    consent_excel_filename,
)
from admins.models import AdminTeamPermission
from .team_deletion import get_team_purge_counts, purge_team
from .respondent_deletion import purge_respondents


class ReadOnlyAdminMixin:
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]


def disable_related_widget(field):
    widget = getattr(field, 'widget', None)
    if widget is None:
        return
    widget.can_add_related = False
    widget.can_change_related = False
    widget.can_delete_related = False
    widget.can_view_related = False


class SuperuserOnlyAdminMixin:
    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


def generate_team_code() -> str:
    return f"T{secrets.token_hex(3).upper()}"


def generate_manager_entry_code() -> str:
    return f"MGR-{secrets.token_hex(4).upper()}"


class TeamAdminForm(forms.ModelForm):
    confirm_start_date_impact = forms.BooleanField(
        required=False,
        label='시작일 변경 영향을 확인했습니다',
        help_text=(
            '만 14세 미만 판정은 팀 시작일 기준입니다. 시작일을 바꿔 법정대리인 동의 미확인 참가자가 '
            '생기는 경우 이 항목을 체크해야 저장됩니다.'
        ),
    )

    class Meta:
        model = Team
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['code'].required = False
        self.fields['manager_entry_code'].required = False
        self.fields['code'].help_text = '비워두면 자동 생성됩니다.'
        self.fields['manager_entry_code'].help_text = '비워두면 자동 생성됩니다.'
        self.affected_underage_count = 0

    def clean(self):
        cleaned = super().clean()
        new_start_date = cleaned.get('start_date')
        if self.instance.pk and 'start_date' in self.changed_data and new_start_date:
            affected = list(unverified_underage_participants(self.instance, reference_date=new_start_date))
            self.affected_underage_count = len(affected)
            if affected and not cleaned.get('confirm_start_date_impact'):
                names = ', '.join(participant.name for participant in affected[:10])
                if len(affected) > 10:
                    names += f' 외 {len(affected) - 10}명'
                self.add_error(
                    'confirm_start_date_impact',
                    f'시작일을 {new_start_date:%Y-%m-%d}로 바꾸면 만 14세 미만이 되는데 법정대리인 동의가 '
                    f'확인되지 않은 참가자가 {len(affected)}명 있습니다: {names}. 이들은 동의 확인 전까지 '
                    '로그인할 수 없습니다. 그래도 변경하려면 체크한 뒤 저장하세요.',
                )
        return cleaned
        self.fields['reflection_image'].help_text = (
            '선택 사항 · JPG, PNG, WebP · 최대 5MB. '
            '소감문 PDF에는 올린 이미지가 그대로 들어갑니다(투명도를 따로 낮추지 않음). '
            '미리 연하게 만든 이미지를 올려 주세요.'
        )


class TeamPurgeConfirmationForm(forms.Form):
    team_code = forms.CharField(label='팀 코드 재입력', max_length=50)
    confirm_purge = forms.BooleanField(
        label='연관된 참가자·응답자·제출·팀별 설문이 복구 불가능하게 삭제됨을 확인합니다.',
    )

    def __init__(self, *args, team: Team, **kwargs):
        super().__init__(*args, **kwargs)
        self.team = team
        self.fields['team_code'].help_text = f'확인을 위해 “{team.code}”를 입력하세요.'

    def clean_team_code(self):
        value = (self.cleaned_data.get('team_code') or '').strip()
        if value != self.team.code:
            raise forms.ValidationError('팀 코드가 일치하지 않습니다.')
        return value


class TeamFeatureSettingInline(admin.StackedInline):
    model = TeamFeatureSetting
    extra = 1
    max_num = 1
    can_delete = True
    verbose_name = '필수 제출 항목 설정'
    verbose_name_plural = '필수 제출 항목 설정 (선택하지 않으면 바로 홈으로 이동합니다)'
    fields = (
        'enable_privacy_consent',
        'enable_third_party_consent',
        'enable_safety_pledge',
    )


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    form = TeamAdminForm
    inlines = (TeamFeatureSettingInline,)
    list_display = (
        "name",
        "organization_name",
        "organization_name_en",
        "code",
        "manager_entry_code",
        "is_active",
        "start_date",
        "max_day_no",
        "submission_status_link",
    )
    list_filter = ("is_active", "start_date")
    search_fields = (
        "name",
        "organization_name",
        "organization_name_en",
        "code",
        "manager_entry_code",
    )
    fields = (
        'name',
        'organization_name',
        'organization_name_en',
        'code',
        'manager_entry_code',
        'is_active',
        'start_date',
        'confirm_start_date_impact',
        'max_day_no',
        'reflection_image',
    )
    delete_confirmation_template = 'admin/teams/team/delete_confirmation.html'
    change_form_template = 'admin/teams/team/change_form.html'

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                '<path:object_id>/reflection-zip/',
                self.admin_site.admin_view(self.reflection_zip_view),
                name='teams_team_reflection_zip',
            ),
            path(
                '<path:object_id>/consent-excel/',
                self.admin_site.admin_view(self.consent_excel_view),
                name='teams_team_consent_excel',
            ),
            path(
                '<path:object_id>/consent-zip/',
                self.admin_site.admin_view(self.consent_zip_view),
                name='teams_team_consent_zip',
            ),
        ]
        return custom_urls + urls

    def _can_download_reflections(self, request, team):
        if request.user.is_superuser:
            return True
        return (
            self.has_view_permission(request, team)
            and AdminTeamPermission.objects.filter(
                user=request.user,
                team=team,
            ).exists()
        )

    def _consent_export_bundles(self, team):
        return list(
            RequiredDocumentConsentBundle.objects.filter(team=team)
            .select_related('team', 'respondent')
            .prefetch_related(
                Prefetch(
                    'submissions',
                    queryset=DocumentSubmission.objects.order_by('agreed_at', 'id'),
                    to_attr='export_submissions',
                )
            )
            .order_by('respondent__name', 'id')
        )

    def _record_consent_export(self, request, team, *, export_format, result, count, error=''):
        ConsentExportAudit.objects.create(
            admin_user=request.user,
            team=team,
            team_name=team.name,
            export_format=export_format,
            scope='team',
            submission_count=count,
            result=result,
            error_message=error[:500],
            requested_at=timezone.now(),
        )
        record_admin_download(
            request,
            team=team,
            resource_type='required_documents',
            export_format=export_format,
            scope='team',
            record_count=count,
            result=result,
            error_message=error,
        )

    def consent_excel_view(self, request, object_id):
        team = self.get_object(request, object_id)
        if team is None:
            return redirect(reverse('admin:teams_team_changelist'))
        if not self._can_download_reflections(request, team):
            raise PermissionDenied
        bundles = self._consent_export_bundles(team)
        try:
            content = build_consent_workbook(bundles)
        except ConsentExportError as exc:
            self._record_consent_export(
                request, team,
                export_format=ConsentExportAudit.ExportFormat.EXCEL,
                result=ConsentExportAudit.Result.FAILED,
                count=0,
                error=str(exc),
            )
            messages.error(request, f'동의서 Excel을 생성하지 못했습니다: {exc}')
            return redirect(reverse('admin:teams_team_change', args=[team.pk]))
        count = sum(len(bundle.export_submissions) for bundle in bundles)
        self._record_consent_export(
            request, team,
            export_format=ConsentExportAudit.ExportFormat.EXCEL,
            result=ConsentExportAudit.Result.SUCCESS,
            count=count,
        )
        response = HttpResponse(content, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote(consent_excel_filename(bundles))}"
        response['Content-Length'] = str(len(content))
        return response

    def consent_zip_view(self, request, object_id):
        team = self.get_object(request, object_id)
        if team is None:
            return redirect(reverse('admin:teams_team_changelist'))
        if not self._can_download_reflections(request, team):
            raise PermissionDenied
        submissions = list(
            DocumentSubmission.objects.filter(team=team)
            .select_related('team', 'respondent', 'bundle')
            .order_by('respondent__name', 'document_type', 'id')
        )
        try:
            archive = build_consent_zip(submissions, file_prefix=f'{team.name}_동의서')
        except ConsentExportError as exc:
            self._record_consent_export(
                request, team,
                export_format=ConsentExportAudit.ExportFormat.ZIP,
                result=ConsentExportAudit.Result.FAILED,
                count=len(submissions),
                error=str(exc),
            )
            messages.error(request, f'동의서 ZIP을 생성하지 못했습니다: {exc}')
            return redirect(reverse('admin:teams_team_change', args=[team.pk]))
        self._record_consent_export(
            request, team,
            export_format=ConsentExportAudit.ExportFormat.ZIP,
            result=ConsentExportAudit.Result.SUCCESS,
            count=len(archive.submission_ids),
        )
        response = HttpResponse(archive.content, content_type='application/zip')
        response['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote(archive.file_name)}"
        response['Content-Length'] = str(len(archive.content))
        return response

    def reflection_zip_view(self, request, object_id):
        team = self.get_object(request, object_id)
        if team is None:
            return redirect(reverse('admin:teams_team_changelist'))
        if not self._can_download_reflections(request, team):
            raise PermissionDenied

        requested_at = timezone.now()
        try:
            archive = build_team_reflection_zip(team)
        except ReflectionExportError as exc:
            ReflectionDownloadAudit.objects.create(
                admin_user=request.user,
                team=team,
                team_name=team.name,
                submission_count=ReflectionSubmission.objects.filter(team=team).count(),
                result=ReflectionDownloadAudit.Result.FAILED,
                error_message=str(exc)[:500],
                requested_at=requested_at,
            )
            record_admin_download(
                request,
                team=team,
                resource_type='reflections',
                export_format='zip',
                scope='team',
                record_count=ReflectionSubmission.objects.filter(team=team).count(),
                result='failed',
                error_message=str(exc),
            )
            messages.error(request, f'소감문 ZIP을 생성하지 못했습니다: {exc}')
            return redirect(reverse('admin:teams_team_change', args=[team.pk]))

        ReflectionDownloadAudit.objects.create(
            admin_user=request.user,
            team=team,
            team_name=team.name,
            submission_count=len(archive.submission_ids),
            result=ReflectionDownloadAudit.Result.SUCCESS,
            requested_at=requested_at,
        )
        record_admin_download(
            request,
            team=team,
            resource_type='reflections',
            export_format='zip',
            scope='team',
            record_count=len(archive.submission_ids),
            result='success',
        )
        response = HttpResponse(archive.content, content_type='application/zip')
        response['Content-Disposition'] = (
            f"attachment; filename*=UTF-8''{quote(archive.file_name)}"
        )
        response['Content-Length'] = str(len(archive.content))
        return response

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop('delete_selected', None)
        return actions

    def delete_view(self, request, object_id, extra_context=None):
        if not request.user.is_superuser:
            raise PermissionDenied
        team = self.get_object(request, object_id)
        if team is None:
            return redirect(reverse('admin:teams_team_changelist'))

        form = TeamPurgeConfirmationForm(
            request.POST or None,
            team=team,
        )
        if request.method == 'POST' and form.is_valid():
            team_label = str(team)
            deleted_counts = purge_team(team_id=team.pk)
            self.log_deletion(request, team, team_label)
            deleted_total = sum(deleted_counts.values())
            messages.success(
                request,
                f'{team_label} 팀과 연관 데이터 {deleted_total}건을 완전히 삭제했습니다.',
            )
            return redirect(reverse('admin:teams_team_changelist'))

        context = {
            **self.admin_site.each_context(request),
            'title': f'{team} 팀 완전 삭제',
            'opts': self.model._meta,
            'original': team,
            'object': team,
            'object_id': object_id,
            'form': form,
            'purge_counts': get_team_purge_counts(team).values(),
            'global_question_sets_preserved': True,
        }
        if extra_context:
            context.update(extra_context)
        return render(request, self.delete_confirmation_template, context)

    def save_model(self, request, obj, form, change):
        if not obj.code:
            while True:
                candidate = generate_team_code()
                if not Team.objects.filter(code=candidate).exists():
                    obj.code = candidate
                    break
        if not obj.manager_entry_code:
            while True:
                candidate = generate_manager_entry_code()
                if not Team.objects.filter(manager_entry_code=candidate).exists():
                    obj.manager_entry_code = candidate
                    break
        super().save_model(request, obj, form, change)
        affected = getattr(form, 'affected_underage_count', 0)
        if change and affected:
            pending_url = (
                reverse('admin:teams_participant_changelist')
                + f'?team__id__exact={obj.pk}&guardian_consent=pending'
            )
            self.message_user(
                request,
                format_html(
                    '시작일 변경으로 법정대리인 동의 미확인 상태가 된 참가자가 {}명 있습니다. '
                    '동의서를 확인한 뒤 <a href="{}">이 목록</a>에서 체크해 주세요.',
                    affected,
                    pending_url,
                ),
                messages.WARNING,
            )

    @admin.display(description='제출 현황')
    def submission_status_link(self, obj):
        url = reverse('admins:prototype-manager')
        return format_html('<a href="{}?team_id={}">조회/미제출 확인</a>', url, obj.id)


class ParticipantBulkUploadForm(forms.Form):
    team = forms.ModelChoiceField(queryset=Team.objects.filter(is_active=True).order_by('name'), label='팀')
    csv_file = forms.FileField(label='명단 파일 (CSV 또는 XLSX)')
    update_existing = forms.BooleanField(required=False, initial=True, label='중복 데이터 갱신(update)')


class ParticipantAdminForm(forms.ModelForm):
    guardian_written_consent_confirmed = forms.BooleanField(
        required=False,
        label='법정대리인 서면 동의서 확인 완료',
        help_text='팀 시작일 기준 만 14세 미만 참가자일 때 서명된 원본을 확인한 후 선택합니다.',
    )

    class Meta:
        model = Participant
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 성별은 식별 키(이름+생년월일+성별)의 일부라 필수이며 남/여만 허용한다.
        self.fields['gender'].required = True
        self.fields['gender'].choices = [
            choice for choice in self.fields['gender'].choices
            if choice[0] != GenderChoices.UNSPECIFIED
        ]

    def clean_gender(self):
        gender = self.cleaned_data.get('gender')
        if gender not in {GenderChoices.MALE, GenderChoices.FEMALE}:
            raise forms.ValidationError('성별은 남/여 중 하나를 선택하세요. 본인확인에 사용됩니다.')
        return gender

    def clean(self):
        # 만 14세 미만이라도 "동의 대기" 상태로 저장할 수 있다. 확인 전까지 로그인은 막힌다.
        return super().clean()


class GuardianConsentStatusFilter(admin.SimpleListFilter):
    title = '법정대리인 동의'
    parameter_name = 'guardian_consent'

    def lookups(self, request, model_admin):
        return (
            ('pending', '미확인 (로그인 불가)'),
            ('verified', '확인 완료'),
            ('not_required', '해당 없음 (만 14세 이상)'),
        )

    def queryset(self, request, queryset):
        value = self.value()
        if value not in {'pending', 'verified', 'not_required'}:
            return queryset
        underage = Q(pk__in=[])
        for team in Team.objects.only('id', 'start_date'):
            underage |= Q(team=team) & underage_participant_filter(team.start_date)
        if value == 'not_required':
            return queryset.exclude(underage)
        return queryset.filter(underage).filter(guardian_consent_verified_at__isnull=(value == 'pending'))


class GuardianConsentEventInline(admin.TabularInline):
    model = GuardianConsentEvent
    fields = ('occurred_at', 'action', 'source', 'actor', 'document_version', 'note')
    readonly_fields = fields
    extra = 0
    can_delete = False
    ordering = ('-occurred_at', '-id')
    verbose_name = '법정대리인 동의 이력'
    verbose_name_plural = '법정대리인 동의 이력'

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class GuardianConsentRevokeForm(forms.Form):
    reason = forms.CharField(
        label='취소 사유',
        max_length=1000,
        widget=forms.Textarea(attrs={'rows': 3}),
        help_text='이력에 남습니다. 예: 동의서 서명 불일치 확인, 보호자 요청',
    )


UPLOAD_SESSION_PREFIX = 'participant_upload:'


@admin.register(Participant)
class ParticipantAdmin(admin.ModelAdmin):
    form = ParticipantAdminForm
    change_list_template = 'admin/teams/participant/change_list.html'
    change_form_template = 'admin/teams/participant/change_form.html'
    inlines = (GuardianConsentEventInline,)
    list_display = ('team', 'name', 'gender', 'birth_date', 'phone', 'guardian_consent_status', 'is_active', 'updated_at')
    list_filter = ('team', GuardianConsentStatusFilter, 'gender', 'is_active')
    search_fields = ('name', 'phone', 'normalized_phone')
    fields = (
        'team', 'name', 'gender', 'birth_date', 'phone', 'is_active',
        'guardian_written_consent_confirmed',
        'guardian_consent_verified_at', 'guardian_consent_verified_by',
        'guardian_consent_method', 'guardian_consent_document_version',
    )
    readonly_fields = (
        'guardian_consent_verified_at', 'guardian_consent_verified_by',
        'guardian_consent_method', 'guardian_consent_document_version',
    )

    @admin.display(description='법정대리인 동의', boolean=True)
    def guardian_consent_status(self, obj):
        if is_eligible_participant_age(obj.birth_date, obj.team.start_date):
            return None
        return participant_has_required_guardian_consent(obj)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        field = super().formfield_for_foreignkey(db_field, request, **kwargs)
        disable_related_widget(field)
        return field

    def save_model(self, request, obj, form, change):
        obj.normalized_name = normalize_name(obj.name or '')
        obj.normalized_phone = normalize_phone(obj.phone or '')
        is_under_age = bool(
            obj.birth_date
            and obj.team_id
            and not is_eligible_participant_age(obj.birth_date, obj.team.start_date)
        )
        should_verify = bool(
            is_under_age
            and form
            and form.cleaned_data.get('guardian_written_consent_confirmed')
            and not obj.guardian_consent_verified_at
        )
        if should_verify:
            mark_guardian_consent_verified(obj, actor=request.user)
        # 만 14세 이상이 되어도 기존 동의 기록은 지우지 않는다(판정에만 쓰이지 않음).
        super().save_model(request, obj, form, change)
        if should_verify:
            record_guardian_consent_event(
                obj,
                action=GuardianConsentEvent.Action.VERIFIED,
                actor=request.user,
                source=GuardianConsentEvent.Source.ADMIN_FORM,
                occurred_at=obj.guardian_consent_verified_at,
            )
            self.message_user(request, '법정대리인 서면 동의 확인을 기록했습니다.', messages.SUCCESS)
        elif is_under_age and not obj.guardian_consent_verified_at:
            self.message_user(
                request,
                '만 14세 미만 참가자가 법정대리인 동의 미확인 상태로 저장되었습니다. '
                '동의서를 확인한 뒤 "법정대리인 서면 동의서 확인 완료"를 체크해야 로그인할 수 있습니다.',
                messages.WARNING,
            )

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('bulk-upload/', self.admin_site.admin_view(self.bulk_upload_view), name='teams_participant_bulk_upload'),
            path(
                'bulk-upload/<str:token>/preview/',
                self.admin_site.admin_view(self.bulk_upload_preview_view),
                name='teams_participant_bulk_upload_preview',
            ),
            path(
                'bulk-upload/<str:token>/errors.csv',
                self.admin_site.admin_view(self.bulk_upload_errors_csv_view),
                name='teams_participant_bulk_upload_errors',
            ),
            path(
                '<path:object_id>/guardian-consent/revoke/',
                self.admin_site.admin_view(self.guardian_consent_revoke_view),
                name='teams_participant_guardian_consent_revoke',
            ),
        ]
        return custom_urls + urls

    # ---- 법정대리인 동의 취소 ----
    def guardian_consent_revoke_view(self, request, object_id):
        participant = self.get_object(request, unquote(object_id))
        if participant is None:
            raise Http404
        if not self.has_change_permission(request, participant):
            raise PermissionDenied
        form = GuardianConsentRevokeForm(request.POST or None)
        if request.method == 'POST' and form.is_valid():
            try:
                revoke_guardian_consent(
                    participant=participant,
                    actor=request.user,
                    reason=form.cleaned_data['reason'],
                )
            except GuardianConsentNotActive as exc:
                self.message_user(request, str(exc), messages.ERROR)
            else:
                self.message_user(
                    request,
                    f'{participant.name} 참가자의 법정대리인 동의 확인을 취소했습니다. '
                    '이후 로그인과 새 제출은 막히고, 이미 제출된 기록은 유지됩니다.',
                    messages.WARNING,
                )
            return redirect(reverse('admin:teams_participant_change', args=[participant.pk]))
        context = {
            **self.admin_site.each_context(request),
            'title': '법정대리인 동의 확인 취소',
            'opts': self.model._meta,
            'original': participant,
            'form': form,
            'can_revoke': bool(participant.guardian_consent_verified_at),
        }
        return TemplateResponse(request, 'admin/teams/participant/guardian_consent_revoke.html', context)

    # ---- 명단 일괄 등록: 검증 → 확정 ----
    def _upload_session_key(self, token: str) -> str:
        return f'{UPLOAD_SESSION_PREFIX}{token}'

    def _load_upload_session(self, request, token: str) -> dict:
        data = request.session.get(self._upload_session_key(token))
        if not data:
            raise Http404
        return data

    def bulk_upload_view(self, request):
        if not self.has_add_permission(request):
            raise PermissionDenied
        form = ParticipantBulkUploadForm(request.POST or None, request.FILES or None)
        if request.method == 'POST' and form.is_valid():
            team = form.cleaned_data['team']
            upload = form.cleaned_data['csv_file']
            update_existing = form.cleaned_data['update_existing']
            try:
                rows = list(iter_upload_rows(upload.name, upload.read()))
            except forms.ValidationError as exc:
                messages.error(request, ' '.join(exc.messages))
                return redirect('.')
            if not rows:
                messages.error(request, '업로드할 데이터가 없습니다.')
                return redirect('.')
            missing = missing_required_columns(rows)
            if missing:
                messages.error(request, f'필수 컬럼 누락: {", ".join(missing)}')
                return redirect('.')
            plans = plan_participant_upload(team=team, rows=rows, update_existing=update_existing)
            token = secrets.token_urlsafe(12)
            request.session[self._upload_session_key(token)] = {
                'team_id': team.pk,
                'update_existing': update_existing,
                'file_name': upload.name,
                'plans': [plan.as_dict() for plan in plans],
                'applied': False,
            }
            return redirect(reverse('admin:teams_participant_bulk_upload_preview', args=[token]))

        context = {
            **self.admin_site.each_context(request),
            'opts': self.model._meta,
            'title': '대상자 명단 일괄등록',
            'form': form,
        }
        return render(request, 'admin/teams/participant/bulk_upload.html', context)

    def bulk_upload_preview_view(self, request, token):
        if not self.has_add_permission(request):
            raise PermissionDenied
        data = self._load_upload_session(request, token)
        team = Team.objects.filter(pk=data['team_id']).first()
        if team is None:
            raise Http404
        plans = [UploadRowPlan.from_dict(item) for item in data['plans']]
        summary = summarize_plans(plans)
        errors_url = reverse('admin:teams_participant_bulk_upload_errors', args=[token])

        if request.method == 'POST' and not data.get('applied'):
            audit = apply_participant_upload(
                team=team,
                plans=plans,
                update_existing=data['update_existing'],
                actor=request.user,
                file_name=data.get('file_name', ''),
            )
            data['applied'] = True
            request.session[self._upload_session_key(token)] = data
            message = format_html(
                '일괄 등록 완료: 생성 {}건, 갱신 {}건, 건너뜀 {}건, 오류 {}건.',
                audit.created_count, audit.updated_count, audit.skipped_count, audit.error_count,
            )
            if audit.pending_consent_count:
                pending_url = (
                    reverse('admin:teams_participant_changelist')
                    + f'?team__id__exact={team.pk}&guardian_consent=pending'
                )
                message += format_html(
                    ' 만 14세 미만 동의 대기 {}건은 <a href="{}">이 목록</a>에서 동의서 확인 후 체크하세요.',
                    audit.pending_consent_count, pending_url,
                )
            if audit.error_count:
                message += format_html(
                    ' 오류 행 {}건은 <a href="{}">CSV로 내려받아</a> 고친 뒤 다시 올리세요.',
                    audit.error_count, errors_url,
                )
            self.message_user(
                request,
                message,
                messages.WARNING if (audit.error_count or audit.pending_consent_count) else messages.SUCCESS,
            )
            return redirect(reverse('admin:teams_participant_changelist') + f'?team__id__exact={team.pk}')

        context = {
            **self.admin_site.each_context(request),
            'opts': self.model._meta,
            'title': '대상자 명단 일괄등록 확인',
            'team': team,
            'plans': plans,
            'summary': summary,
            'applied': bool(data.get('applied')),
            'update_existing': data['update_existing'],
            'file_name': data.get('file_name', ''),
            'errors_url': errors_url,
            'upload_url': reverse('admin:teams_participant_bulk_upload'),
        }
        return TemplateResponse(request, 'admin/teams/participant/bulk_upload_preview.html', context)

    def bulk_upload_errors_csv_view(self, request, token):
        if not self.has_add_permission(request):
            raise PermissionDenied
        data = self._load_upload_session(request, token)
        plans = [UploadRowPlan.from_dict(item) for item in data['plans']]
        response = HttpResponse(error_rows_csv(plans), content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote('명단_오류행.csv')}"
        return response

@admin.register(Respondent)
class RespondentAdmin(SuperuserOnlyAdminMixin, ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("id", "team", "name", "gender", "birth_date", "phone")
    list_filter = ("team",)
    search_fields = ("name", "phone", "normalized_phone")

    def get_deleted_objects(self, objs, request):
        if not request.user.is_superuser:
            return super().get_deleted_objects(objs, request)

        using = router.db_for_write(self.model)
        collector = NestedObjects(using=using)
        respondents = list(objs)
        respondent_ids = [respondent.pk for respondent in respondents]
        related_querysets = (
            SurveySubmission.objects.filter(respondent_id__in=respondent_ids),
            ExitSurveySubmission.objects.filter(respondent_id__in=respondent_ids),
            DocumentSubmission.objects.filter(respondent_id__in=respondent_ids),
            ReflectionSubmission.objects.filter(respondent_id__in=respondent_ids),
            RequiredDocumentConsentBundle.objects.filter(
                respondent_id__in=respondent_ids,
            ),
        )
        for queryset in related_querysets:
            related_objects = list(queryset)
            if related_objects:
                collector.collect(related_objects)
        collector.collect(respondents)
        model_count = {
            model._meta.verbose_name_plural: len(instances)
            for model, instances in collector.model_objs.items()
        }
        return collector.nested(format_callback=str), model_count, set(), []

    def delete_model(self, request, obj):
        if request.user.is_superuser:
            purge_respondents(respondent_ids=[obj.pk])

    def delete_queryset(self, request, queryset):
        if request.user.is_superuser:
            purge_respondents(
                respondent_ids=queryset.values_list('pk', flat=True),
            )


@admin.register(ParticipantUploadAudit)
class ParticipantUploadAuditAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        'requested_at', 'admin_user', 'team_name', 'file_name', 'total_rows',
        'created_count', 'updated_count', 'skipped_count', 'error_count', 'pending_consent_count',
    )
    list_filter = ('team', 'requested_at')
    search_fields = ('team_name', 'file_name', 'admin_user__username')
    date_hierarchy = 'requested_at'
    ordering = ('-requested_at', '-id')


@admin.register(GuardianConsentEvent)
class GuardianConsentEventAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('occurred_at', 'participant', 'participant_team', 'action', 'source', 'actor', 'document_version', 'note')
    list_filter = ('action', 'source', 'participant__team', 'occurred_at')
    search_fields = ('participant__name', 'participant__phone', 'note', 'actor__username')
    date_hierarchy = 'occurred_at'
    ordering = ('-occurred_at', '-id')
    list_select_related = ('participant', 'participant__team', 'actor')

    @admin.display(description='팀', ordering='participant__team__name')
    def participant_team(self, obj):
        return obj.participant.team
