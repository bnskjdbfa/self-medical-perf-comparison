from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from surveys.models import DocumentTemplate, TeamFeatureSetting
from surveys.required_documents import (
    GuardianConsentNotVerified,
    build_canonical_document_evidence,
    build_participant_snapshot,
    create_required_document_submissions_from_consents,
)
from surveys.views import SESSION_TEAM_CODE, SESSION_TEAM_ID
from teams.age_policy import GUARDIAN_CONSENT_DOCUMENT_VERSION
from teams.models import Participant, Respondent, Team


class UnderageLoginPolicyTests(TestCase):
    def setUp(self):
        today = date.today()
        self.team = Team.objects.create(
            name='연령 로그인 팀', organization_name='운영기관', organization_name_en='Operator',
            code='UNDERAGE-LOGIN', start_date=today, max_day_no=1,
        )
        self.birth_date = today.replace(year=today.year - 13)
        self.participant = Participant.objects.create(
            team=self.team,
            name='만14세미만',
            normalized_name='만14세미만',
            birth_date=self.birth_date,
            phone='01012345678',
            normalized_phone='01012345678',
            gender='M',
        )

    def login(self):
        # v2 진입: 팀 코드 → 개인정보 동의 → 본인확인
        self.client.post(reverse('surveys:preview-v2-code'), {'team_code': self.team.code})
        self.client.post(reverse('surveys:preview-v2-privacy-consent'), {'agreed': 'on'})
        self.assertEqual(self.client.session.get(SESSION_TEAM_ID), self.team.pk)
        self.assertEqual(self.client.session.get(SESSION_TEAM_CODE), self.team.code)
        return self.client.post(
            reverse('surveys:preview-v2-identity'),
            {'name': '만14세미만', 'birth_date': self.birth_date.isoformat(), 'phone': '01012345678', 'gender': 'M'},
        )

    def test_unverified_underage_roster_row_cannot_log_in(self):
        response = self.login()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '법정대리인 서면 동의 확인이 완료되지 않았습니다.')
        self.assertFalse(Respondent.objects.exists())

    def test_verified_underage_participant_can_log_in(self):
        self.participant.guardian_consent_verified_at = timezone.now()
        self.participant.guardian_consent_verified_by = get_user_model().objects.create_superuser(
            'guardian-checker', 'checker@example.com', 'pass'
        )
        self.participant.guardian_consent_method = Participant.GuardianConsentMethod.WRITTEN
        self.participant.guardian_consent_document_version = GUARDIAN_CONSENT_DOCUMENT_VERSION
        self.participant.save()

        response = self.login()

        self.assertEqual(response.status_code, 302)
        respondent = Respondent.objects.get()
        snapshot = build_participant_snapshot(
            respondent,
            participant=self.participant,
        )
        self.assertEqual(
            snapshot['guardian_consent'],
            {
                'verified': True,
                'method': Participant.GuardianConsentMethod.WRITTEN,
                'verified_at': self.participant.guardian_consent_verified_at.isoformat(),
                'verified_by_user_id': self.participant.guardian_consent_verified_by_id,
                'document_version': GUARDIAN_CONSENT_DOCUMENT_VERSION,
            },
        )

    def test_final_document_submit_rechecks_guardian_consent_in_transaction(self):
        respondent = Respondent.objects.create(
            team=self.team,
            name=self.participant.name,
            normalized_name=self.participant.normalized_name,
            birth_date=self.participant.birth_date,
            phone=self.participant.phone,
            normalized_phone=self.participant.normalized_phone,
            gender=self.participant.gender,
        )
        TeamFeatureSetting.objects.create(
            team=self.team,
            enable_privacy_consent=True,
        )
        evidence = build_canonical_document_evidence(
            team=self.team,
            document_type=DocumentTemplate.DocumentType.PRIVACY_CONSENT,
        )

        with self.assertRaises(GuardianConsentNotVerified):
            create_required_document_submissions_from_consents(
                team=self.team,
                respondent=respondent,
                agreed_at_by_document_type={
                    DocumentTemplate.DocumentType.PRIVACY_CONSENT: timezone.now(),
                },
                expected_fingerprints_by_document_type={
                    DocumentTemplate.DocumentType.PRIVACY_CONSENT: {
                        'document_type': DocumentTemplate.DocumentType.PRIVACY_CONSENT,
                        'language': 'ko',
                        'document_version': evidence['version'],
                        'document_content_hash': evidence['content_hash'],
                    },
                },
            )
