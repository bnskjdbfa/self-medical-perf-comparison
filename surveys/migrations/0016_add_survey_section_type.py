from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('surveys', '0015_exitsurveysubmission_and_exit_form_type'),
    ]

    operations = [
        migrations.AlterField(
            model_name='commonquestionsection',
            name='section_type',
            field=models.CharField(
                choices=[
                    ('physical', '신체'),
                    ('mental', '정신'),
                    ('survey', '설문'),
                ],
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name='surveysection',
            name='section_type',
            field=models.CharField(
                choices=[
                    ('physical', '신체'),
                    ('mental', '정신'),
                    ('survey', '설문'),
                ],
                max_length=20,
            ),
        ),
    ]
