import csv
import io
from datetime import date

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from teams.admin import ParticipantAdminForm
from teams.age_policy import (
    GUARDIAN_CONSENT_DOCUMENT_VERSION,
    age_on_date,
    is_eligible_participant_age,
    underage_birth_date_threshold,
    unverified_underage_participants,
)
from teams.guardian_consent import (
    GuardianConsentNotActive,
    revoke_guardian_consent,
    verify_guardian_consent,
)
from teams.models import GuardianConsentEvent, Participant, ParticipantUploadAudit, Team
from teams.participant_upload import (
    apply_participant_upload,
    error_rows_csv,
    plan_participant_upload,
    summarize_plans,
)


START = date(2026, 8, 20)
ADULT_BIRTH = date(2005, 3, 1)      # 만 21세
UNDER_BIRTH = date(2013, 1, 15)     # 시작일 기준 만 13세
BORDER_BIRTH = date(2012, 8, 20)    # 시작일 당일 만 14세


def make_team(code='GC-TEAM', start=START):
    return Team.objects.create(
        name=f'팀 {code}', organization_name='기관', organization_name_en='Org',
        code=code, start_date=start, max_day_no=3,
    )


def make_participant(team, name, birth_date, phone='01011112222', gender='M', **extra):
    return Participant.objects.create(
        team=team, name=name, normalized_name=name.replace(' ', '').casefold(), birth_date=birth_date,
        phone=phone, normalized_phone=''.join(ch for ch in phone if ch.isdigit()), gender=gender,
        is_active=True, **extra,
    )


def csv_upload(rows, *, name='roster.csv'):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return SimpleUploadedFile(name, output.getvalue().encode('utf-8-sig'), content_type='text/csv')


class AgeThresholdTests(TestCase):
    def test_threshold_matches_age_on_date_including_leap_days(self):
        for reference in (date(2026, 8, 20), date(2028, 2, 29), date(2027, 2, 28), date(2026, 1, 1), date(2026, 12, 31)):
            threshold = underage_birth_date_threshold(reference)
            for offset in range(-3, 4):
                birth = date.fromordinal(threshold.toordinal() + offset)
                with self.subTest(reference=reference, birth=birth):
                    self.assertEqual(
                        is_eligible_participant_age(birth, reference),
                        birth <= threshold,
                        msg=f'age={age_on_date(birth, reference)}',
                    )

    def test_unverified_underage_participants_respects_reference_date(self):
        team = make_team()
        adult = make_participant(team, '성인', ADULT_BIRTH, phone='01000000001')
        border = make_participant(team, '경계', BORDER_BIRTH, phone='01000000002')
        minor = make_participant(team, '미성년', UNDER_BIRTH, phone='01000000003')
        verified = make_participant(team, '확인됨', UNDER_BIRTH, phone='01000000004')
        verify_guardian_consent(participant=verified, actor=None, source=GuardianConsentEvent.Source.ADMIN_FORM)

        self.assertEqual(list(unverified_underage_participants(team)), [minor])
        # 시작일을 앞당기면 경계 참가자도 만 14세 미만이 된다.
        self.assertEqual(
            list(unverified_underage_participants(team, reference_date=date(2026, 8, 1))),
            [border, minor],
        )
        self.assertNotIn(adult, unverified_underage_participants(team, reference_date=date(2026, 8, 1)))


