import json
import re
from urllib.parse import quote

from django import forms
from django.contrib import admin
from django.contrib import messages
from django.contrib.admin.utils import unquote
from django.core.exceptions import PermissionDenied
from django.db.models import Max, Prefetch
from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.utils.html import format_html, format_html_join
from django.urls import path, reverse
from django.utils import timezone

from admins.permissions import allowed_team_ids
from admins.survey_excel import (
    build_survey_results_workbook,
    survey_results_filename,
)
from teams.models import Respondent, Team
from .models import (
    AnonymousSurveySubmission,
    AdminDownloadAudit,
    CommonQuestion,
    CommonQuestionSection,
    CommonQuestionSet,
    CommonQuestionSetDeployment,
    ConsentExportAudit,
    DocumentSubmission,
    DocumentTemplate,
    ExitSurveySubmission,
    ReflectionDownloadAudit,
    RequiredDocumentConsentBundle,
    ReflectionSubmission,
    SurveyAnswer,
    SurveyForm,
    SurveyQuestion,
    SurveySection,
    SurveySubmission,
    TeamCommonQuestionExclusion,
)
from .consent_exports import (
    ConsentExportError,
    build_consent_workbook,
    build_consent_zip,
    consent_excel_filename,
    consent_pdf_filename,
    render_consent_confirmation_pdf,
)
from .survey_deployment import (
    clone_common_question_set_to_days,
    deploy_common_question_set_to_team,
    deploy_common_question_set_to_teams,
)
from .services import normalize_option_label_en, result_summary_from_result_json
from .download_audits import record_admin_download


class ReadOnlyAdminMixin:
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def get_readonly_fields(self, request, obj=None):
        model_fields = [field.name for field in self.model._meta.fields]
        declared_fields = list(super().get_readonly_fields(request, obj))
        return tuple(dict.fromkeys([*model_fields, *declared_fields]))


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


class HiddenAdminMenuMixin:
    """Keep internal admin URLs available without listing them as operator menus."""

    def get_model_perms(self, request):
        return {}


def format_submission_answer(value):
    if isinstance(value, list):
        return ', '.join(str(item) for item in value) or '미입력'
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False) or '미입력'
    return str(value).strip() or '미입력'


