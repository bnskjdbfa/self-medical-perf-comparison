import uuid
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('surveys', '0021_admindownloadaudit')]
    operations = [
        migrations.CreateModel(
            name='LoginSessionFamily',
            fields=[
                ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ('secret_hash', models.CharField(max_length=64)),
                ('epoch', models.PositiveBigIntegerField(default=0)),
                ('active_generation', models.UUIDField(null=True)),
                ('expires_at', models.DateTimeField(db_index=True)),
            ], options={'db_table': 'login_session_families'},
        ),
        migrations.CreateModel(
            name='LoginSessionCredential',
            fields=[
                ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ('epoch', models.PositiveBigIntegerField()),
                ('session_key', models.CharField(max_length=40, unique=True)),
                ('key_hash', models.CharField(max_length=64, unique=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('family', models.ForeignKey(to='surveys.loginsessionfamily', on_delete=django.db.models.deletion.CASCADE)),
            ], options={'db_table': 'login_session_credentials'},
        ),
    ]
