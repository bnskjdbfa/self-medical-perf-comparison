from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('surveys', '0019_reflection_export_lifecycle'),
    ]

    operations = [
        migrations.CreateModel(
            name='ConsentExportAudit',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('team_name', models.CharField(blank=True, max_length=100)),
                ('export_format', models.CharField(choices=[('xlsx', 'Excel'), ('pdf', 'PDF'), ('zip', 'ZIP')], max_length=10)),
                ('scope', models.CharField(max_length=30)),
                ('submission_count', models.PositiveIntegerField(default=0)),
                ('result', models.CharField(choices=[('success', '성공'), ('failed', '실패')], max_length=20)),
                ('error_message', models.CharField(blank=True, max_length=500)),
                ('requested_at', models.DateTimeField()),
                ('admin_user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='consent_export_audits', to=settings.AUTH_USER_MODEL)),
                ('team', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='consent_export_audits', to='teams.team')),
            ],
            options={
                'verbose_name': '동의서 다운로드 감사기록',
                'verbose_name_plural': '동의서 다운로드 감사기록',
                'db_table': 'consent_export_audits',
                'indexes': [
                    models.Index(fields=['team', 'requested_at'], name='cons_audit_team_at_idx'),
                    models.Index(fields=['admin_user', 'requested_at'], name='cons_audit_user_at_idx'),
                ],
            },
        ),
    ]
