import unicodedata

from django.db import migrations


def normalize_name_v2(value):
    text = unicodedata.normalize('NFKC', str(value or ''))
    text = unicodedata.normalize('NFD', text)
    text = ''.join(ch for ch in text if unicodedata.category(ch) != 'Mn')
    text = unicodedata.normalize('NFC', text)
    return ''.join(ch for ch in text.casefold() if ch.isalnum())


def normalize_name_v1(value):
    return ''.join(str(value or '').split()).casefold()


def _renormalize(apps, normalizer):
    problems = []
    for model_name in ('Participant', 'Respondent'):
        model = apps.get_model('teams', model_name)
        rows = list(model.objects.values('id', 'team_id', 'name', 'birth_date', 'gender', 'normalized_name'))
        seen = {}
        for row in rows:
            new_value = normalizer(row['name'])
            if row['gender'] is not None:
                key = (row['team_id'], new_value, row['birth_date'], row['gender'])
                if key in seen:
                    problems.append(f"{model_name}: team={row['team_id']} name={new_value} birth={row['birth_date']} "
                                    f"gender={row['gender']} ids={seen[key]},{row['id']}")
                else:
                    seen[key] = row['id']
        if problems:
            continue
        for row in rows:
            new_value = normalizer(row['name'])
            if new_value != row['normalized_name']:
                model.objects.filter(pk=row['id']).update(normalized_name=new_value)
    if problems:
        raise RuntimeError(
            '이름 정규화 규칙을 바꾸면 같은 사람으로 합쳐지는 행이 있어 진행할 수 없습니다. '
            '먼저 정리한 뒤 다시 migrate 하세요 (manage.py check_identity_duplicates):\n' + '\n'.join(problems[:20])
        )


def forwards(apps, schema_editor):
    _renormalize(apps, normalize_name_v2)


def backwards(apps, schema_editor):
    _renormalize(apps, normalize_name_v1)


class Migration(migrations.Migration):
    dependencies = [
        ('teams', '0010_identity_without_phone'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
