from datetime import date

from django.test import TestCase
from django.utils import timezone

from teams.age_policy import (
    GUARDIAN_CONSENT_DOCUMENT_VERSION,
    age_on_date,
    is_eligible_participant_age,
    participant_has_required_guardian_consent,
)
from teams.models import Participant, Team


class ParticipantAgePolicyTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(
            name='연령 정책 팀', organization_name='운영기관', organization_name_en='Operator',
            code='AGE-POLICY', start_date=date(2026, 8, 20), max_day_no=1,
        )

    def test_age_is_calculated_on_team_start_date(self):
        self.assertEqual(age_on_date(date(2012, 8, 20), self.team.start_date), 14)
        self.assertTrue(is_eligible_participant_age(date(2012, 8, 20), self.team.start_date))
        self.assertFalse(is_eligible_participant_age(date(2012, 8, 21), self.team.start_date))

    def test_under_14_can_be_registered_but_requires_verified_written_consent(self):
        participant = Participant(
            team=self.team,
            name='미성년 참가자',
            normalized_name='미성년참가자',
            birth_date=date(2012, 8, 21),
            phone='01011112222',
            normalized_phone='01011112222',
        )
        participant.full_clean()
        participant.save()
        self.assertFalse(participant_has_required_guardian_consent(participant))

        participant.guardian_consent_verified_at = timezone.now()
        participant.guardian_consent_method = Participant.GuardianConsentMethod.WRITTEN
        participant.guardian_consent_document_version = GUARDIAN_CONSENT_DOCUMENT_VERSION
        self.assertTrue(participant_has_required_guardian_consent(participant))
