from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.models import Group, User

from .models import AdminTeamPermission


class ReadOnlyAdminMixin:
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]


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


@admin.register(AdminTeamPermission)
class AdminTeamPermissionAdmin(SuperuserOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("id", "user", "team", "role", "updated_at")
    list_filter = ("role", "team")
    search_fields = ("user__username", "team__name", "team__code")

try:
    admin.site.unregister(User)
except admin.sites.NotRegistered:
    pass

try:
    admin.site.unregister(Group)
except admin.sites.NotRegistered:
    pass


@admin.register(User)
class SuperuserOnlyUserAdmin(SuperuserOnlyAdminMixin, UserAdmin):
    pass


@admin.register(Group)
class SuperuserOnlyGroupAdmin(SuperuserOnlyAdminMixin, GroupAdmin):
    pass
