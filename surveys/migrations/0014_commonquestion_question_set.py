from django.db import migrations, models
import django.db.models.deletion


def backfill_question_sets(apps, schema_editor):
    CommonQuestion = apps.get_model('surveys', 'CommonQuestion')
    for question in CommonQuestion.objects.select_related('section').iterator():
        question.question_set_id = question.section.question_set_id
        question.save(update_fields=['question_set'])


class Migration(migrations.Migration):
    dependencies = [
        ('surveys', '0013_alter_commonquestionset_options'),
    ]

    operations = [
        migrations.AddField(
            model_name='commonquestion',
            name='question_set',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='questions',
                to='surveys.commonquestionset',
            ),
        ),
        migrations.RunPython(
            backfill_question_sets,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='commonquestion',
            name='question_set',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='questions',
                to='surveys.commonquestionset',
            ),
        ),
    ]
