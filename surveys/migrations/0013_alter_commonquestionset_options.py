from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('surveys', '0012_commonquestionset_form_type'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='commonquestionset',
            options={
                'verbose_name': '공통 설문',
                'verbose_name_plural': '공통 설문',
            },
        ),
    ]
