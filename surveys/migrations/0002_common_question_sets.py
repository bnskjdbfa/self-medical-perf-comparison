from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('surveys', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='CommonQuestionSet',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=150, unique=True)),
                ('title', models.CharField(max_length=200)),
                ('day_no', models.PositiveSmallIntegerField()),
                ('is_active', models.BooleanField(default=True)),
                ('description', models.TextField(blank=True)),
            ],
            options={
                'db_table': 'common_question_sets',
                'verbose_name': '공통 질문 세트',
                'verbose_name_plural': '공통 질문 세트',
            },
        ),
        migrations.CreateModel(
            name='CommonQuestionSection',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('title', models.CharField(max_length=100)),
                ('section_type', models.CharField(choices=[('physical', '신체'), ('mental', '정신')], max_length=20)),
                ('display_order', models.PositiveSmallIntegerField(default=1)),
                ('question_set', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='sections', to='surveys.commonquestionset')),
            ],
            options={
                'db_table': 'common_question_sections',
                'ordering': ['display_order', 'id'],
                'verbose_name': '공통 질문 섹션',
                'verbose_name_plural': '공통 질문 섹션',
            },
        ),
        migrations.CreateModel(
            name='CommonQuestion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('key', models.CharField(max_length=100)),
                ('label', models.CharField(max_length=255)),
                ('question_type', models.CharField(choices=[('text', '주관식'), ('single_choice', '단일선택'), ('multi_choice', '복수선택')], max_length=30)),
                ('options_json', models.JSONField(blank=True, default=list)),
                ('is_required', models.BooleanField(default=True)),
                ('display_order', models.PositiveSmallIntegerField(default=1)),
                ('section', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='questions', to='surveys.commonquestionsection')),
            ],
            options={
                'db_table': 'common_questions',
                'ordering': ['display_order', 'id'],
                'verbose_name': '공통 질문 문항',
                'verbose_name_plural': '공통 질문 문항',
            },
        ),
        migrations.AddConstraint(
            model_name='commonquestionset',
            constraint=models.CheckConstraint(check=models.Q(('day_no__gte', 1)), name='common_q_set_day_no_gte_1'),
        ),
        migrations.AddConstraint(
            model_name='commonquestion',
            constraint=models.UniqueConstraint(fields=('section', 'key'), name='common_q_section_key_uq'),
        ),
    ]
