import hashlib
import io
import tempfile
import zipfile
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.storage import storages
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image as PillowImage

from admins.models import AdminTeamPermission
from teams.models import Respondent, Team

from .models import ReflectionDownloadAudit, ReflectionSubmission
from .reflection_exports import (
    build_team_reflection_zip,
    generate_reflection_pdf,
    purge_expired_reflection_exports,
    render_reflection_pdf,
)


class ReflectionExportTest(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage_override = override_settings(
            REFLECTION_EXPORT_RETENTION_HOURS=72,
            STORAGES={
                'default': {
                    'BACKEND': 'django.core.files.storage.FileSystemStorage',
                    'OPTIONS': {
                        'location': self.temp_dir.name,
                        'base_url': None,
                    },
                },
                'reflection_exports': {
                    'BACKEND': 'django.core.files.storage.FileSystemStorage',
                    'OPTIONS': {
                        'location': self.temp_dir.name,
                        'base_url': None,
                    },
                },
                'staticfiles': {
                    'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
                },
            },
        )
        self.storage_override.enable()
        self.addCleanup(self.storage_override.disable)
        self.addCleanup(self.temp_dir.cleanup)

        self.team = Team.objects.create(
            name='1차 원정대',
            organization_name='운영기관',
            organization_name_en='Operating Organization',
            code='REFLECTION-TEAM',
            is_active=True,
            start_date=date.today(),
            max_day_no=3,
        )
        self.respondent = Respondent.objects.create(
            team=self.team,
            name='홍길동',
            birth_date=date(1990, 1, 2),
            phone='01012345678',
            gender='M',
            normalized_name='홍길동',
            normalized_phone='01012345678',
        )
        self.submission = ReflectionSubmission.objects.create(
            team=self.team,
            respondent=self.respondent,
            affiliation='독도초등학교',
            country='대한민국',
            title='기억에 남는 탐방',
            content='탐방 프로그램에서 느낀 점을 충분한 분량으로 작성한 테스트 소감문입니다.',
            submitted_at=timezone.now(),
            is_locked=True,
        )

    def test_test_material_renders_and_stores_valid_pdf_metadata(self):
        pdf_bytes = render_reflection_pdf(
            ReflectionSubmission.objects.select_related('team', 'respondent').get(
                pk=self.submission.pk
            )
        )
        self.assertTrue(pdf_bytes.startswith(b'%PDF-'))

        generated = generate_reflection_pdf(self.submission.pk)
        self.assertEqual(generated.pdf_status, ReflectionSubmission.PdfStatus.READY)
        self.assertEqual(generated.file_name, '1차 원정대_홍길동.pdf')
        self.assertEqual(generated.file_size, len(pdf_bytes))
        self.assertEqual(len(generated.file_hash), 64)
        self.assertTrue(storages['reflection_exports'].exists(generated.file_path))
        with storages['reflection_exports'].open(generated.file_path, 'rb') as stored:
            stored_bytes = stored.read()
        self.assertEqual(hashlib.sha256(stored_bytes).hexdigest(), generated.file_hash)

    def test_long_reflection_content_can_span_multiple_pdf_pages(self):
        self.submission.content = '\n'.join(
            f'{index + 1}. 탐방 중 느낀 점을 상세히 기록한 긴 문장입니다.'
            for index in range(180)
        )
        self.submission.save(update_fields=['content', 'updated_at'])
        pdf_bytes = render_reflection_pdf(
            ReflectionSubmission.objects.select_related('team', 'respondent').get(
                pk=self.submission.pk
            )
        )
        self.assertTrue(pdf_bytes.startswith(b'%PDF-'))
        self.assertGreater(len(pdf_bytes), 5000)

    def test_team_image_is_embedded_as_content_watermark(self):
        watermark_buffer = io.BytesIO()
        PillowImage.new('RGBA', (40, 20), (120, 90, 220, 180)).save(
            watermark_buffer,
            format='PNG',
        )
        self.team.reflection_image = SimpleUploadedFile(
            'watermark.png',
            watermark_buffer.getvalue(),
            content_type='image/png',
        )
        self.team.save(update_fields=['reflection_image', 'updated_at'])

        pdf_bytes = render_reflection_pdf(
            ReflectionSubmission.objects.select_related('team', 'respondent').get(
                pk=self.submission.pk
            )
        )

        self.assertTrue(pdf_bytes.startswith(b'%PDF-'))
        self.assertIn(b'/Subtype /Image', pdf_bytes)

    def test_team_image_is_drawn_without_extra_transparency(self):
        # 운영자가 이미 연하게 만든 이미지를 올리므로 PDF에서 다시 투명하게 만들지 않는다.
        watermark_buffer = io.BytesIO()
        PillowImage.new('RGB', (40, 20), (200, 220, 240)).save(watermark_buffer, format='PNG')
        self.team.reflection_image = SimpleUploadedFile(
            'faded.png',
            watermark_buffer.getvalue(),
            content_type='image/png',
        )
        self.team.save(update_fields=['reflection_image', 'updated_at'])

        pdf_bytes = render_reflection_pdf(
            ReflectionSubmission.objects.select_related('team', 'respondent').get(
                pk=self.submission.pk
            )
        )

        self.assertIn(b'/Subtype /Image', pdf_bytes)
        # reportlab writes a /ca (fill alpha) ExtGState only when setFillAlpha() was used.
        self.assertNotRegex(pdf_bytes, rb'/ca\s*0?\.\d')

    def test_previous_watermark_renderer_pdf_is_regenerated(self):
        storage = storages['reflection_exports']
        old_path = storage.save(
            'teams/watermark-v1/1-team/1/old.pdf',
            SimpleUploadedFile('old.pdf', b'%PDF-old'),
        )
        self.submission.file_path = old_path
        self.submission.file_name = 'old.pdf'
        self.submission.file_size = 8
        self.submission.file_hash = hashlib.sha256(b'%PDF-old').hexdigest()
        self.submission.pdf_status = ReflectionSubmission.PdfStatus.READY
        self.submission.generated_at = timezone.now()
        self.submission.save()

        generated = generate_reflection_pdf(self.submission.pk)

        self.assertTrue(generated.file_path.startswith('teams/watermark-v2/'))
        self.assertFalse(storage.exists(old_path))

    def test_legacy_ready_pdf_is_regenerated_with_current_renderer(self):
        storage = storages['reflection_exports']
        legacy_path = storage.save(
            'teams/legacy/reflection.pdf',
            SimpleUploadedFile('reflection.pdf', b'%PDF-legacy'),
        )
        self.submission.file_path = legacy_path
        self.submission.file_name = 'reflection.pdf'
        self.submission.file_size = 11
        self.submission.file_hash = hashlib.sha256(b'%PDF-legacy').hexdigest()
        self.submission.pdf_status = ReflectionSubmission.PdfStatus.READY
        self.submission.generated_at = timezone.now()
        self.submission.save()

        generated = generate_reflection_pdf(self.submission.pk)

        self.assertTrue(generated.file_path.startswith('teams/watermark-v2/'))
        self.assertFalse(storage.exists(legacy_path))
        self.assertTrue(storage.exists(generated.file_path))

    def test_team_zip_contains_every_pdf_and_starts_single_retention_window(self):
        second_respondent = Respondent.objects.create(
            team=self.team,
            name='홍길동',
            birth_date=date(1991, 2, 3),
            phone='01099998888',
            gender='F',
            normalized_name='홍길동',
            normalized_phone='01099998888',
        )
        second = ReflectionSubmission.objects.create(
            team=self.team,
            respondent=second_respondent,
            affiliation='독도중학교',
            country='대한민국',
            title='두 번째 소감문',
            content='같은 이름을 가진 참가자의 ZIP 파일명 충돌을 검증하기 위한 충분한 본문입니다.',
            submitted_at=timezone.now(),
            is_locked=True,
        )

        archive = build_team_reflection_zip(self.team)
        with zipfile.ZipFile(io.BytesIO(archive.content)) as zip_file:
            self.assertEqual(
                sorted(zip_file.namelist()),
                ['1차 원정대_홍길동.pdf', '1차 원정대_홍길동_2.pdf'],
            )
            self.assertTrue(
                all(zip_file.read(name).startswith(b'%PDF-') for name in zip_file.namelist())
            )

        self.submission.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNotNone(self.submission.first_downloaded_at)
        self.assertEqual(self.submission.first_downloaded_at, second.first_downloaded_at)
        self.assertEqual(self.submission.purge_after, second.purge_after)
        self.assertEqual(
            self.submission.purge_after - self.submission.first_downloaded_at,
            timedelta(hours=72),
        )

        first_downloaded_at = self.submission.first_downloaded_at
        purge_after = self.submission.purge_after
        build_team_reflection_zip(self.team)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.first_downloaded_at, first_downloaded_at)
        self.assertEqual(self.submission.purge_after, purge_after)

    def test_expired_cleanup_removes_pdf_and_original_but_keeps_completion_record(self):
        generated = generate_reflection_pdf(self.submission.pk)
        old_path = generated.file_path
        expired_at = timezone.now() - timedelta(minutes=1)
        ReflectionSubmission.objects.filter(pk=self.submission.pk).update(
            first_downloaded_at=expired_at - timedelta(hours=72),
            purge_after=expired_at,
        )

        self.assertEqual(
            purge_expired_reflection_exports(now=timezone.now()),
            1,
        )
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.pdf_status, ReflectionSubmission.PdfStatus.DELETED)
        self.assertEqual(self.submission.content, '')
        self.assertEqual(self.submission.title, '')
        self.assertEqual(self.submission.file_path, '')
        self.assertIsNotNone(self.submission.purged_at)
        self.assertTrue(self.submission.is_locked)
        self.assertFalse(storages['reflection_exports'].exists(old_path))

    def test_admin_team_download_returns_zip_and_writes_audit(self):
        admin_user = get_user_model().objects.create_superuser(
            username='reflection-admin',
            password='pass',
            email='admin@example.com',
        )
        self.client.force_login(admin_user)
        response = self.client.get(
            reverse('admin:teams_team_reflection_zip', args=[self.team.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/zip')
        self.assertIn("filename*=UTF-8''", response['Content-Disposition'])
        with zipfile.ZipFile(io.BytesIO(response.content)) as zip_file:
            self.assertEqual(zip_file.namelist(), ['1차 원정대_홍길동.pdf'])
        audit = ReflectionDownloadAudit.objects.get()
        self.assertEqual(audit.admin_user, admin_user)
        self.assertEqual(audit.team, self.team)
        self.assertEqual(audit.submission_count, 1)
        self.assertEqual(audit.result, ReflectionDownloadAudit.Result.SUCCESS)

    def test_reflection_admin_list_exposes_team_zip_download_link(self):
        admin_user = get_user_model().objects.create_superuser(
            username='reflection-list-admin',
            password='pass',
            email='list-admin@example.com',
        )
        self.client.force_login(admin_user)

        response = self.client.get(reverse('admin:surveys_reflectionsubmission_changelist'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '소감문 다운로드')
        self.assertContains(response, '1차 원정대 팀 ZIP 다운로드')
        self.assertContains(
            response,
            reverse('admin:teams_team_reflection_zip', args=[self.team.pk]),
        )

    def test_team_permission_is_reapplied_at_download_endpoint(self):
        user_model = get_user_model()
        allowed_user = user_model.objects.create_user(
            username='allowed',
            password='pass',
            is_staff=True,
        )
        allowed_user.user_permissions.add(Permission.objects.get(codename='view_team'))
        AdminTeamPermission.objects.create(user=allowed_user, team=self.team)

        denied_user = user_model.objects.create_user(
            username='denied',
            password='pass',
            is_staff=True,
        )
        self.client.force_login(denied_user)
        response = self.client.get(
            reverse('admin:teams_team_reflection_zip', args=[self.team.pk])
        )
        self.assertEqual(response.status_code, 403)

        self.client.post(reverse('admin:login'), {'username': allowed_user.username, 'password': 'pass'})
        response = self.client.get(
            reverse('admin:teams_team_reflection_zip', args=[self.team.pk])
        )
        self.assertEqual(response.status_code, 200)
