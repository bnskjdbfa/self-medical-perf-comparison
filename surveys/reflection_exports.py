from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import timedelta
from html import escape

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.db import transaction
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    Flowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from teams.models import GenderChoices, Team

from .models import ReflectionSubmission


REFLECTION_STORAGE_ALIAS = 'reflection_exports'
REFLECTION_EXPORT_PREFIX = 'teams'
# v2: the team image is drawn as uploaded (no opacity applied). Operators upload an
# already-faded image, so fading it again made it nearly invisible. Bumping the version
# makes generate_reflection_pdf() re-render PDFs produced by the previous renderer.
REFLECTION_RENDERER_VERSION = 'watermark-v2'
_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class ReflectionExportError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReflectionZipArchive:
    content: bytes
    file_name: str
    submission_ids: tuple[int, ...]


def reflection_storage():
    return storages[REFLECTION_STORAGE_ALIAS]


def safe_filename(value: str, *, fallback: str) -> str:
    normalized = _INVALID_FILENAME_CHARS.sub('_', (value or '').strip())
    normalized = re.sub(r'\s+', ' ', normalized).strip(' .')
    return normalized[:100] or fallback


def _register_pdf_fonts() -> tuple[str, str]:
    regular = 'HYSMyeongJo-Medium'
    bold = 'HYSMyeongJo-Medium'
    if regular not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(regular))
    return regular, bold


def _paragraph_text(value) -> str:
    return escape(str(value or '')).replace('\n', '<br/>')


def _team_image_bytes(team: Team) -> bytes | None:
    if not team.reflection_image:
        return None
    try:
        team.reflection_image.open('rb')
        image_bytes = team.reflection_image.read()
    except (OSError, ValueError):
        return None
    finally:
        try:
            team.reflection_image.close()
        except Exception:
            pass
    if not image_bytes:
        return None
    try:
        ImageReader(io.BytesIO(image_bytes)).getSize()
        return image_bytes
    except Exception:
        return None


class WatermarkedContentBox(Flowable):
    def __init__(
        self,
        paragraph: Paragraph,
        *,
        watermark_bytes: bytes | None,
        min_height: float = 145 * mm,
        padding: float = 8 * mm,
    ):
        super().__init__()
        self.paragraph = paragraph
        self.watermark_bytes = watermark_bytes
        self.min_height = min_height
        self.padding = padding
        self.box_width = 0
        self.box_height = 0

    def wrap(self, avail_width, avail_height):
        content_width = max(avail_width - (self.padding * 2), 1)
        _, paragraph_height = self.paragraph.wrap(content_width, max(avail_height, 1))
        required_height = paragraph_height + (self.padding * 2)
        self.box_width = avail_width
        self.box_height = max(required_height, min(self.min_height, avail_height))
        return self.box_width, self.box_height

    def split(self, avail_width, avail_height):
        content_width = max(avail_width - (self.padding * 2), 1)
        content_height = avail_height - (self.padding * 2)
        if content_height <= 0:
            return []
        paragraphs = self.paragraph.split(content_width, content_height)
        if not paragraphs:
            return []
        return [
            type(self)(
                paragraph,
                watermark_bytes=self.watermark_bytes,
                min_height=self.min_height,
                padding=self.padding,
            )
            for paragraph in paragraphs
        ]

    def draw(self):
        canvas = self.canv
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor('#222222'))
        canvas.setLineWidth(0.8)
        canvas.rect(0, 0, self.box_width, self.box_height, stroke=1, fill=0)

        if self.watermark_bytes:
            try:
                watermark = ImageReader(io.BytesIO(self.watermark_bytes))
                image_width, image_height = watermark.getSize()
                max_width = self.box_width * 0.72
                max_height = self.box_height * 0.48
                scale = min(max_width / image_width, max_height / image_height)
                draw_width = image_width * scale
                draw_height = image_height * scale
                draw_x = (self.box_width - draw_width) / 2
                draw_y = (self.box_height - draw_height) / 2
                # No opacity here on purpose: the uploaded image is expected to be
                # pre-faded, so it is placed exactly as the operator prepared it.
                canvas.drawImage(
                    watermark,
                    draw_x,
                    draw_y,
                    width=draw_width,
                    height=draw_height,
                    preserveAspectRatio=True,
                    mask='auto',
                )
            except Exception:
                pass
        canvas.restoreState()

        content_width = self.box_width - (self.padding * 2)
        _, paragraph_height = self.paragraph.wrap(content_width, self.box_height)
        self.paragraph.drawOn(
            canvas,
            self.padding,
            self.box_height - self.padding - paragraph_height,
        )


