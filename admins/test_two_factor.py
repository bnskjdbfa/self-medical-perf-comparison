from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django_otp import oath
from django_otp.plugins.otp_totp.models import TOTPDevice

from admins.models import AdminRecoveryCode
from admins.two_factor import generate_recovery_codes


@override_settings(ADMIN_2FA_REQUIRED=True)
class AdminTwoFactorTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("admin2fa", "admin@example.com", "pass")

    def current_token(self, device):
        return oath.totp(
            device.bin_key,
            step=device.step,
            t0=device.t0,
            digits=device.digits,
            drift=device.drift,
        )

    def test_anonymous_admin_login_remains_accessible(self):
        response = self.client.get(reverse("admin:login"))
        self.assertEqual(response.status_code, 200)

    def test_password_only_admin_is_sent_to_setup(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("admin:index"))
        self.assertRedirects(
            response,
            f"{reverse('admins:two-factor-setup')}?next=%2Fadmin%2F",
            fetch_redirect_response=False,
        )

    def test_setup_confirms_device_and_shows_one_time_recovery_codes(self):
        self.client.force_login(self.user)
        setup = self.client.get(reverse("admins:two-factor-setup"))
        self.assertEqual(setup.status_code, 200)
        device = TOTPDevice.objects.get(user=self.user, confirmed=False)
        response = self.client.post(
            reverse("admins:two-factor-setup"),
            {"token": str(self.current_token(device)), "next": reverse("admin:index")},
        )
        self.assertRedirects(response, reverse("admins:two-factor-recovery-codes"), fetch_redirect_response=False)
        device.refresh_from_db()
        self.assertTrue(device.confirmed)
        self.assertEqual(AdminRecoveryCode.objects.filter(user=self.user).count(), 10)
        codes_page = self.client.get(reverse("admins:two-factor-recovery-codes"))
        self.assertContains(codes_page, "복구 코드를 보관해주세요")
        second_view = self.client.get(reverse("admins:two-factor-recovery-codes"))
        self.assertRedirects(second_view, reverse("admin:index"), fetch_redirect_response=False)

    def test_existing_device_can_verify(self):
        device = TOTPDevice.objects.create(user=self.user, name="phone", confirmed=True)
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("admins:two-factor-verify"),
            {"token": str(self.current_token(device)), "next": reverse("admin:index")},
        )
        self.assertRedirects(response, reverse("admin:index"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)

    def test_recovery_code_is_single_use(self):
        TOTPDevice.objects.create(user=self.user, name="phone", confirmed=True)
        raw_code = generate_recovery_codes(self.user, count=1)[0]
        self.client.force_login(self.user)
        response = self.client.post(reverse("admins:two-factor-verify"), {"token": raw_code})
        self.assertEqual(response.status_code, 302)
        recovery = AdminRecoveryCode.objects.get(user=self.user)
        self.assertIsNotNone(recovery.used_at)

        self.client.logout()
        self.client.force_login(self.user)
        response = self.client.post(reverse("admins:two-factor-verify"), {"token": raw_code})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "올바르지 않습니다")

    def test_ops_route_is_also_protected(self):
        TOTPDevice.objects.create(user=self.user, name="phone", confirmed=True)
        self.client.force_login(self.user)
        response = self.client.get(reverse("admins:prototype-manager"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("admins:two-factor-verify")))

    @override_settings(ADMIN_2FA_REQUIRED=False)
    def test_rollout_switch_can_temporarily_disable_enforcement(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)
