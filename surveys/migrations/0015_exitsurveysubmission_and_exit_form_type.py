from django.db import migrations, models
import django.db.models.deletion


def classify_deployed_exit_forms(apps, schema_editor):
    CommonQuestionSetDeployment = apps.get_model(
        'surveys',
        'CommonQuestionSetDeployment',
    )
    SurveyForm = apps.get_model('surveys', 'SurveyForm')
    deployed_form_ids = CommonQuestionSetDeployment.objects.filter(
        question_set__form_type='exit_survey',
        survey_form_id__isnull=False,
    ).values_list('survey_form_id', flat=True)
    SurveyForm.objects.filter(id__in=deployed_form_ids).update(
        form_type='exit_survey',
        is_anonymous=False,
    )


class Migration(migrations.Migration):
    dependencies = [
        ('surveys', '0014_commonquestion_question_set'),
    ]

    operations = [
        migrations.AlterField(
            model_name='surveyform',
            name='form_type',
            field=models.CharField(
                choices=[
                    ('daily_health', '일차별 자가검진'),
                    ('exit_anonymous', '설문'),
                    ('exit_survey', '탐방 종료 설문'),
                ],
                default='daily_health',
                max_length=30,
            ),
        ),
        migrations.RunPython(
            classify_deployed_exit_forms,
            migrations.RunPython.noop,
        ),
        migrations.CreateModel(
            name='ExitSurveySubmission',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('form_version', models.PositiveIntegerField()),
                ('form_snapshot_json', models.JSONField(default=dict)),
                ('answers_json', models.JSONField(default=dict)),
                ('is_locked', models.BooleanField(default=True)),
                ('submitted_at', models.DateTimeField()),
                ('respondent', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='exit_survey_submissions', to='teams.respondent')),
                ('survey_form', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='identified_exit_submissions', to='surveys.surveyform')),
                ('team', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='exit_survey_submissions', to='teams.team')),
            ],
            options={
                'verbose_name': '탐방 종료 설문 제출',
                'verbose_name_plural': '탐방 종료 설문 제출',
                'db_table': 'exit_survey_submissions',
                'indexes': [models.Index(fields=['team', 'submitted_at'], name='exit_surv_team_at_idx')],
                'constraints': [models.UniqueConstraint(fields=('team', 'respondent', 'survey_form'), name='exit_surv_team_resp_form_uq')],
            },
        ),
    ]