class UploadPlanAndApplyTests(TestCase):
    def setUp(self):
        self.team = make_team()
        self.actor = get_user_model().objects.create_superuser('uploader', 'u@example.com', 'pass')
        self.existing_verified = make_participant(self.team, '기존확인', UNDER_BIRTH, phone='01099990000')
        verify_guardian_consent(participant=self.existing_verified, actor=self.actor, source=GuardianConsentEvent.Source.ADMIN_FORM)
        self.existing_adult = make_participant(self.team, '기존성인', ADULT_BIRTH, phone='01099991111')
        self.rows = [
            {'이름': '성인', '생년월일': '20050301', '연락처': '010-1000-0001', '성별': '남', '법정대리인동의확인': ''},
            {'이름': '대기미성년', '생년월일': '20130115', '연락처': '010-1000-0002', '성별': '여', '법정대리인동의확인': ''},
            {'이름': '확인미성년', '생년월일': '20130115', '연락처': '010-1000-0003', '성별': '남', '법정대리인동의확인': '완료'},
            {'이름': '기존확인', '생년월일': '20130115', '연락처': '010-9999-0000', '성별': '남', '법정대리인동의확인': ''},
            {'이름': '기존성인', '생년월일': '20050301', '연락처': '010-9999-1111', '성별': '남', '법정대리인동의확인': ''},
            {'이름': '성별오류', '생년월일': '20050301', '연락처': '010-1000-0005', '성별': 'X', '법정대리인동의확인': ''},
            {'이름': '', '생년월일': '20050301', '연락처': '010-1000-0006', '성별': '남', '법정대리인동의확인': ''},
        ]

    def test_plan_marks_underage_rows_as_pending_instead_of_rejecting(self):
        plans = plan_participant_upload(team=self.team, rows=self.rows, update_existing=True)

        by_name = {plan.name: plan for plan in plans}
        self.assertEqual((by_name['성인'].action, by_name['성인'].consent), ('create', 'not_required'))
        self.assertEqual((by_name['대기미성년'].action, by_name['대기미성년'].consent), ('create', 'pending'))
        self.assertIn('로그인할 수 없습니다', by_name['대기미성년'].message)
        self.assertEqual((by_name['확인미성년'].action, by_name['확인미성년'].consent), ('create', 'verified'))
        self.assertEqual((by_name['기존확인'].action, by_name['기존확인'].consent), ('update', 'kept'))
        self.assertEqual(by_name['기존성인'].action, 'update')
        self.assertEqual(by_name['성별오류'].action, 'error')
        self.assertTrue(plans[-1].is_error)
        self.assertEqual(
            summarize_plans(plans),
            {'total': 7, 'create': 3, 'update': 2, 'skip': 0, 'error': 2, 'pending': 1, 'verified': 1},
        )

        skip_plans = plan_participant_upload(team=self.team, rows=self.rows, update_existing=False)
        self.assertEqual({p.name: p.action for p in skip_plans}['기존확인'], 'skip')

    def test_apply_creates_pending_and_verified_participants_with_audit(self):
        plans = plan_participant_upload(team=self.team, rows=self.rows, update_existing=True)

        audit = apply_participant_upload(
            team=self.team, plans=plans, update_existing=True, actor=self.actor, file_name='roster.csv',
        )

        pending = Participant.objects.get(team=self.team, name='대기미성년')
        self.assertIsNone(pending.guardian_consent_verified_at)
        self.assertFalse(pending.guardian_consent_events.exists())

        verified = Participant.objects.get(team=self.team, name='확인미성년')
        self.assertIsNotNone(verified.guardian_consent_verified_at)
        self.assertEqual(verified.guardian_consent_verified_by, self.actor)
        self.assertEqual(verified.guardian_consent_document_version, GUARDIAN_CONSENT_DOCUMENT_VERSION)
        event = verified.guardian_consent_events.get()
        self.assertEqual(event.action, GuardianConsentEvent.Action.VERIFIED)
        self.assertEqual(event.source, GuardianConsentEvent.Source.BULK_UPLOAD)
        self.assertIn('roster.csv', event.note)

        self.existing_verified.refresh_from_db()
        self.assertIsNotNone(self.existing_verified.guardian_consent_verified_at)
        self.assertEqual(self.existing_verified.guardian_consent_events.count(), 1)  # 재기록 없음

        self.assertFalse(Participant.objects.filter(team=self.team, name='성별오류').exists())
        self.assertEqual(
            (audit.created_count, audit.updated_count, audit.skipped_count, audit.error_count, audit.pending_consent_count),
            (3, 2, 0, 2, 1),
        )
        self.assertEqual(audit.admin_user, self.actor)
        self.assertEqual(audit.team_name, self.team.name)

    def test_error_rows_csv_lists_only_error_rows(self):
        plans = plan_participant_upload(team=self.team, rows=self.rows, update_existing=True)

        content = error_rows_csv(plans).decode('utf-8-sig')

        rows = list(csv.reader(io.StringIO(content)))
        self.assertEqual(rows[0], ['행', '이름', '생년월일', '연락처', '성별', '사유'])
        self.assertEqual([row[1] for row in rows[1:]], ['성별오류', ''])
        self.assertEqual(len(rows), 3)


