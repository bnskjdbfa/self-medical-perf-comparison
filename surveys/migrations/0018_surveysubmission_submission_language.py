from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('surveys', '0017_multilingual_survey_and_document_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='surveysubmission',
            name='submission_language',
            field=models.CharField(
                choices=[('ko', '한국어'), ('en', '영어')],
                default='ko',
                max_length=2,
            ),
        ),
    ]
