# Generated manually for the required-document v2 flow.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('surveys', '0005_anonymoussurveysubmission_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='teamfeaturesetting',
            name='enable_third_party_consent',
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name='documentsubmission',
            name='document_type',
            field=models.CharField(
                choices=[
                    ('privacy_consent', '개인정보제공동의서'),
                    ('third_party_consent', '제3자 정보 제공 동의서'),
                    ('safety_pledge', '여행안전수칙서약서'),
                ],
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name='documenttemplate',
            name='document_type',
            field=models.CharField(
                choices=[
                    ('privacy_consent', '개인정보제공동의서'),
                    ('third_party_consent', '제3자 정보 제공 동의서'),
                    ('safety_pledge', '여행안전수칙서약서'),
                ],
                max_length=30,
            ),
        ),
    ]