class BulkUploadAdminFlowTests(TestCase):
    def setUp(self):
        self.team = make_team()
        self.superuser = get_user_model().objects.create_superuser('root', 'root@example.com', 'pass')
        self.client.force_login(self.superuser)
        self.upload_url = reverse('admin:teams_participant_bulk_upload')

    def _post_file(self, rows, **extra):
        return self.client.post(
            self.upload_url,
            {'team': self.team.pk, 'csv_file': csv_upload(rows), 'update_existing': 'on', **extra},
        )

    def test_upload_previews_then_confirms_and_reports_pending_minors(self):
        rows = [
            {'이름': '성인', '생년월일': '20050301', '연락처': '010-1000-0001', '성별': '남'},
            {'이름': '대기미성년', '생년월일': '20130115', '연락처': '010-1000-0002', '성별': '여'},
            {'이름': '성별오류', '생년월일': '20050301', '연락처': '010-1000-0005', '성별': 'X'},
        ]

        response = self._post_file(rows)

        self.assertEqual(response.status_code, 302)
        self.assertIn('/bulk-upload/', response.url)
        self.assertEqual(Participant.objects.filter(team=self.team).count(), 0)  # 검증 단계에서는 저장 안 함

        preview = self.client.get(response.url)
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, '대기미성년')
        self.assertContains(preview, '동의 대기 (로그인 불가)')
        self.assertContains(preview, '오류 행 CSV 내려받기')
        self.assertContains(preview, '확인하고 저장')

        errors_csv = self.client.get(preview.context['errors_url'])
        self.assertEqual(errors_csv.status_code, 200)
        self.assertIn('성별오류', errors_csv.content.decode('utf-8-sig'))

        confirm = self.client.post(response.url, follow=True)
        self.assertEqual(confirm.status_code, 200)
        self.assertEqual(Participant.objects.filter(team=self.team).count(), 2)
        self.assertContains(confirm, '생성 2건')
        self.assertContains(confirm, '동의 대기 1건')
        self.assertContains(confirm, 'guardian_consent=pending')
        self.assertContains(confirm, '오류 행 1건')
        audit = ParticipantUploadAudit.objects.get()
        self.assertEqual((audit.created_count, audit.error_count, audit.pending_consent_count), (2, 1, 1))

        # 같은 토큰으로 다시 확정해도 중복 저장되지 않는다.
        self.client.post(response.url)
        self.assertEqual(Participant.objects.filter(team=self.team).count(), 2)
        self.assertEqual(ParticipantUploadAudit.objects.count(), 1)

    def test_unknown_token_returns_404(self):
        response = self.client.get(reverse('admin:teams_participant_bulk_upload_preview', args=['no-such-token']))
        self.assertEqual(response.status_code, 404)

    def test_missing_required_column_is_rejected_before_preview(self):
        response = self._post_file([{'이름': '성인', '성별': '남'}])
        self.assertEqual(response.status_code, 302)
        followed = self.client.get(self.upload_url)
        self.assertContains(followed, '필수 컬럼 누락')
        self.assertEqual(Participant.objects.count(), 0)


