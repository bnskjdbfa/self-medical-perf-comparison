from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('surveys', '0016_add_survey_section_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='commonquestion',
            name='graph_label_en',
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name='commonquestion',
            name='label_en',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name='commonquestionsection',
            name='title_en',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='commonquestionset',
            name='title_en',
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name='documenttemplate',
            name='content_html_en',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='documenttemplate',
            name='title_en',
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name='surveyform',
            name='title_en',
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name='surveyquestion',
            name='graph_label_en',
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name='surveyquestion',
            name='label_en',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name='surveysection',
            name='title_en',
            field=models.CharField(blank=True, max_length=100),
        ),
    ]
