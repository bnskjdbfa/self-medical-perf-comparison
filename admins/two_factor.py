import secrets

from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from .models import AdminRecoveryCode


def confirmed_devices(user):
    return TOTPDevice.objects.filter(user=user, confirmed=True).order_by("id")


def has_confirmed_device(user) -> bool:
    return bool(user.is_authenticated and confirmed_devices(user).exists())


def get_pending_device(user):
    device = TOTPDevice.objects.filter(user=user, confirmed=False).order_by("id").first()
    if device is None:
        device = TOTPDevice.objects.create(user=user, name="관리자 인증 앱", confirmed=False)
    return device


def normalize_recovery_code(value: str) -> str:
    return "".join(character for character in (value or "").lower() if character.isalnum())


@transaction.atomic
def generate_recovery_codes(user, count: int = 10) -> list[str]:
    AdminRecoveryCode.objects.filter(user=user).delete()
    plain_codes = []
    rows = []
    for _ in range(count):
        raw = secrets.token_hex(8)
        displayed = "-".join(raw[index:index + 4] for index in range(0, 16, 4))
        plain_codes.append(displayed)
        rows.append(AdminRecoveryCode(user=user, token_hash=make_password(raw)))
    AdminRecoveryCode.objects.bulk_create(rows)
    return plain_codes


@transaction.atomic
def consume_recovery_code(user, value: str) -> bool:
    normalized = normalize_recovery_code(value)
    if not normalized:
        return False
    codes = AdminRecoveryCode.objects.select_for_update().filter(user=user, used_at__isnull=True)
    for code in codes:
        if check_password(normalized, code.token_hash):
            code.used_at = timezone.now()
            code.save(update_fields=["used_at", "updated_at"])
            return True
    return False
