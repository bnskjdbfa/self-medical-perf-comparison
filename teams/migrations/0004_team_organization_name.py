from django.db import migrations, models


def copy_team_name_to_organization_names(apps, schema_editor):
    Team = apps.get_model('teams', 'Team')
    for team in Team.objects.only('id', 'name').iterator():
        Team.objects.filter(pk=team.pk).update(
            organization_name=team.name,
            organization_name_en=team.name,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('teams', '0003_participant_respondent_gender'),
    ]

    operations = [
        migrations.AddField(
            model_name='team',
            name='organization_name',
            field=models.CharField(
                default='',
                help_text='국문 동의서와 서약서 등 공식 문서에 표시할 기관명입니다.',
                max_length=100,
                verbose_name='운영기관',
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='team',
            name='organization_name_en',
            field=models.CharField(
                default='',
                help_text='영문 설문지와 공식 문서에 표시할 영문 기관명입니다.',
                max_length=150,
                verbose_name='운영기관(영문)',
            ),
            preserve_default=False,
        ),
        migrations.RunPython(
            copy_team_name_to_organization_names,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='team',
            name='organization_name',
            field=models.CharField(
                help_text='국문 동의서와 서약서 등 공식 문서에 표시할 기관명입니다.',
                max_length=100,
                verbose_name='운영기관',
            ),
        ),
        migrations.AlterField(
            model_name='team',
            name='organization_name_en',
            field=models.CharField(
                help_text='영문 설문지와 공식 문서에 표시할 영문 기관명입니다.',
                max_length=150,
                verbose_name='운영기관(영문)',
            ),
        ),
        migrations.AlterField(
            model_name='team',
            name='name',
            field=models.CharField(max_length=100, verbose_name='팀명'),
        ),
    ]
