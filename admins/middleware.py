from urllib.parse import urlencode

from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse

from .two_factor import has_confirmed_device


class AdminTwoFactorMiddleware:
    exempt_paths = {"/admin/login/", "/admin/logout/", "/ops/login/", "/ops/logout/"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._requires_verification(request):
            route = "admins:two-factor-verify" if has_confirmed_device(request.user) else "admins:two-factor-setup"
            destination = f"{reverse(route)}?{urlencode({'next': request.get_full_path()})}"
            return redirect(destination)
        return self.get_response(request)

    def _requires_verification(self, request) -> bool:
        if not settings.ADMIN_2FA_REQUIRED:
            return False
        path = request.path
        if not (path.startswith("/admin/") or path.startswith("/ops/")):
            return False
        if path in self.exempt_paths or path.startswith("/ops/2fa/"):
            return False
        user = request.user
        if not user.is_authenticated or not (user.is_staff or user.is_superuser):
            return False
        return not user.is_verified()
