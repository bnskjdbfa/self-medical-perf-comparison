import teams.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('teams', '0005_repair_organization_name_en_column'),
    ]

    operations = [
        migrations.AddField(
            model_name='team',
            name='reflection_image',
            field=models.FileField(
                blank=True,
                help_text='선택 사항 · JPG, PNG, WebP · 최대 5MB',
                upload_to='teams/reflection/',
                validators=[teams.validators.validate_reflection_image],
                verbose_name='소감문 안내 이미지',
            ),
        ),
    ]
