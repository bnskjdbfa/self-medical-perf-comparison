from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('surveys', '0018_surveysubmission_submission_language'),
    ]

    operations = [
        migrations.AddField(
            model_name='reflectionsubmission',
            name='first_downloaded_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='reflectionsubmission',
            name='pdf_error',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='reflectionsubmission',
            name='pdf_status',
            field=models.CharField(
                choices=[
                    ('pending', '생성 대기'),
                    ('ready', '생성 완료'),
                    ('failed', '생성 실패'),
                    ('deleted', '보관기간 만료 삭제'),
                ],
                default='pending',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='reflectionsubmission',
            name='purge_after',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='reflectionsubmission',
            name='purged_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name='ReflectionDownloadAudit',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('team_name', models.CharField(max_length=100)),
                ('submission_count', models.PositiveIntegerField(default=0)),
                ('result', models.CharField(choices=[('success', '성공'), ('failed', '실패')], max_length=20)),
                ('error_message', models.CharField(blank=True, max_length=500)),
                ('requested_at', models.DateTimeField()),
                ('admin_user', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='reflection_download_audits',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('team', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='reflection_download_audits',
                    to='teams.team',
                )),
            ],
            options={
                'verbose_name': '소감문 다운로드 감사기록',
                'verbose_name_plural': '소감문 다운로드 감사기록',
                'db_table': 'reflection_download_audits',
                'indexes': [
                    models.Index(fields=['team', 'requested_at'], name='refl_audit_team_at_idx'),
                    models.Index(fields=['admin_user', 'requested_at'], name='refl_audit_user_at_idx'),
                ],
            },
        ),
    ]