def render_reflection_pdf(submission: ReflectionSubmission) -> bytes:
    regular_font, bold_font = _register_pdf_fonts()
    output = io.BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=22 * mm,
        leftMargin=22 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        title=f'{submission.team.name}_{submission.respondent.name}_소감문',
        author=submission.team.organization_name or submission.team.name,
    )
    base_styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'ReflectionTitle',
        parent=base_styles['Title'],
        fontName=bold_font,
        fontSize=21,
        leading=28,
        alignment=TA_CENTER,
        textColor=colors.HexColor('#222222'),
        spaceAfter=10 * mm,
    )
    label_style = ParagraphStyle(
        'ReflectionLabel',
        parent=base_styles['Normal'],
        fontName=bold_font,
        fontSize=10,
        leading=15,
        textColor=colors.HexColor('#555555'),
    )
    value_style = ParagraphStyle(
        'ReflectionValue',
        parent=base_styles['Normal'],
        fontName=regular_font,
        fontSize=10,
        leading=15,
        textColor=colors.HexColor('#222222'),
    )
    content_style = ParagraphStyle(
        'ReflectionContent',
        parent=value_style,
        fontSize=11,
        leading=19,
    )

    gender_label = dict(GenderChoices.choices).get(
        submission.respondent.gender,
        submission.respondent.gender or '',
    )
    submitted_date = timezone.localtime(submission.submitted_at).date().isoformat()
    metadata = [
        [
            Paragraph('팀명', label_style),
            Paragraph(_paragraph_text(submission.team.name), value_style),
            Paragraph('제출일', label_style),
            Paragraph(submitted_date, value_style),
        ],
        [
            Paragraph('이름', label_style),
            Paragraph(_paragraph_text(submission.respondent.name), value_style),
            Paragraph('생년월일', label_style),
            Paragraph(submission.respondent.birth_date.isoformat(), value_style),
        ],
        [
            Paragraph('성별', label_style),
            Paragraph(_paragraph_text(gender_label), value_style),
            Paragraph('소속', label_style),
            Paragraph(_paragraph_text(submission.affiliation), value_style),
        ],
        [
            Paragraph('국가', label_style),
            Paragraph(_paragraph_text(submission.country), value_style),
            Paragraph('제목', label_style),
            Paragraph(_paragraph_text(submission.title), value_style),
        ],
    ]
    metadata_table = Table(
        metadata,
        colWidths=[22 * mm, 55 * mm, 22 * mm, 55 * mm],
        repeatRows=0,
    )
    metadata_table.setStyle(
        TableStyle(
            [
                ('FONTNAME', (0, 0), (-1, -1), regular_font),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#F4F4F4')),
                ('BACKGROUND', (2, 0), (2, -1), colors.HexColor('#F4F4F4')),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#D8D8D8')),
                ('LEFTPADDING', (0, 0), (-1, -1), 6),
                ('RIGHTPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 7),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
            ]
        )
    )

    story = [Paragraph('소감문', title_style), metadata_table, Spacer(1, 10 * mm)]
    story.extend(
        [
            Paragraph('소감문 내용', label_style),
            Spacer(1, 3 * mm),
            WatermarkedContentBox(
                Paragraph(_paragraph_text(submission.content), content_style),
                watermark_bytes=_team_image_bytes(submission.team),
            ),
        ]
    )
    document.build(story)
    return output.getvalue()


def generate_reflection_pdf(
    submission_id: int,
    *,
    force: bool = False,
) -> ReflectionSubmission:
    with transaction.atomic():
        submission = (
            ReflectionSubmission.objects.select_for_update()
            .select_related('team', 'respondent')
            .get(pk=submission_id)
        )
        if submission.purged_at:
            raise ReflectionExportError('보관기간이 만료되어 원문이 삭제된 소감문입니다.')
        storage = reflection_storage()
        current_renderer_prefix = (
            f'{REFLECTION_EXPORT_PREFIX}/{REFLECTION_RENDERER_VERSION}/'
        )
        if (
            not force
            and submission.pdf_status == ReflectionSubmission.PdfStatus.READY
            and submission.file_path
            and submission.file_path.startswith(current_renderer_prefix)
            and storage.exists(submission.file_path)
        ):
            return submission

        old_path = submission.file_path
        try:
            pdf_bytes = render_reflection_pdf(submission)
            team_segment = safe_filename(submission.team.name, fallback=f'team-{submission.team_id}')
            file_name = (
                f'{safe_filename(submission.team.name, fallback="team")}_'
                f'{safe_filename(submission.respondent.name, fallback="participant")}.pdf'
            )
            storage_path = (
                f'{current_renderer_prefix}{submission.team_id}-{team_segment}/'
                f'{submission.pk}/{file_name}'
            )
            if storage.exists(storage_path):
                storage.delete(storage_path)
            saved_path = storage.save(storage_path, ContentFile(pdf_bytes))
            if old_path and old_path != saved_path and storage.exists(old_path):
                storage.delete(old_path)
        except Exception as exc:
            submission.pdf_status = ReflectionSubmission.PdfStatus.FAILED
            submission.pdf_error = str(exc)[:2000]
            submission.save(update_fields=['pdf_status', 'pdf_error', 'updated_at'])
            raise ReflectionExportError(str(exc)) from exc

        submission.file_path = saved_path
        submission.file_name = file_name
        submission.file_size = len(pdf_bytes)
        submission.file_hash = hashlib.sha256(pdf_bytes).hexdigest()
        submission.generated_at = timezone.now()
        submission.pdf_status = ReflectionSubmission.PdfStatus.READY
        submission.pdf_error = ''
        submission.save(
            update_fields=[
                'file_path',
                'file_name',
                'file_size',
                'file_hash',
                'generated_at',
                'pdf_status',
                'pdf_error',
                'updated_at',
            ]
        )
        return submission


def _unique_archive_name(file_name: str, used_names: set[str]) -> str:
    if file_name not in used_names:
        used_names.add(file_name)
        return file_name
    stem = file_name[:-4] if file_name.lower().endswith('.pdf') else file_name
    suffix = 2
    while f'{stem}_{suffix}.pdf' in used_names:
        suffix += 1
    resolved = f'{stem}_{suffix}.pdf'
    used_names.add(resolved)
    return resolved


def build_team_reflection_zip(team: Team) -> ReflectionZipArchive:
    submissions = list(
        ReflectionSubmission.objects.filter(team=team)
        .select_related('team', 'respondent')
        .order_by('respondent__name', 'pk')
    )
    if not submissions:
        raise ReflectionExportError('다운로드할 소감문이 없습니다.')
    if any(submission.purged_at for submission in submissions):
        raise ReflectionExportError('보관기간이 만료되어 삭제된 소감문이 포함되어 있습니다.')

    prepared = []
    for submission in submissions:
        prepared.append(generate_reflection_pdf(submission.pk))

    output = io.BytesIO()
    used_names: set[str] = set()
    storage = reflection_storage()
    with zipfile.ZipFile(output, mode='w', compression=zipfile.ZIP_DEFLATED) as archive:
        for submission in prepared:
            if not submission.file_path or not storage.exists(submission.file_path):
                raise ReflectionExportError(f'{submission.respondent.name} PDF 파일을 찾을 수 없습니다.')
            with storage.open(submission.file_path, 'rb') as pdf_file:
                pdf_bytes = pdf_file.read()
            if hashlib.sha256(pdf_bytes).hexdigest() != submission.file_hash:
                raise ReflectionExportError(f'{submission.respondent.name} PDF 무결성 검증에 실패했습니다.')
            archive.writestr(
                _unique_archive_name(submission.file_name, used_names),
                pdf_bytes,
            )

    now = timezone.now()
    retention_hours = settings.REFLECTION_EXPORT_RETENTION_HOURS
    purge_after = now + timedelta(hours=retention_hours)
    submission_ids = tuple(submission.pk for submission in prepared)
    with transaction.atomic():
        ReflectionSubmission.objects.filter(
            pk__in=submission_ids,
            first_downloaded_at__isnull=True,
        ).update(
            first_downloaded_at=now,
            purge_after=purge_after,
        )

    zip_name = (
        f'{safe_filename(team.name, fallback=f"team-{team.pk}")}_'
        f'소감문_{timezone.localdate().isoformat()}.zip'
    )
    return ReflectionZipArchive(
        content=output.getvalue(),
        file_name=zip_name,
        submission_ids=submission_ids,
    )


def purge_expired_reflection_exports(*, now=None) -> int:
    now = now or timezone.now()
    submission_ids = list(
        ReflectionSubmission.objects.filter(
            purge_after__lte=now,
            purged_at__isnull=True,
        ).values_list('pk', flat=True)
    )
    purged_count = 0
    storage = reflection_storage()
    for submission_id in submission_ids:
        with transaction.atomic():
            submission = ReflectionSubmission.objects.select_for_update().get(pk=submission_id)
            if submission.purged_at or not submission.purge_after or submission.purge_after > now:
                continue
            if submission.file_path and storage.exists(submission.file_path):
                storage.delete(submission.file_path)
            submission.affiliation = ''
            submission.country = ''
            submission.title = ''
            submission.content = ''
            submission.file_path = ''
            submission.file_name = ''
            submission.file_size = 0
            submission.file_hash = ''
            submission.pdf_status = ReflectionSubmission.PdfStatus.DELETED
            submission.pdf_error = ''
            submission.purged_at = now
            submission.save(
                update_fields=[
                    'affiliation',
                    'country',
                    'title',
                    'content',
                    'file_path',
                    'file_name',
                    'file_size',
                    'file_hash',
                    'pdf_status',
                    'pdf_error',
                    'purged_at',
                    'updated_at',
                ]
            )
            purged_count += 1
    return purged_count
