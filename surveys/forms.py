from datetime import datetime

from django import forms
from django.core.exceptions import ValidationError

from teams.models import GenderChoices

from .models import ReflectionSubmission


class TeamCodeForm(forms.Form):
    team_code = forms.CharField(max_length=50, label='팀 코드')


class RespondentInfoForm(forms.Form):
    name = forms.CharField(max_length=50, label='이름')
    birth_date = forms.CharField(max_length=10, label='생년월일')
    gender = forms.ChoiceField(
        required=False,
        label='성별',
        choices=[
            ('', '선택안함'),
            *[
                choice
                for choice in GenderChoices.choices
                if choice[0] != GenderChoices.UNSPECIFIED
            ],
        ],
    )

    def clean_birth_date(self):
        raw_value = (self.cleaned_data.get('birth_date') or '').strip()
        digits_only = ''.join(ch for ch in raw_value if ch.isdigit())

        if len(digits_only) == 6:
            year_prefix = '20' if int(digits_only[:2]) <= 30 else '19'
            try:
                return datetime.strptime(f'{year_prefix}{digits_only}', '%Y%m%d').date()
            except ValueError as exc:
                raise ValidationError('생년월일 6자리를 확인해 주세요.') from exc

        for date_format in ('%Y-%m-%d', '%Y%m%d'):
            try:
                return datetime.strptime(raw_value, date_format).date()
            except ValueError:
                continue

        raise ValidationError('생년월일을 다시 확인해 주세요.')


class V2RespondentInfoForm(RespondentInfoForm):
    gender = forms.ChoiceField(
        required=True,
        label='성별',
        choices=[
            ('', '성별'),
            *[
                choice
                for choice in GenderChoices.choices
                if choice[0] != GenderChoices.UNSPECIFIED
            ],
        ],
        error_messages={'required': '성별을 선택해 주세요.'},
    )


class RequiredDocumentAgreementForm(forms.Form):
    agreed = forms.BooleanField(
        required=True,
        label='문서 내용을 확인했으며 이에 동의합니다.',
        error_messages={'required': '동의 또는 서약 확인이 필요합니다.'},
    )

    def clean_agreed(self):
        if self.data.get('agreed') != 'on':
            raise ValidationError('동의 또는 서약 확인이 필요합니다.')
        return True


class ReflectionSubmissionForm(forms.ModelForm):
    class Meta:
        model = ReflectionSubmission
        fields = ('affiliation', 'country', 'title', 'content')
        widgets = {
            'affiliation': forms.TextInput(attrs={'placeholder': '소속 / school(belong to)'}),
            'country': forms.TextInput(attrs={'placeholder': '국가 / country'}),
            'title': forms.TextInput(attrs={'placeholder': '제목 / Title'}),
            'content': forms.Textarea(
                attrs={
                    'placeholder': (
                        '탐방 프로그램 동안 느낀 점을 작성해주세요.\n'
                        '(Please write down what you felt during the tour program.)'
                    ),
                    'rows': 10,
                }
            ),
        }

    def clean_content(self):
        content = self.cleaned_data['content'].strip()
        if len(content) < 10:
            raise ValidationError('소감문은 10자 이상 입력해 주세요.')
        return content
