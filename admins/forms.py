from django import forms


class TwoFactorTokenForm(forms.Form):
    token = forms.CharField(
        label="인증 코드",
        max_length=32,
        strip=True,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric"}),
    )
