from django.db import migrations


def backfill_verified_events(apps, schema_editor):
    Participant = apps.get_model('teams', 'Participant')
    GuardianConsentEvent = apps.get_model('teams', 'GuardianConsentEvent')
    for participant in Participant.objects.filter(guardian_consent_verified_at__isnull=False).iterator():
        if GuardianConsentEvent.objects.filter(participant=participant).exists():
            continue
        GuardianConsentEvent.objects.create(
            participant=participant,
            action='verified',
            source='backfill',
            actor_id=participant.guardian_consent_verified_by_id,
            document_version=participant.guardian_consent_document_version,
            note='기존 확인 기록 이관',
            occurred_at=participant.guardian_consent_verified_at,
        )


class Migration(migrations.Migration):
    dependencies = [
        ('teams', '0008_guardian_consent_events_upload_audit'),
    ]

    operations = [
        migrations.RunPython(backfill_verified_events, migrations.RunPython.noop),
    ]