class SurveySectionForm(forms.ModelForm):
    class Meta:
        model = SurveySection
        fields = '__all__'

    class FormChoiceField(forms.ModelChoiceField):
        def label_from_instance(self, obj):
            return f'[{obj.team.name}] {obj.day_no}일차 · {obj.title} (v{obj.version})'

    form = FormChoiceField(
        queryset=SurveyForm.objects.select_related('team').order_by('team__name', 'day_no', '-version'),
        help_text='먼저 SurveyForm(팀/일차/버전)을 생성한 뒤 섹션을 추가하세요.',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        disable_related_widget(self.fields['form'])
        self.fields['title'].help_text = '예: 신체 상태, 정신 상태 등 섹션 이름'
        self.fields['section_type'].help_text = '신체/정신/설문 영역 구분값입니다.'
        self.fields['display_order'].help_text = '같은 Form 내에서 섹션 표시 순서(1부터 권장)'


class SurveyQuestionForm(forms.ModelForm):
    option_lines = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 5}),
        label='보기 입력(줄바꿈)',
        help_text='선택형 문항 입력 예시: 전혀 없음|1 (한 줄에 하나씩, |뒤는 점수)',
    )
    option_lines_en = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 5}),
        label='선택지 영문',
        help_text='한국어 선택지와 같은 순서로 영문 문구를 한 줄에 하나씩 입력하세요.',
    )

    class Meta:
        model = SurveyQuestion
        fields = '__all__'

    class SectionChoiceField(forms.ModelChoiceField):
        def label_from_instance(self, obj):
            return f'[{obj.form.team.name}] {obj.form.day_no}일차 · {obj.form.title} > {obj.title}'

    section = SectionChoiceField(
        queryset=SurveySection.objects.select_related('form__team').order_by('form__team__name', 'form__day_no', 'display_order'),
        help_text='먼저 SurveySection을 생성한 뒤 문항을 추가하세요.',
        label='상위 섹션',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        disable_related_widget(self.fields['section'])
        if 'key' in self.fields:
            self.fields['key'].help_text = '영문/숫자/언더스코어만 사용 (예: q_sleep_quality)'
            self.fields['key'].label = '문항 키'
        if 'label' in self.fields:
            self.fields['label'].help_text = '사용자에게 보이는 문항 문구'
            self.fields['label'].label = '문항 문구(한국어)'
        if 'label_en' in self.fields:
            self.fields['label_en'].label = '문항 문구(영어)'
        if 'graph_label' in self.fields:
            self.fields['graph_label'].help_text = '결과 카드/그래프에 표시할 짧은 이름 (예: 통증, 수면)'
            self.fields['graph_label'].label = '결과 그래프 표시명(한국어)'
        if 'graph_label_en' in self.fields:
            self.fields['graph_label_en'].label = '결과 그래프 표시명(영어)'
        if 'display_order' in self.fields:
            self.fields['display_order'].help_text = '같은 Section 내 표시 순서(1부터 권장)'
            self.fields['display_order'].label = '표시 순서'
        if 'is_required' in self.fields:
            self.fields['is_required'].help_text = '필수 응답 여부'
            self.fields['is_required'].label = '필수 응답 여부'
        if 'question_type' in self.fields:
            self.fields['question_type'].help_text = '주관식은 선택지 비움, 선택형은 아래 "보기 입력" 사용 권장'
            self.fields['question_type'].label = '문항 유형'
        if 'options_json' in self.fields:
            self.fields['options_json'].help_text = (
                '고급 편집용 원본 JSON 필드입니다. 일반적으로는 위 "보기 입력(줄바꿈)" 필드를 사용하세요.'
            )
            self.fields['options_json'].label = '선택지 저장값(JSON)'
        if 'option_lines' in self.fields:
            self.fields['option_lines'].label = '선택지 및 점수 입력'
            self.fields['option_lines'].help_text = (
                "각 줄에 '보기문구|점수' 형식으로 입력하세요. "
                "예: 전혀 없음|1 / 가끔 있음|2 / 자주 있음|3"
            )
        options = self.instance.options_json if self.instance and self.instance.pk else []
        if 'option_lines' in self.fields:
            self.fields['option_lines'].initial = self._to_option_lines(options)
        self.fields['option_lines_en'].initial = '\n'.join(
            normalize_option_label_en(item.get('label_en', ''))
            for item in options
            if isinstance(item, dict)
        )

    @staticmethod
    def _to_option_lines(options):
        lines = []
        for item in options or []:
            if isinstance(item, dict):
                label = str(item.get('label', '')).strip()
                score = item.get('score', 0)
                if label:
                    lines.append(f'{label}|{score}')
            else:
                label = str(item).strip()
                if label:
                    lines.append(f'{label}|0')
        return '\n'.join(lines)

    @staticmethod
    def _parse_option_lines(value: str) -> list[dict]:
        parsed = []
        for raw_line in value.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if '|' in line:
                label, raw_score = line.split('|', 1)
            else:
                label, raw_score = line, '0'
            label = label.strip()
            if not label:
                continue
            try:
                score = int(raw_score.strip())
            except ValueError as exc:
                raise forms.ValidationError(f'점수는 정수여야 합니다: "{raw_line}"') from exc
            parsed.append({'label': label, 'score': score})
        return parsed

    def clean_key(self):
        key = (self.cleaned_data.get('key') or '').strip()
        if not re.fullmatch(r'[A-Za-z0-9_]+', key):
            raise forms.ValidationError('key는 영문/숫자/언더스코어만 사용할 수 있습니다.')
        return key

    def clean(self):
        cleaned = super().clean()
        question_type = cleaned.get('question_type')
        options = cleaned.get('options_json')
        option_lines = (cleaned.get('option_lines') or '').strip()
        parsed_lines = self._parse_option_lines(option_lines) if option_lines else []
        english_lines = [
            normalize_option_label_en(line)
            for line in (cleaned.get('option_lines_en') or '').splitlines()
            if line.strip()
        ]

        if parsed_lines:
            if english_lines and len(english_lines) != len(parsed_lines):
                self.add_error('option_lines_en', '한국어 선택지와 영어 선택지의 개수가 같아야 합니다.')
            for index, option in enumerate(parsed_lines):
                if index < len(english_lines):
                    option['label_en'] = english_lines[index]
            options = parsed_lines
            cleaned['options_json'] = parsed_lines

        if question_type == SurveyQuestion.QuestionType.TEXT:
            if options not in (None, [], {}):
                self.add_error('options_json', '주관식 문항은 선택지를 입력하지 않습니다.')
            return cleaned

        if question_type in (SurveyQuestion.QuestionType.SINGLE, SurveyQuestion.QuestionType.MULTI):
            if not isinstance(options, list) or not options:
                self.add_error('option_lines', '선택형 문항은 보기와 점수를 입력해야 합니다. 예: 전혀 없음|1')
                return cleaned

            invalid = [
                opt for opt in options
                if not (
                    isinstance(opt, str) and opt.strip()
                    or isinstance(opt, dict) and str(opt.get('label', '')).strip() != ''
                )
            ]
            if invalid:
                self.add_error('option_lines', '보기 문구는 비어 있을 수 없습니다.')

        return cleaned


class CommonQuestionForm(SurveyQuestionForm):
    class Meta:
        model = CommonQuestion
        fields = (
            'section',
            'key',
            'label',
            'label_en',
            'graph_label',
            'graph_label_en',
            'question_type',
            'options_json',
            'is_required',
            'display_order',
        )

    class SectionChoiceField(forms.ModelChoiceField):
        def label_from_instance(self, obj):
            return f'[{obj.question_set.name}] {obj.question_set.day_no}일차 · {obj.title}'

    section = SectionChoiceField(
        queryset=CommonQuestionSection.objects.select_related('question_set').order_by(
            'question_set__name', 'question_set__day_no', 'display_order'
        ),
        help_text='먼저 CommonQuestionSection을 생성한 뒤 문항을 추가하세요.',
        label='상위 섹션',
    )

    def clean(self):
        cleaned = super().clean()
        section = cleaned.get('section')
        if section:
            if self.instance.question_set_id and (
                self.instance.question_set_id != section.question_set_id
            ):
                self.add_error(
                    'section',
                    '같은 공통 설문에 속한 섹션만 선택할 수 있습니다.',
                )
            self.instance.question_set_id = section.question_set_id
        question_type = cleaned.get('question_type')
        if question_type == SurveyQuestion.QuestionType.TEXT:
            return cleaned
        options = cleaned.get('options_json')
        if not isinstance(options, list):
            return cleaned
        invalid = [
            opt for opt in options
            if not (
                isinstance(opt, str) and opt.strip()
                or isinstance(opt, dict) and str(opt.get('label', '')).strip() != ''
            )
        ]
        if invalid:
            self.add_error('option_lines', '보기 문구는 비어 있을 수 없습니다.')
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        if 'options_json' in self.cleaned_data:
            instance.options_json = self.cleaned_data['options_json']
        if commit:
            instance.save()
            self.save_m2m()
        return instance


@admin.register(SurveyForm)
class SurveyFormAdmin(HiddenAdminMenuMixin, admin.ModelAdmin):
    list_display = ("team", "title", "day_no", "version", "form_type", "is_anonymous", "is_active", "updated_at")
    list_filter = ("team", "day_no", "form_type", "is_anonymous", "is_active")
    search_fields = ("title", "team__name", "team__code")
    ordering = ('team__name', 'day_no', '-version')
    exclude = ('schema_json',)
    fieldsets = (
        ('기본 정보', {'fields': ('team', 'day_no', 'title', 'title_en', 'form_type', 'is_anonymous', 'is_active')}),
        ('운영 안내', {'fields': (), 'description': '운영 기본 순서: Form 생성 → Section 생성 → Question 생성'}),
    )
    readonly_fields = ('version',)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        field = super().formfield_for_foreignkey(db_field, request, **kwargs)
        disable_related_widget(field)
        return field

    def save_model(self, request, obj, form, change):
        if not change:
            max_version = (
                SurveyForm.objects.filter(team=obj.team, day_no=obj.day_no)
                .aggregate(max_ver=Max('version'))
                .get('max_ver')
                or 0
            )
            obj.version = max_version + 1
        if not obj.schema_json:
            obj.schema_json = {'sections': []}
        super().save_model(request, obj, form, change)


@admin.register(SurveySection)
class SurveySectionAdmin(HiddenAdminMenuMixin, admin.ModelAdmin):
    form = SurveySectionForm
    list_display = ('form_context', 'title', 'section_type', 'display_order')
    list_filter = ('section_type', 'form')
    search_fields = ('title', 'form__title', 'form__team__name', 'form__team__code')
    ordering = ('form__team__name', 'form__day_no', 'display_order')
    fieldsets = (
        ('상위 폼 선택', {'fields': ('form',), 'description': '1) SurveyForm을 먼저 생성하세요.'}),
        ('섹션 정보', {'fields': ('title', 'title_en', 'section_type', 'display_order'), 'description': '2) 같은 Form에 섹션을 순서대로 추가하세요.'}),
    )

    @admin.display(description='상위 폼')
    def form_context(self, obj):
        return f'[{obj.form.team.name}] {obj.form.day_no}일차 · {obj.form.title}'


@admin.register(SurveyQuestion)
class SurveyQuestionAdmin(HiddenAdminMenuMixin, admin.ModelAdmin):
    form = SurveyQuestionForm
    list_display = ('section_context', 'label', 'question_type', 'is_required', 'display_order')
    list_filter = ('question_type', 'is_required', 'section')
    search_fields = ('key', 'label', 'section__title', 'section__form__title', 'section__form__team__name')
    ordering = ('section__form__team__name', 'section__form__day_no', 'section__display_order', 'display_order')
    fieldsets = (
        ('상위 섹션 선택', {'fields': ('section',), 'description': '1) 먼저 상위 섹션을 선택하세요.'}),
        ('문항 기본값', {'fields': ('key', 'label', 'label_en', 'graph_label', 'graph_label_en', 'question_type', 'is_required', 'display_order'), 'description': '2) 문항 키와 한·영 문항 문구, 결과 표시명, 유형, 필수 여부, 표시 순서를 입력하세요.'}),
        ('선택형 보기', {'fields': ('option_lines', 'option_lines_en'), 'description': "3) 한국어는 '보기문구|점수', 영어는 같은 순서의 문구를 줄바꿈으로 입력하세요."}),
    )
    def get_fieldsets(self, request, obj=None):
        base = list(super().get_fieldsets(request, obj))
        if request.user.is_superuser:
            base.append(
                (
                    '고급 설정(슈퍼유저)',
                    {
                        'fields': ('options_json',),
                        'description': '내부 저장 JSON 직접 수정이 필요한 경우에만 사용하세요.',
                    },
                )
            )
        return tuple(base)

    def get_readonly_fields(self, request, obj=None):
        return ('key',) if obj else ()

    @admin.display(description='상위 섹션')
    def section_context(self, obj):
        return f'[{obj.section.form.team.name}] {obj.section.form.day_no}일차 · {obj.section.title}'


@admin.register(SurveySubmission)
class SurveySubmissionAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("team", "respondent", "day_no", "is_locked", "submitted_at", "result_summary")
    list_filter = ("team", "day_no", "is_locked", "submitted_at")
    search_fields = ("respondent__name", "respondent__normalized_phone")
    readonly_fields = (
        'team',
        'respondent',
        'day_no',
        'is_locked',
        'submitted_at',
        'form_version',
        'answer_overview',
    )
    fields = readonly_fields

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(team_id__in=allowed_team_ids(request.user))

    @admin.display(description='결과 요약')
    def result_summary(self, obj):
        return result_summary_from_result_json(obj.result_json)

    @admin.display(description='답변 요약')
    def answer_overview(self, obj):
        answers = obj.answers_json or {}
        items = []
        snapshot_keys = set()
        for section in (obj.form_snapshot_json or {}).get('sections') or []:
            for question in section.get('questions') or []:
                key = str(question.get('key') or '')
                if not key:
                    continue
                snapshot_keys.add(key)
                items.append(
                    (
                        question.get('label') or key,
                        format_submission_answer(answers.get(key)),
                    )
                )
        fallback_labels = {}
        missing_snapshot_keys = set(answers) - snapshot_keys
        if missing_snapshot_keys:
            fallback_labels = dict(
                obj.answers.filter(question__key__in=missing_snapshot_keys)
                .values_list('question__key', 'question__label')
            )
        for key, value in answers.items():
            if key not in snapshot_keys:
                items.append(
                    (
                        fallback_labels.get(key, key),
                        format_submission_answer(value),
                    )
                )
        if not items:
            return '-'
        return format_html(
            '<ul style="margin:0; padding-left:18px;">{}</ul>',
            format_html_join(
                '',
                '<li><strong>{}</strong>: {}</li>',
                items,
            ),
        )


@admin.register(SurveyAnswer)
class SurveyAnswerAdmin(
    HiddenAdminMenuMixin,
    SuperuserOnlyAdminMixin,
    ReadOnlyAdminMixin,
    admin.ModelAdmin,
):
    list_display = (
        'id',
        'respondent_name',
        'team_name',
        'day_no',
        'submission_id_display',
        'question_display',
        'answer_preview',
        'created_at',
    )
    list_filter = ('submission__team', 'submission__day_no', 'question__question_type', 'created_at')
    search_fields = (
        'submission__respondent__name',
        'submission__respondent__phone',
        'question__key',
        'question__label',
        'answer_text',
        'submission__id',
    )
    ordering = ('-created_at', '-id')
    list_select_related = ('submission__team', 'submission__respondent', 'question__section')
    date_hierarchy = 'created_at'
    readonly_fields = (
        'submission',
        'respondent_name',
        'team_name',
        'day_no',
        'question_display',
        'answer_text',
        'answer_json',
        'created_at',
        'updated_at',
    )
    fields = readonly_fields

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'submission__team',
            'submission__respondent',
            'question__section',
        )

    @admin.display(description='응답자')
    def respondent_name(self, obj):
        return obj.submission.respondent.name

    @admin.display(description='팀')
    def team_name(self, obj):
        return obj.submission.team.name

    @admin.display(description='일차')
    def day_no(self, obj):
        return obj.submission.day_no

    @admin.display(description='제출 ID')
    def submission_id_display(self, obj):
        return obj.submission_id

    @admin.display(description='문항')
    def question_display(self, obj):
        return f'[{obj.question.key}] {obj.question.label}'

    @admin.display(description='답변')
    def answer_preview(self, obj):
        answer_value = obj.answer_text
        if not answer_value and isinstance(obj.answer_json, dict):
            answer_value = obj.answer_json.get('value', '')
        answer_value = answer_value or '미입력'
        return answer_value if len(answer_value) <= 60 else f'{answer_value[:57]}...'