class ParticipantAdminConsentTests(TestCase):
    def setUp(self):
        self.team = make_team()
        self.superuser = get_user_model().objects.create_superuser('root', 'root@example.com', 'pass')
        self.client.force_login(self.superuser)

    def _payload(self, participant=None, **overrides):
        data = {
            'team': self.team.pk, 'name': '미성년', 'gender': 'M', 'birth_date': UNDER_BIRTH.isoformat(),
            'phone': '01012340000', 'is_active': 'on',
            'guardian_consent_events-TOTAL_FORMS': '0', 'guardian_consent_events-INITIAL_FORMS': '0',
            'guardian_consent_events-MIN_NUM_FORMS': '0', 'guardian_consent_events-MAX_NUM_FORMS': '1000',
        }
        data.update(overrides)
        return data

    def test_form_allows_underage_without_consent_checkbox(self):
        form = ParticipantAdminForm(data={
            'team': self.team.pk, 'name': '미성년', 'birth_date': UNDER_BIRTH.isoformat(), 'phone': '01012340000',
            'normalized_name': '미성년', 'normalized_phone': '01012340000',
            'gender': 'M', 'is_active': 'on',
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertNotIn('guardian_written_consent_confirmed', form.errors)

    def test_add_underage_without_checkbox_saves_pending_and_warns(self):
        response = self.client.post(reverse('admin:teams_participant_add'), self._payload(), follow=True)

        self.assertEqual(response.status_code, 200)
        participant = Participant.objects.get(team=self.team, name='미성년')
        self.assertIsNone(participant.guardian_consent_verified_at)
        self.assertContains(response, '동의 미확인 상태로 저장되었습니다')

    def test_checkbox_verifies_consent_and_logs_event(self):
        participant = make_participant(self.team, '미성년', UNDER_BIRTH, phone='01012340000')

        response = self.client.post(
            reverse('admin:teams_participant_change', args=[participant.pk]),
            self._payload(guardian_written_consent_confirmed='on'),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        participant.refresh_from_db()
        self.assertIsNotNone(participant.guardian_consent_verified_at)
        self.assertEqual(participant.guardian_consent_verified_by, self.superuser)
        event = participant.guardian_consent_events.get()
        self.assertEqual((event.action, event.source, event.actor), ('verified', 'admin_form', self.superuser))
        self.assertContains(response, '법정대리인 서면 동의 확인을 기록했습니다')

    def test_consent_record_is_kept_when_participant_becomes_eligible_by_age(self):
        participant = make_participant(self.team, '미성년', UNDER_BIRTH, phone='01012340000')
        verify_guardian_consent(participant=participant, actor=self.superuser, source=GuardianConsentEvent.Source.ADMIN_FORM)

        self.client.post(
            reverse('admin:teams_participant_change', args=[participant.pk]),
            self._payload(birth_date=ADULT_BIRTH.isoformat()),
            follow=True,
        )

        participant.refresh_from_db()
        self.assertEqual(participant.birth_date, ADULT_BIRTH)
        self.assertIsNotNone(participant.guardian_consent_verified_at)

    def test_revoke_view_requires_reason_and_logs_revocation(self):
        participant = make_participant(self.team, '미성년', UNDER_BIRTH, phone='01012340000')
        verify_guardian_consent(participant=participant, actor=self.superuser, source=GuardianConsentEvent.Source.ADMIN_FORM)
        revoke_url = reverse('admin:teams_participant_guardian_consent_revoke', args=[participant.pk])

        change_page = self.client.get(reverse('admin:teams_participant_change', args=[participant.pk]))
        self.assertContains(change_page, revoke_url)
        self.assertContains(change_page, '법정대리인 동의 이력')

        self.assertEqual(self.client.get(revoke_url).status_code, 200)
        missing_reason = self.client.post(revoke_url, {'reason': ''})
        self.assertEqual(missing_reason.status_code, 200)
        participant.refresh_from_db()
        self.assertIsNotNone(participant.guardian_consent_verified_at)

        response = self.client.post(revoke_url, {'reason': '서명 불일치'}, follow=True)

        self.assertContains(response, '동의 확인을 취소했습니다')
        participant.refresh_from_db()
        self.assertIsNone(participant.guardian_consent_verified_at)
        self.assertEqual(participant.guardian_consent_method, '')
        revoked = participant.guardian_consent_events.filter(action='revoked').get()
        self.assertEqual((revoked.note, revoked.source, revoked.actor), ('서명 불일치', 'admin_revoke', self.superuser))
        self.assertEqual(participant.guardian_consent_events.count(), 2)

        with self.assertRaises(GuardianConsentNotActive):
            revoke_guardian_consent(participant=participant, actor=self.superuser, reason='다시')

    def test_guardian_consent_list_filter(self):
        adult = make_participant(self.team, '성인', ADULT_BIRTH, phone='01000000001')
        pending = make_participant(self.team, '대기', UNDER_BIRTH, phone='01000000002')
        verified = make_participant(self.team, '확인', UNDER_BIRTH, phone='01000000003')
        verify_guardian_consent(participant=verified, actor=self.superuser, source=GuardianConsentEvent.Source.ADMIN_FORM)
        changelist = reverse('admin:teams_participant_changelist')

        def names(value):
            response = self.client.get(changelist, {'guardian_consent': value})
            self.assertEqual(response.status_code, 200)
            return sorted(p.name for p in response.context['cl'].queryset)

        self.assertEqual(names('pending'), ['대기'])
        self.assertEqual(names('verified'), ['확인'])
        self.assertEqual(names('not_required'), ['성인'])
        self.assertEqual(sorted(p.name for p in self.client.get(changelist).context['cl'].queryset), ['대기', '성인', '확인'])
        self.assertIn(pending, unverified_underage_participants(self.team))
        self.assertNotIn(adult, unverified_underage_participants(self.team))


class TeamStartDateGuardTests(TestCase):
    def setUp(self):
        self.team = make_team()
        self.superuser = get_user_model().objects.create_superuser('root', 'root@example.com', 'pass')
        self.client.force_login(self.superuser)
        # 시작일 2026-08-20 기준 만 14세, 시작일을 2026-05-01로 당기면 만 13세
        self.border = make_participant(self.team, '경계', date(2012, 6, 1), phone='01000000009')
        self.change_url = reverse('admin:teams_team_change', args=[self.team.pk])

    def _payload(self, **overrides):
        data = {
            'name': self.team.name, 'organization_name': '기관', 'organization_name_en': 'Org',
            'code': self.team.code, 'manager_entry_code': '', 'is_active': 'on',
            'start_date': START.isoformat(), 'max_day_no': 3,
            'feature_settings-TOTAL_FORMS': '0', 'feature_settings-INITIAL_FORMS': '0',
            'feature_settings-MIN_NUM_FORMS': '0', 'feature_settings-MAX_NUM_FORMS': '1',
        }
        data.update(overrides)
        return data

    def test_moving_start_date_earlier_requires_confirmation_when_it_creates_unverified_minors(self):
        response = self.client.post(self.change_url, self._payload(start_date='2026-05-01'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '동의가 확인되지 않은 참가자가 1명 있습니다: 경계')
        self.team.refresh_from_db()
        self.assertEqual(self.team.start_date, START)

        confirmed = self.client.post(
            self.change_url, self._payload(start_date='2026-05-01', confirm_start_date_impact='on'), follow=True,
        )

        self.assertEqual(confirmed.status_code, 200)
        self.team.refresh_from_db()
        self.assertEqual(self.team.start_date, date(2026, 5, 1))
        self.assertContains(confirmed, '동의 미확인 상태가 된 참가자가 1명')
        self.assertContains(confirmed, 'guardian_consent=pending')

    def test_start_date_change_without_impact_saves_directly(self):
        response = self.client.post(self.change_url, self._payload(start_date='2026-09-01'), follow=True)

        self.assertEqual(response.status_code, 200)
        self.team.refresh_from_db()
        self.assertEqual(self.team.start_date, date(2026, 9, 1))
        self.assertNotContains(response, '동의 미확인 상태가 된 참가자')

    def test_other_field_changes_do_not_trigger_confirmation(self):
        response = self.client.post(self.change_url, self._payload(max_day_no=4), follow=True)
        self.assertEqual(response.status_code, 200)
        self.team.refresh_from_db()
        self.assertEqual(self.team.max_day_no, 4)
