from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('teams', '0001_initial'),
        ('surveys', '0002_common_question_sets'),
    ]

    operations = [
        migrations.CreateModel(
            name='TeamCommonQuestionExclusion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('question_key', models.CharField(max_length=100)),
                ('question_set', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='team_exclusions', to='surveys.commonquestionset')),
                ('team', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='common_question_exclusions', to='teams.team')),
            ],
            options={
                'verbose_name': '팀별 공통 문항 제외',
                'verbose_name_plural': '팀별 공통 문항 제외',
                'db_table': 'team_common_question_exclusions',
            },
        ),
        migrations.AddConstraint(
            model_name='teamcommonquestionexclusion',
            constraint=models.UniqueConstraint(fields=('team', 'question_set', 'question_key'), name='team_common_q_excl_uq'),
        ),
        migrations.AddIndex(
            model_name='teamcommonquestionexclusion',
            index=models.Index(fields=['team', 'question_set'], name='team_cq_excl_tset_idx'),
        ),
    ]
