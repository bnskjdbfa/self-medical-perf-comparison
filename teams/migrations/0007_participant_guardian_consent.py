from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('teams', '0006_team_reflection_image'),
    ]

    operations = [
        migrations.AddField(
            model_name='participant',
            name='guardian_consent_document_version',
            field=models.CharField(blank=True, max_length=40, verbose_name='법정대리인 동의서 버전'),
        ),
        migrations.AddField(
            model_name='participant',
            name='guardian_consent_method',
            field=models.CharField(blank=True, choices=[('written', '서면 동의서')], max_length=20, verbose_name='법정대리인 동의 확인방법'),
        ),
        migrations.AddField(
            model_name='participant',
            name='guardian_consent_verified_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='법정대리인 동의 확인일시'),
        ),
        migrations.AddField(
            model_name='participant',
            name='guardian_consent_verified_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='verified_guardian_consents', to=settings.AUTH_USER_MODEL, verbose_name='법정대리인 동의 확인자'),
        ),
    ]
