from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('surveys', '0010_reflectionsubmission_metadata'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='teamfeaturesetting',
            name='enable_exit_anonymous_survey',
        ),
        migrations.RemoveField(
            model_name='teamfeaturesetting',
            name='enable_reflection',
        ),
    ]
