from datetime import date
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from surveys.models import TeamFeatureSetting
from surveys.services import diagnose_identity_mismatch, normalize_name
from surveys.views import SESSION_RESPONDENT_ID
from teams.models import Participant, Team


class NormalizeNameTests(TestCase):
    def test_punctuation_width_accents_and_case_are_ignored(self):
        same = [
            ('Anne-Marie Lee', 'anne marie/lee'),
            ('Anne-Marie Lee', 'ANNE.MARIE,LEE'),
            ("O'Neil", 'oneil'),
            ('Ｋｉｍ Ｍｉｎｓｕ', 'kim minsu'),
            ('Nguyễn Văn An', 'nguyen van an'),
            ('홍 길동', '홍길동'),
        ]
        for left, right in same:
            with self.subTest(left=left, right=right):
                self.assertEqual(normalize_name(left), normalize_name(right))

    def test_word_order_and_hangul_are_preserved(self):
        self.assertNotEqual(normalize_name('Alex Kim'), normalize_name('Kim Alex'))
        self.assertEqual(normalize_name('홍길동'), '홍길동')
        self.assertEqual(normalize_name('Anne-Marie Lee'), 'annemarielee')


class IdentityMismatchHighlightTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(
            name='표시 팀', organization_name='기관', organization_name_en='Org',
            code='HILITE', start_date=date.today(), max_day_no=3,
        )
        TeamFeatureSetting.objects.create(team=self.team, enable_privacy_consent=True)
        self.participant = Participant.objects.create(
            team=self.team, name='Anne-Marie Lee', normalized_name=normalize_name('Anne-Marie Lee'),
            birth_date=date(1998, 5, 4), phone='', normalized_phone='', gender='F', is_active=True,
        )
        Participant.objects.create(
            team=self.team, name='홍길동', normalized_name='홍길동', birth_date=date(1990, 1, 1),
            phone='', normalized_phone='', gender='M', is_active=True,
        )

    def _post(self, payload):
        self.client.post(reverse('surveys:preview-v2-code'), {'team_code': self.team.code})
        self.client.post(reverse('surveys:preview-v2-privacy-consent'), {'agreed': 'on'})
        return self.client.post(reverse('surveys:preview-v2-identity'), payload)

    def _invalid_fields(self, response):
        html = response.content.decode()
        fields = []
        for field in ('name', 'birth_date', 'gender'):
            marker = f'name="{field}"'
            block_start = html.rfind('<div class="v2-identity-field', 0, html.index(marker))
            block = html[block_start:html.index(marker)]
            if 'v2-field--invalid' in block:
                fields.append(field)
        return fields

    def test_name_tag_spelling_with_punctuation_logs_in(self):
        response = self._post({'name': 'anne marie/lee', 'birth_date': '980504', 'gender': 'F'})
        self.assertEqual(response.status_code, 302)
        self.assertIn(SESSION_RESPONDENT_ID, self.client.session)

    def test_only_the_wrong_field_is_outlined_and_message_is_unchanged(self):
        cases = [
            ({'name': 'Anne-Marie Lea', 'birth_date': '980504', 'gender': 'F'}, ['name']),
            ({'name': 'Anne-Marie Lee', 'birth_date': '980405', 'gender': 'F'}, ['birth_date']),
            ({'name': 'Anne-Marie Lee', 'birth_date': '980504', 'gender': 'M'}, ['gender']),
            ({'name': 'Anne-Marie Lee', 'birth_date': '980405', 'gender': 'M'}, ['birth_date', 'gender']),
            ({'name': '아무개', 'birth_date': '000101', 'gender': 'M'}, ['name', 'birth_date', 'gender']),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                response = self._post(payload)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, '입력한 정보가 참가자 명단과 일치하지 않습니다.')
                self.assertNotContains(response, '명찰의 이름과 다릅니다')
                self.assertEqual(self._invalid_fields(response), expected)
                self.assertContains(response, 'aria-describedby="identity-form-error"', count=len(expected))
                self.assertNotIn(SESSION_RESPONDENT_ID, self.client.session)

    def test_same_name_twice_uses_the_closest_roster_row(self):
        Participant.objects.create(
            team=self.team, name='홍길동', normalized_name='홍길동', birth_date=date(1990, 1, 1),
            phone='', normalized_phone='', gender='F', is_active=True,
        )
        # 이름·생년월일이 같은 두 명(성별만 다름) 중 어느 쪽과도 생년월일이 다르면 생년월일만 표시
        fields = diagnose_identity_mismatch(team=self.team, name='홍길동', birth_date=date(1991, 1, 1), gender='F')
        self.assertEqual(fields, {'birth_date'})

    def test_guardian_consent_pending_shows_message_without_outlines(self):
        minor_birth = date(date.today().year - 13, 3, 1)
        Participant.objects.create(
            team=self.team, name='미성년', normalized_name='미성년', birth_date=minor_birth,
            phone='', normalized_phone='', gender='M', is_active=True,
        )
        response = self._post({'name': '미성년', 'birth_date': minor_birth.strftime('%y%m%d'), 'gender': 'M'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '법정대리인 서면 동의 확인이 완료되지 않았습니다')
        self.assertEqual(self._invalid_fields(response), [])


class CheckIdentityDuplicatesWithNewNormalizationTests(TestCase):
    def test_rows_that_merge_under_the_new_rule_are_reported_as_conflicts(self):
        team = Team.objects.create(
            name='정규화 점검', organization_name='기관', organization_name_en='Org',
            code='NORMCHK', start_date=date.today(), max_day_no=1,
        )
        # 옛 규칙으로 저장된 두 행: 'anne-marie' 와 'annemarie' (새 규칙에서는 같은 사람)
        Participant.objects.create(team=team, name='Anne-Marie', normalized_name='anne-marie', birth_date=date(1998, 5, 4),
                                   phone='', normalized_phone='', gender='F', is_active=True)
        Participant.objects.create(team=team, name='Anne Marie', normalized_name='annemarie', birth_date=date(1998, 5, 4),
                                   phone='', normalized_phone='', gender='F', is_active=True)
        out = StringIO()
        with self.assertRaises(SystemExit) as raised:
            call_command('check_identity_duplicates', stdout=out)
        self.assertEqual(raised.exception.code, 1)
        self.assertIn('충돌', out.getvalue())
        self.assertIn('annemarie', out.getvalue())
