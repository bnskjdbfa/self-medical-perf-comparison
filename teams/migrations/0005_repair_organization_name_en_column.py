from django.db import migrations


def add_missing_organization_name_en(apps, schema_editor):
    """
    Repair databases where teams.0004 was recorded as applied before
    organization_name_en was added to the 0004 migration file.

    Fresh databases already containing the column are left unchanged.
    """

    Team = apps.get_model("teams", "Team")

    connection = schema_editor.connection
    table_name = Team._meta.db_table
    field = Team._meta.get_field("organization_name_en")
    column_name = field.column

    with connection.cursor() as cursor:
        existing_columns = {
            column.name
            for column in connection.introspection.get_table_description(
                cursor,
                table_name,
            )
        }

    # A fresh database may already have received this column from 0004.
    if column_name in existing_columns:
        return

    database_type = field.db_type(connection)

    if not database_type:
        raise RuntimeError(
            "Could not determine the database type for "
            "Team.organization_name_en."
        )

    quote = schema_editor.quote_name
    quoted_table = quote(table_name)
    quoted_column = quote(column_name)
    quoted_name_column = quote("name")

    # Add as nullable first so existing rows do not violate constraints.
    schema_editor.execute(
        f"ALTER TABLE {quoted_table} "
        f"ADD COLUMN {quoted_column} {database_type} NULL"
    )

    # Preserve existing data by using the current team name.
    schema_editor.execute(
        f"UPDATE {quoted_table} "
        f"SET {quoted_column} = COALESCE({quoted_name_column}, '') "
        f"WHERE {quoted_column} IS NULL"
    )

    # Match the final Django model constraint.
    if not field.null:
        schema_editor.execute(
            f"ALTER TABLE {quoted_table} "
            f"ALTER COLUMN {quoted_column} SET NOT NULL"
        )


class Migration(migrations.Migration):

    dependencies = [
        ("teams", "0004_team_organization_name"),
    ]

    operations = [
        migrations.RunPython(
            add_missing_organization_name_en,
            reverse_code=migrations.RunPython.noop,
        ),
    ]