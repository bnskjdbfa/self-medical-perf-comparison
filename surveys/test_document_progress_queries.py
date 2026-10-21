"""Required-document gates must remain fresh and respondent-scoped."""
from django.db import connection
from django.test import TestCase
from surveys import tests as flow_tests
from surveys.models import TeamFeatureSetting, DocumentSubmission
from surveys.required_documents import (
    get_required_document_progress, get_enabled_required_document_types,
    get_submitted_required_document_types, create_required_document_submissions,
)
from teams.models import Respondent


class DocumentProgressQueryTest(TestCase):
    setUp = flow_tests.RequiredDocumentFlowTest.setUp

    def assert_matches_original(self):
        enabled = get_enabled_required_document_types(self.team)
        submitted = get_submitted_required_document_types(team=self.team, respondent=self.respondent)
        with self.assertNumQueries(1 if connection.vendor == 'postgresql' else 2):
            actual = get_required_document_progress(team=self.team, respondent=self.respondent)
        self.assertEqual(actual.enabled_types, enabled)
        self.assertEqual(actual.submitted_types, submitted)
        return actual

    def test_empty_partial_and_complete_are_equivalent(self):
        self.assertFalse(self.assert_matches_original().is_complete)
        create_required_document_submissions(team=self.team, respondent=self.respondent)
        self.assertTrue(self.assert_matches_original().is_complete)
        DocumentSubmission.objects.filter(team=self.team, respondent=self.respondent,
                                           document_type='safety_pledge').delete()
        self.assertFalse(self.assert_matches_original().is_complete)

    def test_settings_changes_and_missing_settings_are_fresh(self):
        create_required_document_submissions(team=self.team, respondent=self.respondent)
        self.assert_matches_original()
        TeamFeatureSetting.objects.filter(team=self.team).update(enable_safety_pledge=False)
        self.assertNotIn('safety_pledge', self.assert_matches_original().enabled_types)
        TeamFeatureSetting.objects.filter(team=self.team).delete()
        actual = self.assert_matches_original()
        self.assertEqual(actual.enabled_types, ())
        self.assertEqual(len(actual.submitted_types), 3)

    def test_other_respondents_submissions_do_not_unlock_documents(self):
        create_required_document_submissions(team=self.team, respondent=self.respondent)
        other = Respondent.objects.create(team=self.team, name='Progress Fixture',
            normalized_name='progressfixture', birth_date='1990-01-01', gender='M')
        self.respondent = other
        actual = self.assert_matches_original()
        self.assertFalse(actual.is_complete)
        self.assertEqual(actual.submitted_types, frozenset())
