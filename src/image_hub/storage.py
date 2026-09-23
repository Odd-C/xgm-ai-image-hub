import hashlib
import io
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile
from PIL import Image, UnidentifiedImageError

from image_hub.config import settings

ALLOWED_FORMATS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
MAX_IMAGE_PIXELS = 60_000_000
PNG_EXTENSION = ".png"
DOWNLOAD_STEM_PATTERN = re.compile(r"[^0-9A-Za-z._-]+")
GENERIC_ARTIFACT_STEMS = {"result", "artifact", "image", "output", "file", "download", "generated"}


class InvalidImage(ValueError):
    pass


@dataclass(frozen=True)
class StoredImage:
    storage_key: str
    sha256: str
    mime_type: str
    width: int
    height: int
    byte_size: int


def validate_image_payload(payload: bytes) -> tuple[str, int, int]:
    """校验字节确实是一张受支持的图片，返回 ``(格式, 宽, 高)``。"""
    if not payload or len(payload) > settings.max_upload_bytes:
        raise InvalidImage("图片为空或超过 30 MB")
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image.verify()
        with Image.open(io.BytesIO(payload)) as image:
            image_format, (width, height) = image.format or "", image.size
    except (UnidentifiedImageError, OSError) as exc:
        raise InvalidImage("无法识别该图片") from exc
    if image_format not in ALLOWED_FORMATS or width < 64 or height < 64:
        raise InvalidImage("仅支持宽高不小于 64px 的 PNG、JPEG、WebP")
    if width * height > MAX_IMAGE_PIXELS:
        raise InvalidImage("图片像素总量过大")
    return image_format, width, height


def _mime_for(image_format: str) -> str:
    return {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[image_format]


def store_asset_payload(payload: bytes) -> StoredImage:
    """把图片字节落成素材文件，按内容寻址（同一份内容只落一份盘）。"""
    image_format, width, height = validate_image_payload(payload)
    digest = hashlib.sha256(payload).hexdigest()
    extension = ALLOWED_FORMATS[image_format]
    storage_key = f"assets/{digest}{extension}"
    target = settings.storage_root / storage_key
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        # 先写唯一临时名再原子改名：并发写同一份内容时不会互相截断
        temp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.part")
        try:
            temp.write_bytes(payload)
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
    return StoredImage(
        storage_key=storage_key,
        sha256=digest,
        mime_type=_mime_for(image_format),
        width=width,
        height=height,
        byte_size=len(payload),
    )


def link_or_copy(source: Path, target: Path) -> None:
    """把已存在的文件挂到新路径：优先硬链接，失败退回复制。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


async def store_reference(generation_id: str, reference_id: str, upload: UploadFile) -> StoredImage:
    payload = await upload.read(settings.max_upload_bytes + 1)
    image_format, width, height = validate_image_payload(payload)
    extension = ALLOWED_FORMATS[image_format]
    storage_key = f"generations/{generation_id}/references/{reference_id}{extension}"
    target = settings.storage_root / storage_key
    target.parent.mkdir(parents=True, exist_ok=True)
    # 先写唯一临时名再 replace：直接写正式路径的话
    staging = target.with_name(f"{target.name}.{uuid.uuid4().hex}.part")
    staging.write_bytes(payload)
    os.replace(staging, target)
    return StoredImage(
        storage_key=storage_key,
        sha256=hashlib.sha256(payload).hexdigest(),
        mime_type=_mime_for(image_format),
        width=width,
        height=height,
        byte_size=len(payload),
    )


def validate_artifact(path: Path) -> None:
    if not path.is_file() or path.stat().st_size > settings.max_artifact_bytes:
        raise InvalidImage("上游结果不存在或超过文件大小限制")
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image_format = image.format or ""
            width, height = image.size
    except (UnidentifiedImageError, OSError) as exc:
        raise InvalidImage("上游返回的结果不是有效图片") from exc
    if image_format not in ALLOWED_FORMATS or width * height > MAX_IMAGE_PIXELS:
        raise InvalidImage("上游结果格式或像素总量不符合限制")


def resolve_storage_key(storage_key: str) -> Path:
    root = settings.storage_root.resolve()
    candidate = (root / storage_key).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("非法存储路径")
    return candidate


def png_download_name(source_name: str, generation_id: str = "") -> str:
    """A safe ASCII file name that always ends in ``.png``."""
    stem = Path(str(source_name or "")).stem
    safe = DOWNLOAD_STEM_PATTERN.sub("-", stem).strip("-._")[:60]
    prefix = re.sub(r"[^0-9A-Za-z]+", "", str(generation_id or ""))[:8]
    if not safe or (safe.lower() in GENERIC_ARTIFACT_STEMS and prefix):
        safe = f"image-{prefix}" if prefix else (safe or "image")
    return f"{safe}{PNG_EXTENSION}"


def transcode_artifact_to_png(path: Path) -> bytes:
    """Decode a stored artifact and re-encode it as lossless PNG bytes."""
    if not path.is_file():
        raise InvalidImage("图片不存在，无法下载")
    if path.stat().st_size > settings.max_artifact_bytes:
        raise InvalidImage("图片超过文件大小限制，无法转换为 PNG")
    try:
        with Image.open(path) as image:
            image_format = image.format or ""
            if image_format not in ALLOWED_FORMATS:
                raise InvalidImage("仅支持 PNG、JPEG、WebP 图片转换为 PNG")
            if int(getattr(image, "n_frames", 1) or 1) > 1:
                raise InvalidImage("该图片包含多帧动画，无法转换为单张 PNG；请下载原始文件")
            width, height = image.size
            if width * height > MAX_IMAGE_PIXELS:
                raise InvalidImage("图片像素总量过大，无法转换为 PNG")
            has_alpha = "A" in image.getbands() or (
                image.mode == "P" and "transparency" in image.info
            )
            # Alpha is preserved as RGBA; opaque images stay RGB.
            target_mode = "RGBA" if has_alpha else "RGB"
            converted = image if image.mode == target_mode else image.convert(target_mode)
            buffer = io.BytesIO()
            converted.save(buffer, format="PNG", compress_level=6)
    except InvalidImage:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise InvalidImage("图片像素总量过大，无法转换为 PNG") from exc
    except (UnidentifiedImageError, OSError, ValueError, MemoryError) as exc:
        raise InvalidImage("图片已损坏或格式不受支持，无法转换为 PNG") from exc
    return buffer.getvalue()
