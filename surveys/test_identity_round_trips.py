"""Policy-lock and latency regressions on a disposable PostgreSQL test database."""
import time
from unittest import skipUnless

from django.db import connection, transaction
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from config.db_limits import bounded_atomic, policy_row
from surveys.models import DocumentSubmission, RequiredDocumentConsentBundle, TeamFeatureSetting
from surveys.required_documents import (
    RequiredDocumentEvidenceChanged, build_canonical_document_evidence,
    create_respondent_with_privacy_consent,
)
from teams.models import Participant, Respondent, Team


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL shared-row locks and RTT budget')
class IdentityRoundTripTest(TransactionTestCase):
    def setUp(self):
        self.team = Team.objects.create(name='Latency fixture', code='RTT-FIXTURE',
                                       start_date=timezone.localdate(), max_day_no=1)
        TeamFeatureSetting.objects.create(team=self.team, enable_privacy_consent=True,
                                         enable_third_party_consent=True, enable_safety_pledge=True)
        Participant.objects.create(team=self.team, name='Latency Fixture', normalized_name='latencyfixture',
                                   birth_date='1990-01-01', gender='M', is_active=True)

    def evidence(self):
        evidence = build_canonical_document_evidence(team=self.team, document_type='privacy_consent')
        return {'document_type': 'privacy_consent', 'language': 'ko',
                'document_version': evidence['version'], 'document_content_hash': evidence['content_hash']}

    def submit(self, evidence):
        from datetime import date
        return create_respondent_with_privacy_consent(team=self.team, name='Latency Fixture',
            birth_date=date(1990, 1, 1), gender='M', agreed_at=timezone.now(), expected_fingerprint=evidence)

    def test_single_policy_read_keeps_shared_lock_and_nowait(self):
        import psycopg
        from psycopg import sql
        other = psycopg.connect(**connection.get_connection_params(), autocommit=True)
        shared = sql.SQL('SELECT id FROM {} WHERE id=%s FOR SHARE NOWAIT').format(sql.Identifier(Team._meta.db_table))
        exclusive = sql.SQL('SELECT id FROM {} WHERE id=%s FOR UPDATE NOWAIT').format(sql.Identifier(Team._meta.db_table))
        try:
            with bounded_atomic():
                with CaptureQueriesContext(connection) as queries:
                    locked = policy_row(Team, pk=self.team.pk, is_active=True)
                self.assertEqual(len(queries), 1)
                self.assertEqual(locked.pk, self.team.pk)
                other.execute(shared, [self.team.pk])
                with self.assertRaises(psycopg.errors.LockNotAvailable):
                    other.execute(exclusive, [self.team.pk])
            # The lock is released on commit, not held around the entire request.
            other.execute(exclusive, [self.team.pk])
            Team.objects.filter(pk=self.team.pk).update(is_active=False)
            with bounded_atomic():
                self.assertIsNone(policy_row(Team, pk=self.team.pk, is_active=True))
        finally:
            other.close()

    def test_identity_survives_70ms_per_sql_without_relaxing_budget(self):
        fingerprint = self.evidence()
        count = 0
        def simulated_rtt(run, sql, params, many, context):
            nonlocal count
            count += 1
            time.sleep(.070)
            return run(sql, params, many, context)
        with connection.execute_wrapper(simulated_rtt):
            respondent = self.submit(fingerprint)
        self.assertEqual(Respondent.objects.count(), 1)
        self.assertEqual(DocumentSubmission.objects.filter(respondent=respondent).count(), 1)
        bundle = RequiredDocumentConsentBundle.objects.get()
        self.assertEqual(bundle.enabled_document_types_snapshot,
                         ['privacy_consent', 'third_party_consent', 'safety_pledge'])
        self.assertIsNone(bundle.completed_at)
        self.assertEqual(connection.settings_dict['ENGINE'], 'django.db.backends.postgresql')
        self.assertLess(count, 21)

    def test_changed_consent_is_still_rejected_atomically(self):
        evidence = self.evidence()
        evidence['document_content_hash'] = '0' * 64
        with self.assertRaises(RequiredDocumentEvidenceChanged):
            self.submit(evidence)
        self.assertFalse(Respondent.objects.exists())
        self.assertFalse(DocumentSubmission.objects.exists())
