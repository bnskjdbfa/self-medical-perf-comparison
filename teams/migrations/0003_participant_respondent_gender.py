from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('teams', '0002_team_manager_entry_code'),
    ]

    operations = [
        migrations.AddField(
            model_name='participant',
            name='gender',
            field=models.CharField(blank=True, choices=[('M', '남'), ('F', '여'), ('U', '미입력')], max_length=1, null=True),
        ),
        migrations.AddField(
            model_name='respondent',
            name='gender',
            field=models.CharField(blank=True, choices=[('M', '남'), ('F', '여'), ('U', '미입력')], max_length=1, null=True),
        ),
    ]
