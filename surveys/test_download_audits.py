from datetime import date

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from surveys.download_audits import record_admin_download
from surveys.models import AdminDownloadAudit
from teams.models import Team


class AdminDownloadAuditTests(TestCase):
    def test_records_only_allowlisted_metadata(self):
        user = get_user_model().objects.create_superuser('audit-admin', 'audit@example.com', 'pass')
        team = Team.objects.create(
            name='감사 팀', organization_name='운영기관', organization_name_en='Operator',
            code='AUDIT-TEAM', start_date=date(2026, 8, 1), max_day_no=1,
        )
        request = RequestFactory().get('/admin/export/', {'team': team.pk})
        request.user = user
        audit = record_admin_download(
            request,
            team=team,
            resource_type='exit_survey',
            export_format='xlsx',
            scope='selected',
            record_count=3,
            result='success',
            filters={'date_from': '2026-08-01'},
        )
        self.assertEqual(AdminDownloadAudit.objects.count(), 1)
        self.assertEqual(audit.team_name, '감사 팀')
        self.assertEqual(audit.filters_json, {'date_from': '2026-08-01'})
        self.assertEqual(audit.endpoint, '/admin/export/')
