import hashlib
import uuid

from django.db import migrations, models
import django.db.models.deletion


DOCUMENT_ORDER = (
    'privacy_consent',
    'third_party_consent',
    'safety_pledge',
)


def _normalize_content(content):
    return (content or '').replace('\r\n', '\n').replace('\r', '\n').strip()


def backfill_consent_evidence(apps, schema_editor):
    Bundle = apps.get_model('surveys', 'RequiredDocumentConsentBundle')
    Submission = apps.get_model('surveys', 'DocumentSubmission')

    pairs = (
        Submission.objects.filter(bundle_id__isnull=True)
        .order_by()
        .values_list('team_id', 'respondent_id')
        .distinct()
    )
    for team_id, respondent_id in pairs.iterator():
        submissions = list(
            Submission.objects.filter(
                team_id=team_id,
                respondent_id=respondent_id,
                bundle_id__isnull=True,
            )
            .select_related('team', 'respondent', 'template')
            .order_by('agreed_at', 'id')
        )
        if not submissions:
            continue
        first = submissions[0]
        enabled_types = [
            document_type
            for document_type in DOCUMENT_ORDER
            if any(submission.document_type == document_type for submission in submissions)
        ]
        bundle, _ = Bundle.objects.get_or_create(
            team_id=team_id,
            respondent_id=respondent_id,
            defaults={
                'participant_snapshot_json': {
                    'name': first.respondent.name,
                    'birth_date': first.respondent.birth_date.isoformat(),
                    'phone': first.respondent.phone,
                    'gender': first.respondent.gender or '',
                },
                'team_snapshot_json': {
                    'team_id': first.team.id,
                    'team_name': first.team.name,
                    'organization_name': first.team.organization_name,
                    'organization_name_en': first.team.organization_name_en,
                },
                'enabled_document_types_snapshot': enabled_types,
                'snapshot_schema_version': 1,
                'snapshot_source': 'legacy_current_state',
                'completed_at': None,
            },
        )
        for submission in submissions:
            updates = {
                'bundle_id': bundle.id,
                'snapshot_source': 'unavailable',
                'document_title_snapshot': '',
                'document_content_snapshot': '',
                'document_content_hash_at_submit': '',
            }
            if submission.template_id:
                content = _normalize_content(submission.template.content_html)
                if content:
                    updates.update(
                        document_title_snapshot=submission.template.title,
                        document_content_snapshot=content,
                        document_content_hash_at_submit=hashlib.sha256(content.encode('utf-8')).hexdigest(),
                        snapshot_source='legacy_template_backfill',
                    )
            Submission.objects.filter(pk=submission.pk).update(**updates)


class Migration(migrations.Migration):

    dependencies = [
        ('surveys', '0007_required_document_admin_labels'),
        ('teams', '0005_repair_organization_name_en_column'),
    ]

    operations = [
        migrations.CreateModel(
            name='RequiredDocumentConsentBundle',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('bundle_uuid', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('participant_snapshot_json', models.JSONField(default=dict)),
                ('team_snapshot_json', models.JSONField(default=dict)),
                ('enabled_document_types_snapshot', models.JSONField(default=list)),
                ('snapshot_schema_version', models.PositiveSmallIntegerField(default=1)),
                ('snapshot_source', models.CharField(choices=[('live_submit', '실시간 제출'), ('legacy_current_state', '기존 데이터 현재 상태')], default='live_submit', max_length=30)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('respondent', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='required_document_consent_bundles', to='teams.respondent')),
                ('team', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='required_document_consent_bundles', to='teams.team')),
            ],
            options={
                'verbose_name': '필수문서 전자 동의 묶음',
                'verbose_name_plural': '필수문서 전자 동의 묶음',
                'db_table': 'required_document_consent_bundles',
            },
        ),
        migrations.AddField(
            model_name='documentsubmission',
            name='document_content_hash_at_submit',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name='documentsubmission',
            name='document_content_snapshot',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='documentsubmission',
            name='document_title_snapshot',
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name='documentsubmission',
            name='snapshot_source',
            field=models.CharField(choices=[('live_submit', '실시간 제출'), ('legacy_template_backfill', '기존 템플릿 복원'), ('legacy_current_state', '기존 데이터 현재 상태'), ('reconstructed', '재구성'), ('unavailable', '복원 불가')], default='unavailable', max_length=30),
        ),
        migrations.AddField(
            model_name='documentsubmission',
            name='bundle',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='submissions', to='surveys.requireddocumentconsentbundle'),
        ),
        migrations.AddConstraint(
            model_name='requireddocumentconsentbundle',
            constraint=models.UniqueConstraint(fields=('team', 'respondent'), name='req_doc_bundle_team_resp_uq'),
        ),
        migrations.AddIndex(
            model_name='requireddocumentconsentbundle',
            index=models.Index(fields=['team', 'completed_at'], name='req_doc_bundle_team_done_idx'),
        ),
        migrations.RunPython(backfill_consent_evidence, migrations.RunPython.noop),
    ]
