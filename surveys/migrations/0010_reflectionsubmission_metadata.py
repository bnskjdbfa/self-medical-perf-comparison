from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('surveys', '0009_repair_document_submission_template_nullable'),
    ]

    operations = [
        migrations.AddField(
            model_name='reflectionsubmission',
            name='affiliation',
            field=models.CharField(default='', max_length=100),
        ),
        migrations.AddField(
            model_name='reflectionsubmission',
            name='country',
            field=models.CharField(default='', max_length=100),
        ),
        migrations.AddField(
            model_name='reflectionsubmission',
            name='title',
            field=models.CharField(default='', max_length=200),
        ),
    ]
