from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('surveys', '0020_consent_export_audit'),
    ]

    operations = [
        migrations.CreateModel(
            name='AdminDownloadAudit',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('team_name', models.CharField(blank=True, max_length=100)),
                ('resource_type', models.CharField(max_length=40)),
                ('export_format', models.CharField(max_length=10)),
                ('scope', models.CharField(max_length=30)),
                ('record_count', models.PositiveIntegerField(default=0)),
                ('endpoint', models.CharField(blank=True, max_length=255)),
                ('filters_json', models.JSONField(blank=True, default=dict)),
                ('result', models.CharField(choices=[('success', '성공'), ('failed', '실패')], max_length=20)),
                ('error_message', models.CharField(blank=True, max_length=500)),
                ('requested_at', models.DateTimeField()),
                ('admin_user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='admin_download_audits', to=settings.AUTH_USER_MODEL)),
                ('team', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='admin_download_audits', to='teams.team')),
            ],
            options={
                'verbose_name': '관리자 다운로드 감사기록',
                'verbose_name_plural': '관리자 다운로드 감사기록',
                'db_table': 'admin_download_audits',
                'indexes': [
                    models.Index(fields=['team', 'requested_at'], name='download_team_at_idx'),
                    models.Index(fields=['admin_user', 'requested_at'], name='download_user_at_idx'),
                    models.Index(fields=['resource_type', 'requested_at'], name='download_type_at_idx'),
                ],
            },
        ),
    ]
