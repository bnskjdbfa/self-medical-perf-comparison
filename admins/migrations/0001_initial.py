from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('teams', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='AdminTeamPermission',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('role', models.CharField(choices=[('read_only_team_admin', '조회전용 팀 관리자')], default='read_only_team_admin', max_length=30)),
                ('team', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='admin_permissions', to='teams.team')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='team_permissions', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': '관리자 팀 권한',
                'verbose_name_plural': '관리자 팀 권한',
                'db_table': 'admin_team_permissions',
            },
        ),
        migrations.AddConstraint(
            model_name='adminteampermission',
            constraint=models.UniqueConstraint(fields=('user', 'team'), name='adm_team_perm_user_team_uq'),
        ),
        migrations.AddIndex(
            model_name='adminteampermission',
            index=models.Index(fields=['team'], name='adm_team_perm_team_idx'),
        ),
    ]
