from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("admins", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="AdminRecoveryCode",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("token_hash", models.CharField(max_length=128)),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="admin_recovery_codes", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "관리자 2단계 인증 복구 코드",
                "verbose_name_plural": "관리자 2단계 인증 복구 코드",
                "db_table": "admin_recovery_codes",
                "indexes": [models.Index(fields=["user", "used_at"], name="adm_recovery_user_used_idx")],
            },
        ),
    ]
