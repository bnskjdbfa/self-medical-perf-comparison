from django.apps import AppConfig


class AdminsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "admins"
    verbose_name = '운영 관리자'