class CommonQuestionSetAdminForm(forms.ModelForm):
    target_teams = forms.ModelMultipleChoiceField(
        queryset=Team.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label='배포 대상 팀',
        help_text='선택한 팀에만 배포합니다.',
    )
    deploy_after_save = forms.BooleanField(
        required=False,
        label='저장 후 선택한 팀에 배포',
        help_text='체크한 경우 섹션과 문항까지 저장한 다음 선택 팀에 배포합니다.',
    )
    class Meta:
        model = CommonQuestionSet
        fields = '__all__'
        labels = {
            'name': '설문 관리명',
            'title': '참가자 화면 제목',
            'title_en': '참가자 화면 제목(영어)',
            'form_type': '설문 유형',
            'day_no': '적용 일차',
            'is_active': '사용 여부',
            'description': '운영 메모',
        }
        help_texts = {
            'name': '관리자 화면에서 설문을 구분할 고유한 이름입니다. 예: 1일차 기본 자가검진',
            'title': '참가자가 설문 화면에서 확인하는 제목입니다.',
            'form_type': '자가건강검진 또는 탐방 종료 설문 중 하나를 선택합니다.',
            'day_no': '현재 단계에서는 설문 유형과 관계없이 1 이상의 일차를 입력합니다.',
            'is_active': '해제하면 이후 팀 배포 시 비활성 상태로 생성됩니다.',
            'description': '운영 목적이나 담당자 참고사항을 입력합니다. 참가자에게는 표시되지 않습니다.',
        }
        widgets = {
            'description': forms.Textarea(attrs={'rows': 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['target_teams'].queryset = Team.objects.filter(
            is_active=True,
        ).order_by('name', 'id')
        if not self.instance.pk:
            self.fields['target_teams'].disabled = True
            self.fields['deploy_after_save'].disabled = True
            self.fields['target_teams'].help_text = (
                '설문과 섹션을 먼저 저장한 뒤 편집 화면에서 대상 팀을 선택하세요.'
            )

    def clean(self):
        cleaned_data = super().clean()
        target_teams = cleaned_data.get('target_teams')
        deploy_after_save = cleaned_data.get('deploy_after_save')
        if deploy_after_save and not target_teams:
            self.add_error(
                'target_teams',
                '배포할 팀을 한 개 이상 선택하세요.',
            )
        return cleaned_data

    def clean_name(self):
        return self.cleaned_data['name'].strip()

    def clean_title(self):
        return self.cleaned_data['title'].strip()

    def clean_description(self):
        return self.cleaned_data.get('description', '').strip()


class CommonQuestionSetCloneForm(forms.Form):
    target_days = forms.CharField(
        label='복제할 일차',
        help_text='쉼표 또는 공백으로 구분해 입력합니다. 예: 2, 3, 4',
    )

    def clean_target_days(self):
        tokens = [
            token
            for token in re.split(r'[,\s]+', self.cleaned_data['target_days'].strip())
            if token
        ]
        day_numbers: list[int] = []
        for token in tokens:
            if not token.isdecimal() or int(token) < 1:
                raise forms.ValidationError('일차는 1 이상의 정수로 입력하세요.')
            day_no = int(token)
            if day_no not in day_numbers:
                day_numbers.append(day_no)
        if not day_numbers:
            raise forms.ValidationError('복제할 일차를 한 개 이상 입력하세요.')
        return day_numbers


class CommonQuestionSectionInlineForm(forms.ModelForm):
    class Meta:
        model = CommonQuestionSection
        fields = ('title', 'title_en', 'section_type', 'display_order')
        labels = {
            'title': '섹션 제목',
            'title_en': '섹션 제목(영어)',
            'section_type': '섹션 유형',
            'display_order': '표시 순서',
        }
        help_texts = {
            'title': '예: 신체 상태, 정신 상태, 탐방 만족도',
            'section_type': '신체, 정신 또는 설문 영역을 선택합니다.',
            'display_order': '참가자 화면에 표시할 순서입니다.',
        }


class CommonQuestionSectionInline(admin.TabularInline):
    model = CommonQuestionSection
    form = CommonQuestionSectionInlineForm
    fields = ('title', 'title_en', 'section_type', 'display_order')
    extra = 1
    ordering = ('display_order', 'id')
    show_change_link = True
    verbose_name = '설문 섹션'
    verbose_name_plural = '설문 섹션'


class CommonQuestionInline(admin.StackedInline):
    model = CommonQuestion
    form = CommonQuestionForm
    fk_name = 'question_set'
    fields = (
        'section',
        'key',
        'label',
        'label_en',
        'graph_label',
        'graph_label_en',
        'question_type',
        'option_lines',
        'option_lines_en',
        'is_required',
        'display_order',
    )
    extra = 1
    ordering = ('section__display_order', 'display_order', 'id')
    show_change_link = True
    verbose_name = '설문 문항'
    verbose_name_plural = '설문 문항'

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        section_field = formset.form.base_fields.get('section')
        if section_field is not None:
            section_field.queryset = (
                CommonQuestionSection.objects.filter(question_set=obj).order_by(
                    'display_order',
                    'id',
                )
                if obj
                else CommonQuestionSection.objects.none()
            )
            section_field.help_text = '위에서 저장한 이 공통 설문의 섹션 중 하나를 선택하세요.'
        return formset


@admin.register(CommonQuestionSet)
class CommonQuestionSetAdmin(admin.ModelAdmin):
    form = CommonQuestionSetAdminForm
    change_form_template = 'admin/surveys/commonquestionset/change_form.html'
    inlines = (CommonQuestionSectionInline, CommonQuestionInline)
    list_display = (
        'management_name',
        'participant_title',
        'survey_type',
        'applicable_day',
        'is_active',
        'updated_at',
    )
    list_filter = ('form_type', 'is_active', 'day_no')
    search_fields = ('name', 'title')
    ordering = ('form_type', 'day_no', 'name')
    list_per_page = 30
    actions = []
    fieldsets = (
        (
            '설문 기본 정보',
            {
                'fields': ('form_type', 'name', 'title', 'title_en', 'is_active'),
                'description': '설문 유형과 관리자용 구분명, 참가자에게 표시할 제목을 입력합니다.',
            },
        ),
        (
            '운영 일정',
            {
                'fields': ('day_no',),
                'description': (
                    '자가건강검진의 적용 일차를 입력합니다. 종료 설문의 일차 정책은 다음 단계에서 분리합니다. '
                    '같은 문항을 다른 일차에도 쓰려면 저장 후 우측 상단 "다른 일차로 복제" 버튼을 사용하세요.'
                ),
            },
        ),
        (
            '운영 메모',
            {
                'fields': ('description',),
                'description': '배포 전 확인할 내부 운영 정보를 기록합니다.',
            },
        ),
        (
            '대상 팀 선택 배포',
            {
                'fields': (
                    'target_teams',
                    'deploy_after_save',
                ),
                'description': (
                    '설문 저장과 팀 배포를 한 화면에서 처리합니다. '
                    '기존 활성 설문과 한·영 전체 내용이 달라진 경우 '
                    '기존 버전을 보존하고 새 활성 버전을 생성합니다.'
                ),
            },
        ),
    )

    @admin.display(description='설문 관리명', ordering='name')
    def management_name(self, obj):
        return obj.name

    @admin.display(description='참가자 화면 제목', ordering='title')
    def participant_title(self, obj):
        return obj.title

    @admin.display(description='설문 유형', ordering='form_type')
    def survey_type(self, obj):
        return obj.get_form_type_display()

    @admin.display(description='적용 일차', ordering='day_no')
    def applicable_day(self, obj):
        return f'{obj.day_no}일차'

    def get_inline_instances(self, request, obj=None):
        inline_instances = super().get_inline_instances(request, obj)
        if obj is None:
            return [
                inline
                for inline in inline_instances
                if not isinstance(inline, CommonQuestionInline)
            ]
        return inline_instances

    def deploy_to_selected_teams(self, *, question_set, teams):
        return deploy_common_question_set_to_teams(
            question_set=question_set,
            teams=teams,
        )

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        target_teams = form.cleaned_data.get('target_teams')
        if form.cleaned_data.get('deploy_after_save'):
            if not form.instance.questions.exists():
                self.message_user(
                    request,
                    '문항이 없어 배포하지 않았습니다. 문항을 추가한 뒤 다시 배포하세요.',
                    messages.WARNING,
                )
                return
            results = self.deploy_to_selected_teams(
                question_set=form.instance,
                teams=target_teams,
            )
            created_count = sum(
                (
                    result.status == CommonQuestionSetDeployment.Status.CREATED
                    and result.message == '선택 대상 팀에 신규 설문 생성'
                )
                for result in results
            )
            updated_count = sum(
                (
                    result.status == CommonQuestionSetDeployment.Status.CREATED
                    and result.message == '공통 설문 변경사항으로 새 버전 생성'
                )
                for result in results
            )
            skipped_count = sum(
                result.status == CommonQuestionSetDeployment.Status.SKIPPED
                for result in results
            )
            failed_count = sum(
                result.status == CommonQuestionSetDeployment.Status.FAILED
                for result in results
            )
            skipped_reasons = sorted(
                {
                    result.message
                    for result in results
                    if (
                        result.status == CommonQuestionSetDeployment.Status.SKIPPED
                        and result.message
                    )
                }
            )
            message = (
                f'선택 배포 완료: 신규 {created_count}팀, '
                f'새 버전 갱신 {updated_count}팀, '
                f'건너뜀 {skipped_count}팀, 실패 {failed_count}팀'
            )
            if skipped_reasons:
                message += f' (건너뜀 사유: {", ".join(skipped_reasons)})'
            self.message_user(
                request,
                message,
                level=messages.SUCCESS if failed_count == 0 else messages.WARNING,
            )

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                '<path:object_id>/clone/',
                self.admin_site.admin_view(self.clone_view),
                name='surveys_commonquestionset_clone',
            ),
        ]
        return custom_urls + urls

    def clone_view(self, request, object_id):
        question_set = self.get_object(request, unquote(object_id))
        if question_set is None:
            raise Http404
        if not (
            self.has_view_permission(request, question_set)
            and self.has_add_permission(request)
        ):
            raise PermissionDenied

        form = CommonQuestionSetCloneForm(request.POST or None)
        if request.method == 'POST' and form.is_valid():
            clones = clone_common_question_set_to_days(
                source=question_set,
                day_numbers=form.cleaned_data['target_days'],
            )
            links = format_html_join(
                ', ',
                '<a href="{}">{}</a>',
                (
                    (
                        reverse('admin:surveys_commonquestionset_change', args=[clone.pk]),
                        clone.name,
                    )
                    for clone in clones
                ),
            )
            self.message_user(
                request,
                format_html(
                    '공통 설문 {}개를 복제했습니다: {}. 복제된 설문을 각각 열어 대상 팀에 배포하세요.',
                    len(clones),
                    links,
                ),
                messages.SUCCESS,
            )
            return redirect(reverse('admin:surveys_commonquestionset_changelist'))

        context = {
            **self.admin_site.each_context(request),
            'title': '공통 설문 복제',
            'opts': self.model._meta,
            'original': question_set,
            'form': form,
            'section_count': question_set.sections.count(),
            'question_count': question_set.questions.count(),
            'exclusion_count': question_set.team_exclusions.count(),
        }
        return TemplateResponse(
            request,
            'admin/surveys/commonquestionset/clone.html',
            context,
        )

    def _deploy_to_team(self, *, question_set, team):
        return deploy_common_question_set_to_team(
            question_set=question_set,
            team=team,
        )


@admin.register(CommonQuestionSection)
class CommonQuestionSectionAdmin(admin.ModelAdmin):
    list_display = ('question_set', 'title', 'section_type', 'display_order')
    list_filter = ('section_type', 'question_set')
    search_fields = ('title', 'question_set__name', 'question_set__title')
    ordering = ('question_set__name', 'question_set__day_no', 'display_order')
    fieldsets = (
        ('상위 세트 선택', {'fields': ('question_set',), 'description': '1) CommonQuestionSet을 먼저 생성하세요.'}),
        ('섹션 정보', {'fields': ('title', 'title_en', 'section_type', 'display_order'), 'description': '2) 같은 세트에 섹션을 순서대로 추가하세요.'}),
    )

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        field = super().formfield_for_foreignkey(db_field, request, **kwargs)
        disable_related_widget(field)
        return field


@admin.register(CommonQuestion)
class CommonQuestionAdmin(admin.ModelAdmin):
    form = CommonQuestionForm
    list_display = ('section', 'label', 'question_type', 'is_required', 'display_order')
    list_filter = ('question_type', 'is_required', 'section')
    search_fields = ('key', 'label', 'section__title', 'section__question_set__name')
    ordering = (
        'section__question_set__name',
        'section__question_set__day_no',
        'section__display_order',
        'display_order',
    )
    fieldsets = (
        ('상위 섹션 선택', {'fields': ('section',), 'description': '1) 먼저 상위 섹션을 선택하세요.'}),
        ('문항 기본값', {'fields': ('key', 'label', 'label_en', 'graph_label', 'graph_label_en', 'question_type', 'is_required', 'display_order'), 'description': '2) 문항 키와 한·영 문항 문구, 결과 표시명, 유형, 필수 여부, 표시 순서를 입력하세요.'}),
        ('선택형 보기', {'fields': ('option_lines', 'option_lines_en'), 'description': "3) 한국어는 '보기문구|점수', 영어는 같은 순서의 문구를 줄바꿈으로 입력하세요."}),
    )

    def get_fieldsets(self, request, obj=None):
        base = list(super().get_fieldsets(request, obj))
        if request.user.is_superuser:
            base.append(
                (
                    '고급 설정(슈퍼유저)',
                    {
                        'fields': ('options_json',),
                        'description': '내부 저장 JSON 직접 수정이 필요한 경우에만 사용하세요.',
                    },
                )
            )
        return tuple(base)

    def get_readonly_fields(self, request, obj=None):
        return ('key',) if obj else ()

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        field = super().formfield_for_foreignkey(db_field, request, **kwargs)
        disable_related_widget(field)
        return field


@admin.register(TeamCommonQuestionExclusion)
class TeamCommonQuestionExclusionAdmin(HiddenAdminMenuMixin, admin.ModelAdmin):
    list_display = ('team', 'question_set', 'question_key', 'updated_at')
    list_filter = ('team', 'question_set')
    search_fields = ('team__name', 'team__code', 'question_set__name', 'question_key')

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        field = super().formfield_for_foreignkey(db_field, request, **kwargs)
        disable_related_widget(field)
        return field



@admin.register(DocumentSubmission)
class DocumentSubmissionAdmin(admin.ModelAdmin):
    list_display = (
        'agreed_at',
        'team_name_snapshot',
        'organization_name_snapshot',
        'participant_name_snapshot',
        'participant_birth_date_snapshot',
        'participant_phone_snapshot',
        'participant_gender_snapshot',
        'document_type',
        'agreed',
        'template_version',
        'snapshot_source',
        'bundle_uuid_short',
        'confirmation_pdf_link',
    )
    list_filter = ('team', 'document_type', 'agreed', 'snapshot_source', 'agreed_at')
    search_fields = (
        'team__name',
        'team__organization_name',
        'respondent__name',
        'respondent__phone',
        'signer_name',
        'bundle__bundle_uuid',
        'bundle__participant_snapshot_json__name',
        'bundle__participant_snapshot_json__phone',
        'bundle__team_snapshot_json__team_name',
        'bundle__team_snapshot_json__organization_name',
    )
    date_hierarchy = 'agreed_at'
    readonly_fields = (
        'team_name_snapshot',
        'organization_name_snapshot',
        'participant_snapshot_summary',
        'team_snapshot_summary',
        'bundle_uuid_display',
        'document_type',
        'document_title_snapshot',
        'template_version',
        'document_content_hash_at_submit',
        'agreed',
        'agreed_at',
        'signature_method',
        'snapshot_source',
        'document_content_snapshot_display',
        'confirmation_pdf_link',
        'created_at',
        'updated_at',
    )

    def get_urls(self):
        return [
            path(
                '<path:object_id>/confirmation.pdf',
                self.admin_site.admin_view(self.confirmation_pdf_view),
                name='surveys_documentsubmission_confirmation_pdf',
            ),
        ] + super().get_urls()

    def confirmation_pdf_view(self, request, object_id):
        submission = self.get_object(request, object_id)
        if submission is None:
            return redirect(reverse('admin:surveys_documentsubmission_changelist'))
        if not self.has_view_permission(request, submission):
            raise PermissionDenied
        requested_at = timezone.now()
        try:
            content = render_consent_confirmation_pdf(submission)
        except ConsentExportError as exc:
            ConsentExportAudit.objects.create(
                admin_user=request.user,
                team=submission.team,
                team_name=submission.team.name,
                export_format=ConsentExportAudit.ExportFormat.PDF,
                scope='individual',
                submission_count=1,
                result=ConsentExportAudit.Result.FAILED,
                error_message=str(exc)[:500],
                requested_at=requested_at,
            )
            record_admin_download(
                request, team=submission.team, resource_type='required_documents',
                export_format='pdf', scope='individual', record_count=1,
                result='failed', error_message=str(exc),
            )
            messages.error(request, f'전자 동의 확인서를 생성하지 못했습니다: {exc}')
            return redirect(reverse('admin:surveys_documentsubmission_change', args=[submission.pk]))
        ConsentExportAudit.objects.create(
            admin_user=request.user,
            team=submission.team,
            team_name=submission.team.name,
            export_format=ConsentExportAudit.ExportFormat.PDF,
            scope='individual',
            submission_count=1,
            result=ConsentExportAudit.Result.SUCCESS,
            requested_at=requested_at,
        )
        record_admin_download(
            request, team=submission.team, resource_type='required_documents',
            export_format='pdf', scope='individual', record_count=1, result='success',
        )
        response = HttpResponse(content, content_type='application/pdf')
        response['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote(consent_pdf_filename(submission))}"
        response['Content-Length'] = str(len(content))
        return response
    fields = (
        'bundle_uuid_display',
        'team_name_snapshot',
        'organization_name_snapshot',
        'participant_snapshot_summary',
        'team_snapshot_summary',
        'document_type',
        'document_title_snapshot',
        'template_version',
        'document_content_hash_at_submit',
        'agreed',
        'agreed_at',
        'signature_method',
        'snapshot_source',
        'document_content_snapshot_display',
        'confirmation_pdf_link',
        'created_at',
        'updated_at',
    )

    def get_queryset(self, request):
        queryset = super().get_queryset(request).select_related('team', 'respondent', 'bundle')
        if request.user.is_superuser:
            return queryset
        return queryset.filter(team_id__in=allowed_team_ids(request.user))

    def _can_access_team(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if obj is None:
            return bool(allowed_team_ids(request.user))
        return obj.team_id in allowed_team_ids(request.user)

    def has_view_permission(self, request, obj=None):
        return self._can_access_team(request, obj)

    def has_module_permission(self, request):
        return self._can_access_team(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return self._can_access_team(request, obj)

    def _participant_snapshot(self, obj):
        if obj.bundle_id and obj.bundle.participant_snapshot_json:
            return obj.bundle.participant_snapshot_json
        return {
            'name': obj.respondent.name,
            'birth_date': obj.respondent.birth_date.isoformat(),
            'phone': obj.respondent.phone,
            'gender': obj.respondent.gender or '',
        }

    def _team_snapshot(self, obj):
        if obj.bundle_id and obj.bundle.team_snapshot_json:
            return obj.bundle.team_snapshot_json
        return {
            'team_name': obj.team.name,
            'organization_name': obj.team.organization_name,
            'organization_name_en': obj.team.organization_name_en,
        }

    @admin.display(description='팀')
    def team_name_snapshot(self, obj):
        return self._team_snapshot(obj).get('team_name') or obj.team.name

    @admin.display(description='운영기관')
    def organization_name_snapshot(self, obj):
        return self._team_snapshot(obj).get('organization_name') or obj.team.organization_name

    @admin.display(description='참가자')
    def participant_name_snapshot(self, obj):
        return self._participant_snapshot(obj).get('name') or obj.respondent.name

    @admin.display(description='생년월일')
    def participant_birth_date_snapshot(self, obj):
        return self._participant_snapshot(obj).get('birth_date') or obj.respondent.birth_date

    @admin.display(description='휴대전화')
    def participant_phone_snapshot(self, obj):
        return self._participant_snapshot(obj).get('phone') or obj.respondent.phone

    @admin.display(description='성별')
    def participant_gender_snapshot(self, obj):
        value = self._participant_snapshot(obj).get('gender')
        return dict(Respondent._meta.get_field('gender').choices).get(value, value or '-')

    @admin.display(description='묶음 UUID')
    def bundle_uuid_short(self, obj):
        return str(obj.bundle.bundle_uuid)[:8] if obj.bundle_id else '-'

    @admin.display(description='묶음 UUID')
    def bundle_uuid_display(self, obj):
        return str(obj.bundle.bundle_uuid) if obj.bundle_id else '-'

    @admin.display(description='참가자 제출 당시 정보')
    def participant_snapshot_summary(self, obj):
        snapshot = self._participant_snapshot(obj)
        gender = dict(Respondent._meta.get_field('gender').choices).get(snapshot.get('gender'), snapshot.get('gender') or '-')
        return format_html(
            '이름: {}<br>생년월일: {}<br>휴대전화: {}<br>성별: {}',
            snapshot.get('name', '-'),
            snapshot.get('birth_date', '-'),
            snapshot.get('phone', '-'),
            gender,
        )

    @admin.display(description='팀 제출 당시 정보')
    def team_snapshot_summary(self, obj):
        snapshot = self._team_snapshot(obj)
        return format_html(
            '팀 ID: {}<br>팀명: {}<br>운영기관: {}<br>운영기관(영문): {}',
            snapshot.get('team_id', obj.team_id),
            snapshot.get('team_name', '-'),
            snapshot.get('organization_name', '-'),
            snapshot.get('organization_name_en', '-'),
        )

    @admin.display(description='문서 본문 스냅샷')
    def document_content_snapshot_display(self, obj):
        return format_html(
            '<pre style="white-space:pre-wrap;max-width:1000px">{}</pre>',
            obj.document_content_snapshot or '복원할 수 없는 기존 제출입니다.',
        )

    @admin.display(description='전자 동의 확인서')
    def confirmation_pdf_link(self, obj):
        if not obj or not obj.pk:
            return '-'
        return format_html(
            '<a href="{}">PDF 다운로드</a>',
            reverse('admin:surveys_documentsubmission_confirmation_pdf', args=[obj.pk]),
        )


@admin.register(RequiredDocumentConsentBundle)
class RequiredDocumentConsentBundleAdmin(admin.ModelAdmin):
    list_per_page = 50
    list_display = (
        'participant_name_snapshot',
        'participant_birth_date_snapshot',
        'participant_phone_snapshot',
        'participant_gender_snapshot',
        'organization_name_snapshot',
        'team_name_snapshot',
        'privacy_consent_status',
        'third_party_consent_status',
        'safety_pledge_status',
        'overall_completion_status',
        'completed_at',
    )
    list_filter = ('team', 'snapshot_source', 'completed_at', 'created_at')
    search_fields = (
        'bundle_uuid',
        'team__name',
        'team__organization_name',
        'respondent__name',
        'respondent__phone',
        'participant_snapshot_json__name',
        'participant_snapshot_json__phone',
        'team_snapshot_json__team_name',
        'team_snapshot_json__organization_name',
    )
    readonly_fields = (
        'bundle_uuid',
        'team',
        'respondent',
        'participant_snapshot_summary',
        'team_snapshot_summary',
        'enabled_document_types_summary',
        'document_submission_summary',
        'snapshot_schema_version',
        'snapshot_source',
        'created_at',
        'completed_at',
        'updated_at',
    )
    fields = readonly_fields
    actions = ('export_selected_excel', 'export_selected_confirmation_zip')

    def _write_export_audit(self, request, bundles, *, export_format, result, error_message=''):
        team_ids = {bundle.team_id for bundle in bundles}
        team = bundles[0].team if len(team_ids) == 1 else None
        ConsentExportAudit.objects.create(
            admin_user=request.user,
            team=team,
            team_name=team.name if team else '복수 팀',
            export_format=export_format,
            scope='selected',
            submission_count=sum(len(getattr(bundle, 'admin_document_submissions', [])) for bundle in bundles),
            result=result,
            error_message=error_message[:500],
            requested_at=timezone.now(),
        )
        record_admin_download(
            request,
            team=team,
            resource_type='required_documents',
            export_format=export_format,
            scope='selected',
            record_count=sum(len(getattr(bundle, 'admin_document_submissions', [])) for bundle in bundles),
            result=result,
            error_message=error_message,
        )

    @admin.action(description='선택한 제출자의 동의서 현황 Excel 다운로드')
    def export_selected_excel(self, request, queryset):
        bundles = list(queryset)
        try:
            content = build_consent_workbook(bundles)
        except ConsentExportError as exc:
            self._write_export_audit(
                request, bundles,
                export_format=ConsentExportAudit.ExportFormat.EXCEL,
                result=ConsentExportAudit.Result.FAILED,
                error_message=str(exc),
            )
            self.message_user(request, str(exc), level=messages.ERROR)
            return None
        self._write_export_audit(
            request, bundles,
            export_format=ConsentExportAudit.ExportFormat.EXCEL,
            result=ConsentExportAudit.Result.SUCCESS,
        )
        response = HttpResponse(
            content,
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote(consent_excel_filename(bundles))}"
        return response

    @admin.action(description='선택한 제출자의 전자 동의 확인서 ZIP 다운로드')
    def export_selected_confirmation_zip(self, request, queryset):
        bundles = list(queryset)
        submissions = list(
            DocumentSubmission.objects.filter(bundle__in=bundles)
            .select_related('team', 'respondent', 'bundle')
            .order_by('team__name', 'respondent__name', 'document_type')
        )
        try:
            archive = build_consent_zip(submissions, file_prefix='선택_동의서')
        except ConsentExportError as exc:
            self._write_export_audit(
                request, bundles,
                export_format=ConsentExportAudit.ExportFormat.ZIP,
                result=ConsentExportAudit.Result.FAILED,
                error_message=str(exc),
            )
            self.message_user(request, str(exc), level=messages.ERROR)
            return None
        self._write_export_audit(
            request, bundles,
            export_format=ConsentExportAudit.ExportFormat.ZIP,
            result=ConsentExportAudit.Result.SUCCESS,
        )
        response = HttpResponse(archive.content, content_type='application/zip')
        response['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote(archive.file_name)}"
        response['Content-Length'] = str(len(archive.content))
        return response

    def changelist_view(self, request, extra_context=None):
        return super().changelist_view(
            request,
            extra_context={
                **(extra_context or {}),
                'title': '동의서 제출자 현황',
            },
        )

    def get_queryset(self, request):
        queryset = (
            super()
            .get_queryset(request)
            .select_related('team', 'respondent')
            .prefetch_related(
                Prefetch(
                    'submissions',
                    queryset=DocumentSubmission.objects.order_by('agreed_at', 'id'),
                    to_attr='admin_document_submissions',
                )
            )
            .order_by('-completed_at', '-created_at')
        )
        if request.user.is_superuser:
            return queryset
        return queryset.filter(team_id__in=allowed_team_ids(request.user))

    def _can_access_team(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if obj is None:
            return bool(allowed_team_ids(request.user))
        return obj.team_id in allowed_team_ids(request.user)

    def has_view_permission(self, request, obj=None):
        return self._can_access_team(request, obj)

    def has_module_permission(self, request):
        return self._can_access_team(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description='묶음 UUID')
    def bundle_uuid_short(self, obj):
        return str(obj.bundle_uuid)[:8]

    def _participant_snapshot(self, obj):
        snapshot = obj.participant_snapshot_json or {}
        return {
            'name': snapshot.get('name') or obj.respondent.name,
            'birth_date': snapshot.get('birth_date') or obj.respondent.birth_date.isoformat(),
            'phone': snapshot.get('phone') or obj.respondent.phone,
            'gender': snapshot.get('gender') or obj.respondent.gender or '',
        }

    def _team_snapshot(self, obj):
        snapshot = obj.team_snapshot_json or {}
        return {
            'team_name': snapshot.get('team_name') or obj.team.name,
            'organization_name': snapshot.get('organization_name') or obj.team.organization_name,
            'organization_name_en': snapshot.get('organization_name_en') or obj.team.organization_name_en,
        }

    def _submission_types(self, obj):
        submissions = getattr(obj, 'admin_document_submissions', None)
        if submissions is None:
            submissions = obj.submissions.all()
        return {submission.document_type for submission in submissions}

    def _document_status(self, obj, document_type):
        if document_type not in obj.enabled_document_types_snapshot:
            return '미사용'
        return '제출 완료' if document_type in self._submission_types(obj) else '미제출'

    @admin.display(description='참가자')
    def participant_name_snapshot(self, obj):
        return self._participant_snapshot(obj)['name']

    @admin.display(description='생년월일')
    def participant_birth_date_snapshot(self, obj):
        return self._participant_snapshot(obj)['birth_date']

    @admin.display(description='휴대전화')
    def participant_phone_snapshot(self, obj):
        return self._participant_snapshot(obj)['phone']

    @admin.display(description='성별')
    def participant_gender_snapshot(self, obj):
        value = self._participant_snapshot(obj)['gender']
        return dict(Respondent._meta.get_field('gender').choices).get(value, value or '-')

    @admin.display(description='운영기관')
    def organization_name_snapshot(self, obj):
        return self._team_snapshot(obj)['organization_name']

    @admin.display(description='팀')
    def team_name_snapshot(self, obj):
        return self._team_snapshot(obj)['team_name']

    @admin.display(description='개인정보 동의')
    def privacy_consent_status(self, obj):
        return self._document_status(obj, DocumentTemplate.DocumentType.PRIVACY_CONSENT)

    @admin.display(description='제3자 제공 동의')
    def third_party_consent_status(self, obj):
        return self._document_status(obj, DocumentTemplate.DocumentType.THIRD_PARTY_CONSENT)

    @admin.display(description='안전수칙 서약')
    def safety_pledge_status(self, obj):
        return self._document_status(obj, DocumentTemplate.DocumentType.SAFETY_PLEDGE)

    @admin.display(description='전체 상태')
    def overall_completion_status(self, obj):
        required_types = set(obj.enabled_document_types_snapshot)
        return (
            '전체 제출 완료'
            if obj.completed_at and required_types.issubset(self._submission_types(obj))
            else '미완료'
        )

    @admin.display(description='참가자 제출 당시 정보')
    def participant_snapshot_summary(self, obj):
        snapshot = self._participant_snapshot(obj)
        gender = dict(Respondent._meta.get_field('gender').choices).get(snapshot.get('gender'), snapshot.get('gender') or '-')
        return format_html(
            '이름: {}<br>생년월일: {}<br>휴대전화: {}<br>성별: {}',
            snapshot.get('name', '-'),
            snapshot.get('birth_date', '-'),
            snapshot.get('phone', '-'),
            gender,
        )

    @admin.display(description='팀 제출 당시 정보')
    def team_snapshot_summary(self, obj):
        snapshot = {
            'team_id': (obj.team_snapshot_json or {}).get('team_id', obj.team_id),
            **self._team_snapshot(obj),
        }
        return format_html(
            '팀 ID: {}<br>팀명: {}<br>운영기관: {}<br>운영기관(영문): {}',
            snapshot.get('team_id', '-'),
            snapshot.get('team_name', '-'),
            snapshot.get('organization_name', '-'),
            snapshot.get('organization_name_en', '-'),
        )

    @admin.display(description='활성 문서 유형')
    def enabled_document_types_summary(self, obj):
        labels = dict(DocumentTemplate.DocumentType.choices)
        return ', '.join(labels.get(value, value) for value in obj.enabled_document_types_snapshot) or '-'

    @admin.display(description='문서별 제출 내역')
    def document_submission_summary(self, obj):
        submissions = getattr(obj, 'admin_document_submissions', None)
        if submissions is None:
            submissions = list(obj.submissions.order_by('agreed_at', 'id'))
        if not submissions:
            return '제출된 문서가 없습니다.'
        labels = dict(DocumentTemplate.DocumentType.choices)
        return format_html(
            '<ul style="margin:0;padding-left:18px">{}</ul>',
            format_html_join(
                '',
                '<li><a href="{}">{}</a> · {} · {}</li>',
                (
                    (
                        reverse('admin:surveys_documentsubmission_change', args=[submission.pk]),
                        labels.get(submission.document_type, submission.document_type),
                        '동의' if submission.agreed else '미동의',
                        submission.agreed_at,
                    )
                    for submission in submissions
                ),
            ),
        )


@admin.register(ReflectionSubmission)
class ReflectionSubmissionAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        'team',
        'respondent',
        'affiliation',
        'country',
        'title',
        'submitted_at',
        'pdf_status',
        'generated_at',
        'first_downloaded_at',
        'purge_after',
        'is_locked',
        'team_zip_download_link',
    )
    list_filter = ('team', 'pdf_status', 'is_locked', 'submitted_at')
    search_fields = (
        'respondent__name',
        'respondent__normalized_phone',
        'affiliation',
        'country',
        'title',
        'content',
    )
    date_hierarchy = 'submitted_at'
    readonly_fields = ('team_zip_download_link',)

    @admin.display(description='소감문 다운로드')
    def team_zip_download_link(self, obj):
        if not obj or not obj.team_id:
            return '-'
        return format_html(
            '<a href="{}">{} 팀 ZIP 다운로드</a>',
            reverse('admin:teams_team_reflection_zip', args=[obj.team_id]),
            obj.team.name,
        )


@admin.register(ReflectionDownloadAudit)
class ReflectionDownloadAuditAdmin(
    HiddenAdminMenuMixin,
    SuperuserOnlyAdminMixin,
    ReadOnlyAdminMixin,
    admin.ModelAdmin,
):
    list_display = (
        'requested_at',
        'admin_user',
        'team_name',
        'submission_count',
        'result',
    )
    list_filter = ('result', 'requested_at')
    search_fields = ('admin_user__username', 'team_name', 'error_message')
    date_hierarchy = 'requested_at'


@admin.register(ConsentExportAudit)
class ConsentExportAuditAdmin(
    HiddenAdminMenuMixin,
    SuperuserOnlyAdminMixin,
    ReadOnlyAdminMixin,
    admin.ModelAdmin,
):
    list_display = (
        'requested_at',
        'admin_user',
        'team_name',
        'export_format',
        'scope',
        'submission_count',
        'result',
    )
    list_filter = ('export_format', 'scope', 'result', 'requested_at')
    search_fields = ('admin_user__username', 'team_name', 'error_message')
    date_hierarchy = 'requested_at'


@admin.register(AdminDownloadAudit)
class AdminDownloadAuditAdmin(
    ReadOnlyAdminMixin,
    SuperuserOnlyAdminMixin,
    admin.ModelAdmin,
):
    list_display = (
        'requested_at', 'admin_user', 'team_name', 'resource_type',
        'export_format', 'scope', 'record_count', 'result',
    )
    list_filter = ('resource_type', 'export_format', 'scope', 'result', 'requested_at')
    search_fields = ('admin_user__username', 'team_name', 'endpoint', 'error_message')
    date_hierarchy = 'requested_at'


@admin.register(AnonymousSurveySubmission)
class AnonymousSurveySubmissionAdmin(
    HiddenAdminMenuMixin,
    ReadOnlyAdminMixin,
    admin.ModelAdmin,
):
    list_display = ('id', 'team', 'survey_form', 'form_version', 'submitted_at')
    list_filter = ('team', 'survey_form', 'submitted_at')
    search_fields = ('team__name', 'team__code', 'survey_form__title')
    date_hierarchy = 'submitted_at'

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('team', 'survey_form')


@admin.register(ExitSurveySubmission)
class ExitSurveySubmissionAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        'id',
        'team',
        'respondent',
        'survey_form',
        'form_version',
        'submitted_at',
        'is_locked',
    )
    list_filter = ('team', 'survey_form', 'form_version', 'submitted_at')
    search_fields = (
        'team__name',
        'team__code',
        'respondent__name',
        'respondent__phone',
        'survey_form__title',
    )
    date_hierarchy = 'submitted_at'
    actions = ('export_survey_results_excel',)
    readonly_fields = (
        'team',
        'respondent',
        'survey_form',
        'form_version',
        'is_locked',
        'submitted_at',
        'answer_overview',
    )
    fields = readonly_fields

    def get_queryset(self, request):
        queryset = super().get_queryset(request).select_related(
            'team',
            'respondent',
            'survey_form',
        )
        if request.user.is_superuser:
            return queryset
        return queryset.filter(team_id__in=allowed_team_ids(request.user))

    @admin.display(description='답변 요약')
    def answer_overview(self, obj):
        answers = obj.answers_json or {}
        items = []
        snapshot_keys = set()
        for section in (obj.form_snapshot_json or {}).get('sections') or []:
            for question in section.get('questions') or []:
                key = str(question.get('key') or '')
                if not key:
                    continue
                snapshot_keys.add(key)
                items.append(
                    (
                        question.get('label') or key,
                        format_submission_answer(answers.get(key)),
                    )
                )
        for key, value in answers.items():
            if key not in snapshot_keys:
                items.append((key, format_submission_answer(value)))
        if not items:
            return '-'
        return format_html(
            '<ul style="margin:0; padding-left:18px;">{}</ul>',
            format_html_join(
                '',
                '<li><strong>{}</strong>: {}</li>',
                items,
            ),
        )

    @admin.action(
        description='선택한 설문 제출 결과를 Excel로 다운로드',
        permissions=['view'],
    )
    def export_survey_results_excel(self, request, queryset):
        team_ids = list(
            queryset.order_by()
            .values_list('team_id', flat=True)
            .distinct()
        )
        if len(team_ids) != 1:
            record_admin_download(
                request,
                resource_type='exit_survey',
                export_format='xlsx',
                scope='selected',
                record_count=queryset.count(),
                result='failed',
                error_message='한 번에 한 팀의 제출 건만 선택해야 합니다.',
            )
            self.message_user(
                request,
                '한 번에 한 팀의 제출 건만 선택해 주세요.',
                level=messages.ERROR,
            )
            return None

        team = Team.objects.get(pk=team_ids[0])
        selected_submissions = queryset.filter(
            team=team,
            is_locked=True,
        ).select_related('survey_form').order_by(
            'survey_form_id',
            'form_version',
            'submitted_at',
            'id',
        )
        workbook = build_survey_results_workbook(
            team=team,
            submissions=selected_submissions,
        )
        filename = survey_results_filename(
            organization_name=team.organization_name,
            team_name=team.name,
        )
        response = HttpResponse(
            workbook.getvalue(),
            content_type=(
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            ),
        )
        response['Content-Disposition'] = (
            f"attachment; filename*=UTF-8''{quote(filename)}"
        )
        response['X-Content-Type-Options'] = 'nosniff'
        record_admin_download(
            request,
            team=team,
            resource_type='exit_survey',
            export_format='xlsx',
            scope='selected',
            record_count=selected_submissions.count(),
            result='success',
        )
        return response

@admin.register(CommonQuestionSetDeployment)
class CommonQuestionSetDeploymentAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('question_set', 'team', 'survey_form', 'status', 'message', 'deployed_at')
    list_filter = ('status', 'question_set', 'team', 'deployed_at')
    search_fields = ('question_set__name', 'question_set__title', 'team__name', 'team__code', 'message')
    date_hierarchy = 'deployed_at'
