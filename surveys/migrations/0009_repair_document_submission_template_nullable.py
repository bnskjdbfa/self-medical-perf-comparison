from django.db import migrations


def repair_document_submission_template_nullable(apps, schema_editor):
    table_name = 'document_submissions'
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        columns = {
            column.name: column
            for column in connection.introspection.get_table_description(cursor, table_name)
        }
    template_column = columns.get('template_id')
    if template_column is None or template_column.null_ok:
        return
    if connection.vendor != 'postgresql':
        raise RuntimeError(
            'document_submissions.template_id의 NOT NULL 복구는 PostgreSQL에서만 자동 지원합니다.'
        )
    quoted_table = schema_editor.quote_name(table_name)
    quoted_column = schema_editor.quote_name('template_id')
    schema_editor.execute(
        f'ALTER TABLE {quoted_table} ALTER COLUMN {quoted_column} DROP NOT NULL'
    )


class Migration(migrations.Migration):

    dependencies = [
        ('surveys', '0008_required_document_consent_evidence'),
    ]

    operations = [
        migrations.RunPython(
            repair_document_submission_template_nullable,
            migrations.RunPython.noop,
        ),
    ]
