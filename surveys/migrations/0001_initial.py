from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ('teams', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='SurveyForm',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('day_no', models.PositiveSmallIntegerField()),
                ('title', models.CharField(max_length=200)),
                ('version', models.PositiveIntegerField(default=1)),
                ('schema_json', models.JSONField(default=dict)),
                ('is_active', models.BooleanField(default=True)),
                ('team', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='survey_forms', to='teams.team')),
            ],
            options={
                'verbose_name': '설문 폼',
                'verbose_name_plural': '설문 폼',
                'db_table': 'survey_forms',
            },
        ),
        migrations.CreateModel(
            name='SurveySection',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('title', models.CharField(max_length=100)),
                ('section_type', models.CharField(choices=[('physical', '신체'), ('mental', '정신')], max_length=20)),
                ('display_order', models.PositiveSmallIntegerField(default=1)),
                ('form', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='sections', to='surveys.surveyform')),
            ],
            options={
                'verbose_name': '설문 섹션',
                'verbose_name_plural': '설문 섹션',
                'db_table': 'survey_sections',
                'ordering': ['display_order', 'id'],
            },
        ),
        migrations.CreateModel(
            name='SurveyQuestion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('key', models.CharField(max_length=100)),
                ('label', models.CharField(max_length=255)),
                ('question_type', models.CharField(choices=[('text', '주관식'), ('single_choice', '단일선택'), ('multi_choice', '복수선택')], max_length=30)),
                ('options_json', models.JSONField(blank=True, default=list)),
                ('is_required', models.BooleanField(default=True)),
                ('display_order', models.PositiveSmallIntegerField(default=1)),
                ('section', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='questions', to='surveys.surveysection')),
            ],
            options={
                'verbose_name': '설문 문항',
                'verbose_name_plural': '설문 문항',
                'db_table': 'survey_questions',
                'ordering': ['display_order', 'id'],
            },
        ),
        migrations.CreateModel(
            name='SurveySubmission',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('day_no', models.PositiveSmallIntegerField()),
                ('answers_json', models.JSONField(default=dict)),
                ('result_json', models.JSONField(default=dict)),
                ('form_version', models.PositiveIntegerField()),
                ('form_snapshot_json', models.JSONField(default=dict)),
                ('is_locked', models.BooleanField(default=True)),
                ('submitted_at', models.DateTimeField()),
                ('respondent', models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.PROTECT, related_name='survey_submissions', to='teams.respondent')),
                ('team', models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.PROTECT, related_name='survey_submissions', to='teams.team')),
            ],
            options={
                'verbose_name': '설문 제출',
                'verbose_name_plural': '설문 제출',
                'db_table': 'survey_submissions',
            },
        ),
        migrations.CreateModel(
            name='SurveyAnswer',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('answer_text', models.TextField(blank=True)),
                ('answer_json', models.JSONField(blank=True, default=dict)),
                ('question', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='answers', to='surveys.surveyquestion')),
                ('submission', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='answers', to='surveys.surveysubmission')),
            ],
            options={
                'verbose_name': '설문 답변',
                'verbose_name_plural': '설문 답변',
                'db_table': 'survey_answers',
            },
        ),
        migrations.AddConstraint(
            model_name='surveyform',
            constraint=models.UniqueConstraint(fields=('team', 'day_no', 'version'), name='surv_form_team_day_ver_uq'),
        ),
        migrations.AddConstraint(
            model_name='surveyform',
            constraint=models.CheckConstraint(condition=models.Q(('day_no__gte', 1)), name='survey_forms_day_no_gte_1'),
        ),
        migrations.AddIndex(
            model_name='surveyform',
            index=models.Index(fields=['team', 'day_no', 'is_active'], name='surv_form_team_day_act_idx'),
        ),
        migrations.AddIndex(
            model_name='surveysection',
            index=models.Index(fields=['form', 'display_order'], name='surv_sec_form_order_idx'),
        ),
        migrations.AddConstraint(
            model_name='surveyquestion',
            constraint=models.UniqueConstraint(fields=('section', 'key'), name='surv_q_section_key_uq'),
        ),
        migrations.AddIndex(
            model_name='surveyquestion',
            index=models.Index(fields=['section', 'display_order'], name='surv_q_section_order_idx'),
        ),
        migrations.AddConstraint(
            model_name='surveysubmission',
            constraint=models.UniqueConstraint(fields=('team', 'respondent', 'day_no'), name='surv_sub_team_resp_day_uq'),
        ),
        migrations.AddConstraint(
            model_name='surveysubmission',
            constraint=models.CheckConstraint(condition=models.Q(('day_no__gte', 1)), name='surv_sub_day_no_gte_1'),
        ),
        migrations.AddIndex(
            model_name='surveysubmission',
            index=models.Index(fields=['team', 'day_no'], name='surv_sub_team_day_idx'),
        ),
        migrations.AddIndex(
            model_name='surveysubmission',
            index=models.Index(fields=['submitted_at'], name='surv_sub_submitted_at_idx'),
        ),
        migrations.AddConstraint(
            model_name='surveyanswer',
            constraint=models.UniqueConstraint(fields=('submission', 'question'), name='surv_ans_sub_q_uq'),
        ),
        migrations.AddIndex(
            model_name='surveyanswer',
            index=models.Index(fields=['submission'], name='surv_ans_submission_idx'),
        ),
        migrations.AddIndex(
            model_name='surveyanswer',
            index=models.Index(fields=['question'], name='surv_ans_question_idx'),
        ),
    ]
