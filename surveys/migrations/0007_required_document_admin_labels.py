from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('surveys', '0006_required_third_party_consent'),
    ]

    operations = [
        migrations.AlterField(
            model_name='teamfeaturesetting',
            name='enable_privacy_consent',
            field=models.BooleanField(default=False, verbose_name='개인정보제공동의서 사용'),
        ),
        migrations.AlterField(
            model_name='teamfeaturesetting',
            name='enable_third_party_consent',
            field=models.BooleanField(default=False, verbose_name='제 3자 정보제공 동의서 사용'),
        ),
        migrations.AlterField(
            model_name='teamfeaturesetting',
            name='enable_safety_pledge',
            field=models.BooleanField(default=False, verbose_name='여행안전수칙서약서 사용'),
        ),
        migrations.AlterField(
            model_name='documentsubmission',
            name='document_type',
            field=models.CharField(
                choices=[
                    ('privacy_consent', '개인정보제공동의서'),
                    ('third_party_consent', '제 3자 정보제공 동의서'),
                    ('safety_pledge', '여행안전수칙서약서'),
                ],
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name='documentsubmission',
            name='template',
            field=models.ForeignKey(
                blank=True,
                help_text='기존 DB 문서 템플릿 제출에만 사용합니다. 정적 문서 제출은 비어 있습니다.',
                null=True,
                on_delete=models.PROTECT,
                related_name='submissions',
                to='surveys.documenttemplate',
            ),
        ),
        migrations.AlterField(
            model_name='documenttemplate',
            name='document_type',
            field=models.CharField(
                choices=[
                    ('privacy_consent', '개인정보제공동의서'),
                    ('third_party_consent', '제 3자 정보제공 동의서'),
                    ('safety_pledge', '여행안전수칙서약서'),
                ],
                max_length=30,
            ),
        ),
    ]
