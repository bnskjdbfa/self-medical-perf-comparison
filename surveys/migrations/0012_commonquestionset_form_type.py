from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('surveys', '0011_remove_optional_feature_approval_flags'),
    ]

    operations = [
        migrations.AddField(
            model_name='commonquestionset',
            name='form_type',
            field=models.CharField(
                choices=[
                    ('daily_health', '자가건강검진'),
                    ('exit_survey', '탐방 종료 설문'),
                ],
                default='daily_health',
                max_length=30,
                verbose_name='설문 유형',
            ),
        ),
    ]
