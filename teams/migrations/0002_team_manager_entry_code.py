from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('teams', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='team',
            name='manager_entry_code',
            field=models.CharField(blank=True, max_length=100, null=True, unique=True),
        ),
    ]
