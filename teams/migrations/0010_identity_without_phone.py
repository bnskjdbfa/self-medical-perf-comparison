from django.db import migrations, models
from django.db.models import Count


def ensure_no_identity_duplicates(apps, schema_editor):
    """새 식별 키(팀+이름+생년월일+성별)로 겹치는 행이 있으면 제약을 만들기 전에 멈춘다."""
    problems = []
    for model_name in ('Participant', 'Respondent'):
        model = apps.get_model('teams', model_name)
        duplicates = (
            model.objects.exclude(gender__isnull=True)
            .values('team_id', 'normalized_name', 'birth_date', 'gender')
            .annotate(row_count=Count('id'))
            .filter(row_count__gt=1)
            .order_by()
        )
        for row in duplicates[:20]:
            problems.append(
                f"{model_name}: team={row['team_id']} name={row['normalized_name']} "
                f"birth={row['birth_date']} gender={row['gender']} x{row['row_count']}"
            )
    if problems:
        raise RuntimeError(
            '이름+생년월일+성별이 같은 행이 있어 새 식별 제약을 만들 수 없습니다. '
            '먼저 정리한 뒤 다시 migrate 하세요 (manage.py check_identity_duplicates 로 전체 목록 확인):\n'
            + '\n'.join(problems)
        )


class Migration(migrations.Migration):
    dependencies = [
        ('teams', '0009_backfill_guardian_consent_events'),
    ]

    operations = [
        migrations.RunPython(ensure_no_identity_duplicates, migrations.RunPython.noop),
        migrations.RemoveConstraint(model_name='participant', name='participants_team_identity_uq'),
        migrations.RemoveConstraint(model_name='respondent', name='respondents_team_identity_unique'),
        migrations.AlterField(
            model_name='respondent',
            name='phone',
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AlterField(
            model_name='respondent',
            name='normalized_phone',
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddConstraint(
            model_name='participant',
            constraint=models.UniqueConstraint(
                fields=('team', 'normalized_name', 'birth_date', 'gender'),
                name='participants_team_identity_v2_uq',
            ),
        ),
        migrations.AddConstraint(
            model_name='respondent',
            constraint=models.UniqueConstraint(
                fields=('team', 'normalized_name', 'birth_date', 'gender'),
                name='respondents_team_identity_v2_uq',
            ),
        ),
    ]
