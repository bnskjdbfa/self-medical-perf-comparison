from datetime import date
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from surveys.models import TeamFeatureSetting
from surveys.views import SESSION_RESPONDENT_ID
from teams.models import Participant, Respondent, Team


class IdentityWithoutPhoneTests(TestCase):
    """참가자 본인확인은 이름·생년월일·성별만 쓰고, 명단의 전화번호는 연락처로만 따라온다."""

    def setUp(self):
        self.team = Team.objects.create(
            name='해외 참가 팀', organization_name='기관', organization_name_en='Org',
            code='NOPHONE', start_date=date.today(), max_day_no=3,
        )
        TeamFeatureSetting.objects.create(team=self.team, enable_privacy_consent=True)
        self.international = Participant.objects.create(
            team=self.team, name='Alex Kim', normalized_name='alexkim', birth_date=date(1998, 5, 4),
            phone='+82 10-1234-5678', normalized_phone='821012345678', gender='M', is_active=True,
        )
        self.no_phone = Participant.objects.create(
            team=self.team, name='Maria Silva', normalized_name='mariasilva', birth_date=date(1999, 12, 24),
            phone='', normalized_phone='', gender='F', is_active=True,
        )

    def _identity(self, payload):
        self.client.post(reverse('surveys:preview-v2-code'), {'team_code': self.team.code})
        self.client.post(reverse('surveys:preview-v2-privacy-consent'), {'agreed': 'on'})
        return self.client.post(reverse('surveys:preview-v2-identity'), payload)

    def test_identity_page_has_no_phone_field(self):
        self.client.post(reverse('surveys:preview-v2-code'), {'team_code': self.team.code})
        self.client.post(reverse('surveys:preview-v2-privacy-consent'), {'agreed': 'on'})
        response = self.client.get(reverse('surveys:preview-v2-identity'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="phone"')
        for field in ('name="name"', 'name="birth_date"', 'name="gender"'):
            self.assertContains(response, field)

    def test_participant_with_international_phone_logs_in_without_typing_it(self):
        response = self._identity({'name': 'alex kim', 'birth_date': '1998-05-04', 'gender': 'M'})

        self.assertEqual(response.status_code, 302)
        respondent = Respondent.objects.get(team=self.team, normalized_name='alexkim')
        self.assertEqual(self.client.session.get(SESSION_RESPONDENT_ID), respondent.id)
        self.assertEqual(respondent.phone, '+82 10-1234-5678')  # 연락처는 명단 값을 그대로 복사

    def test_participant_without_phone_logs_in(self):
        response = self._identity({'name': 'Maria Silva', 'birth_date': '1999-12-24', 'gender': 'F'})

        self.assertEqual(response.status_code, 302)
        respondent = Respondent.objects.get(team=self.team, normalized_name='mariasilva')
        self.assertEqual(respondent.phone, '')
        self.assertEqual(respondent.gender, 'F')

    def test_gender_mismatch_still_blocks(self):
        response = self._identity({'name': 'Maria Silva', 'birth_date': '1999-12-24', 'gender': 'M'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '입력한 정보가 참가자 명단과 일치하지 않습니다.')
        self.assertFalse(Respondent.objects.exists())

    def test_legacy_respondent_without_gender_is_reused(self):
        legacy = Respondent.objects.create(
            team=self.team, name='AlexKim', normalized_name='alexkim', birth_date=date(1998, 5, 4),
            phone='01012345678', normalized_phone='01012345678', gender=None,
        )

        response = self._identity({'name': 'Alex Kim', 'birth_date': '1998-05-04', 'gender': 'M'})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Respondent.objects.filter(team=self.team).count(), 1)
        legacy.refresh_from_db()
        self.assertEqual(legacy.gender, 'M')
        self.assertEqual(self.client.session.get(SESSION_RESPONDENT_ID), legacy.id)


class CheckIdentityDuplicatesCommandTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(
            name='점검 팀', organization_name='기관', organization_name_en='Org',
            code='DUPCHK', start_date=date.today(), max_day_no=1,
        )

    def _run(self):
        out = StringIO()
        try:
            call_command('check_identity_duplicates', stdout=out)
        except SystemExit as exc:
            return exc.code, out.getvalue()
        return 0, out.getvalue()

    def test_reports_clean_when_no_duplicates(self):
        Participant.objects.create(team=self.team, name='홍길동', normalized_name='홍길동', birth_date=date(1990, 1, 1),
                                   phone='01011112222', normalized_phone='01011112222', gender='M', is_active=True)
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertIn('겹침 없음', out)

    def test_warns_about_gender_less_duplicates_without_failing(self):
        # 같은 사람이 전화번호만 다르게 두 번 등록된 옛 데이터. 성별이 없으면 DB 제약에 걸리지 않으므로 경고만 낸다.
        Respondent.objects.bulk_create([
            Respondent(team=self.team, name='홍길동', normalized_name='홍길동', birth_date=date(1990, 1, 1),
                       phone='01011112222', normalized_phone='01011112222', gender=None),
            Respondent(team=self.team, name='홍길동', normalized_name='홍길동', birth_date=date(1990, 1, 1),
                       phone='821011112222', normalized_phone='821011112222', gender=None),
        ])
        code, out = self._run()
        self.assertEqual(code, 0)  # 성별 없는 겹침은 경고만
        self.assertIn('경고(성별 없음', out)
