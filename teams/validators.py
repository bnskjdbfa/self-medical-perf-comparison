from pathlib import Path

from django.core.exceptions import ValidationError


REFLECTION_IMAGE_MAX_BYTES = 5 * 1024 * 1024
REFLECTION_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
REFLECTION_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


def validate_reflection_image(upload):
    if Path(upload.name).suffix.lower() not in REFLECTION_IMAGE_EXTENSIONS:
        raise ValidationError("JPG, PNG, WebP 이미지만 등록할 수 있습니다.")
    if upload.size > REFLECTION_IMAGE_MAX_BYTES:
        raise ValidationError("이미지는 5MB 이하만 등록할 수 있습니다.")

    content_type = getattr(upload, "content_type", "")
    if content_type and content_type not in REFLECTION_IMAGE_CONTENT_TYPES:
        raise ValidationError("이미지 MIME 형식이 올바르지 않습니다.")

    position = upload.tell()
    header = upload.read(12)
    upload.seek(position)
    if header.startswith(b"\xff\xd8\xff"):
        detected_type = "jpeg"
    elif header.startswith(b"\x89PNG\r\n\x1a\n"):
        detected_type = "png"
    elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        detected_type = "webp"
    else:
        raise ValidationError("실제 이미지 파일 형식을 확인해 주세요.")

    expected_type = {
        ".jpg": "jpeg",
        ".jpeg": "jpeg",
        ".png": "png",
        ".webp": "webp",
    }[Path(upload.name).suffix.lower()]
    mime_type = {
        "image/jpeg": "jpeg",
        "image/png": "png",
        "image/webp": "webp",
    }.get(content_type)
    if detected_type != expected_type or (mime_type and detected_type != mime_type):
        raise ValidationError("파일 확장자와 실제 이미지 형식이 일치하지 않습니다.")
