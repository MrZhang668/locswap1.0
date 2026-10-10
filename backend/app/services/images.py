"""Image handling: compress on upload (long edge ≤ IMAGE_MAX_EDGE), store one photo only."""

from __future__ import annotations

import base64
import io
import re
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..config import settings

ALLOWED_SUFFIX = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

#: The only shape `compress_and_save` produces. Every photo path a client sends
#: back is checked against it: the path is joined onto DATA_DIR and its bytes
#: are sent to the model, so `../.env` or `locswap.db` would otherwise be read
#: from disk and shipped to the LLM provider.
UPLOAD_PATH_RE = re.compile(r"uploads/[0-9a-f]{32}\.jpg")


def check_upload_path(value: str | None) -> str | None:
    """Pydantic validator body for client-supplied `photo_path` fields."""
    if value and not UPLOAD_PATH_RE.fullmatch(value):
        raise ValueError("photo_path 必须是上传接口返回的路径")
    return value


def compress_and_save(raw: bytes, filename_hint: str = "") -> str:
    """Compress and write to disk; returns the path relative to DATA_DIR
    (e.g. uploads/xxx.jpg)."""
    image = Image.open(io.BytesIO(raw))
    if image.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", image.size, (255, 255, 255))
        image = image.convert("RGBA")
        background.paste(image, mask=image.split()[-1])
        image = background
    else:
        image = image.convert("RGB")

    max_edge = settings.image_max_edge
    if max(image.size) > max_edge:
        ratio = max_edge / max(image.size)
        image = image.resize((max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
                             Image.LANCZOS)

    name = f"{uuid.uuid4().hex}.jpg"
    settings.upload_path.mkdir(parents=True, exist_ok=True)
    out_path = settings.upload_path / name
    image.save(out_path, format="JPEG", quality=settings.image_jpeg_quality, optimize=True)
    return f"uploads/{name}"


def absolute_path(relative: str | None) -> Path | None:
    if not relative:
        return None
    # Second line of defence behind `check_upload_path`: whatever the string
    # says, never resolve to anything outside the uploads directory.
    path = (settings.data_path / relative).resolve()
    if not path.is_relative_to(settings.upload_path.resolve()):
        return None
    return path


def to_base64_source(relative_path: str) -> dict:
    """Convert to the source of an Anthropic multimodal image block
    (the compressed image, to keep tokens in check)."""
    path = absolute_path(relative_path)
    if not path or not path.exists():
        raise FileNotFoundError(f"图片不存在：{relative_path}")
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {"type": "base64", "media_type": "image/jpeg", "data": data}


_PLACEHOLDER_COLORS = [
    (238, 108, 77), (61, 90, 128), (129, 178, 154), (224, 122, 95),
    (152, 193, 217), (242, 204, 143), (110, 68, 255), (67, 170, 139),
]

#: Common Latin TrueType fonts to try, in order (Windows / Linux). If none is
#: present we fall back to Pillow's built-in bitmap font — small but always there.
_FONT_CANDIDATES = [
    "arial.ttf",
    "segoeui.ttf",
    "DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _load_font(size: int) -> ImageFont.ImageFont:
    for candidate in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:  # noqa: BLE001  font missing/unreadable — try the next one
            continue
    return ImageFont.load_default()


def make_placeholder(text: str, seed: int) -> str:
    """For seed scripts: generate a local placeholder image so we never depend
    on external image sources."""
    color = _PLACEHOLDER_COLORS[seed % len(_PLACEHOLDER_COLORS)]
    image = Image.new("RGB", (640, 480), color)
    draw = ImageDraw.Draw(image)
    label = (text or "Item").strip()[:28] or "Item"
    draw.rectangle([40, 40, 600, 440], outline=(255, 255, 255), width=6)
    draw.ellipse([250, 170, 390, 310], fill=(255, 255, 255, 60), outline=(255, 255, 255), width=4)
    draw.text((56, 56), f"#{seed:02d}", fill=(255, 255, 255), font=_load_font(24))
    draw.text((56, 400), label, fill=(255, 255, 255), font=_load_font(26))

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=settings.image_jpeg_quality)
    return compress_and_save(buffer.getvalue())
