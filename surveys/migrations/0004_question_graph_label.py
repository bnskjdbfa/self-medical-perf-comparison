from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('surveys', '0003_teamcommonquestionexclusion'),
    ]

    operations = [
        migrations.AddField(
            model_name='surveyquestion',
            name='graph_label',
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name='commonquestion',
            name='graph_label',
            field=models.CharField(blank=True, max_length=50),
        ),
    ]
