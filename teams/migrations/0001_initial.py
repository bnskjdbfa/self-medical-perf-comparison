from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='Team',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=100)),
                ('code', models.CharField(max_length=50, unique=True)),
                ('slug', models.SlugField(blank=True, max_length=100, null=True, unique=True)),
                ('is_active', models.BooleanField(db_index=True, default=True)),
                ('start_date', models.DateField()),
                ('max_day_no', models.PositiveSmallIntegerField()),
                ('description', models.TextField(blank=True)),
            ],
            options={
                'verbose_name': '팀',
                'verbose_name_plural': '팀',
                'db_table': 'teams',
            },
        ),
        migrations.CreateModel(
            name='Respondent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=50)),
                ('birth_date', models.DateField()),
                ('phone', models.CharField(max_length=20)),
                ('normalized_name', models.CharField(max_length=50)),
                ('normalized_phone', models.CharField(max_length=20)),
                ('team', models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.PROTECT, related_name='respondents', to='teams.team')),
            ],
            options={
                'verbose_name': '응답자',
                'verbose_name_plural': '응답자',
                'db_table': 'respondents',
            },
        ),
        migrations.CreateModel(
            name='Participant',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=50)),
                ('birth_date', models.DateField(blank=True, null=True)),
                ('phone', models.CharField(blank=True, max_length=20)),
                ('normalized_name', models.CharField(max_length=50)),
                ('normalized_phone', models.CharField(blank=True, max_length=20)),
                ('is_active', models.BooleanField(default=True)),
                ('team', models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name='participants', to='teams.team')),
            ],
            options={
                'verbose_name': '대상자 명단',
                'verbose_name_plural': '대상자 명단',
                'db_table': 'participants',
            },
        ),
        migrations.AddConstraint(
            model_name='team',
            constraint=models.CheckConstraint(condition=models.Q(('max_day_no__gte', 1)), name='teams_max_day_no_gte_1'),
        ),
        migrations.AddIndex(
            model_name='respondent',
            index=models.Index(fields=['team'], name='respondents_team_idx'),
        ),
        migrations.AddConstraint(
            model_name='respondent',
            constraint=models.UniqueConstraint(fields=('team', 'normalized_name', 'birth_date', 'normalized_phone'), name='respondents_team_identity_unique'),
        ),
        migrations.AddIndex(
            model_name='participant',
            index=models.Index(fields=['team', 'is_active'], name='participants_team_active_idx'),
        ),
        migrations.AddConstraint(
            model_name='participant',
            constraint=models.UniqueConstraint(fields=('team', 'normalized_name', 'birth_date', 'normalized_phone'), name='participants_team_identity_uq'),
        ),
    ]
