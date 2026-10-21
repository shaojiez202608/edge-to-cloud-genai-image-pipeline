"""Input checks for uploaded images. The upload is untrusted: the declared content type, size and
hash are compared with the bytes, the magic number must match the declared type, Pillow decodes it
under a pixel limit, and only a re-encoded copy (RGB JPEG, no metadata, bounded size) goes further.
Text hidden in metadata (EXIF, PNG text chunks) therefore never reaches the model.
"""

from __future__ import annotations

import hashlib
import io
import warnings
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from .config import PipelineConfig

MAGIC = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
}
PIL_FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG"}


class InputRejected(Exception):
    """The upload failed an input check. ``code`` is a fixed identifier safe to log."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SanitizedImage:
    jpeg: bytes
    width: int
    height: int
    source_format: str
    source_width: int
    source_height: int


def check_declared(cfg: PipelineConfig, content_type: object, size: object, sha256: object) -> None:
    """Checks on the upload request, before a presigned POST is issued."""
    if content_type not in cfg.allowed_content_types:
        raise InputRejected("content_type_not_allowed")
    if not isinstance(size, int) or isinstance(size, bool) or not cfg.min_upload_bytes <= size <= cfg.max_upload_bytes:
        raise InputRejected("size_out_of_range")
    if not isinstance(sha256, str) or len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise InputRejected("sha256_malformed")


def sanitize(cfg: PipelineConfig, data: bytes, declared_type: str, declared_size: int | None = None,
             declared_sha256: str | None = None, target_side: int | None = None) -> SanitizedImage:
    if not cfg.min_upload_bytes <= len(data) <= cfg.max_upload_bytes:
        raise InputRejected("size_out_of_range")
    if declared_size is not None and len(data) != declared_size:
        raise InputRejected("size_mismatch")
    if declared_sha256 is not None and hashlib.sha256(data).hexdigest() != declared_sha256:
        raise InputRejected("sha256_mismatch")
    if declared_type not in cfg.allowed_content_types:
        raise InputRejected("content_type_not_allowed")
    if not any(data.startswith(m) for m in MAGIC[declared_type]):
        raise InputRejected("magic_mismatch")

    Image.MAX_IMAGE_PIXELS = cfg.max_image_pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                if probe.format != PIL_FORMATS[declared_type]:
                    raise InputRejected("format_mismatch")
                w, h = probe.size
                if max(w, h) > cfg.max_image_side or min(w, h) < cfg.min_image_side:
                    raise InputRejected("dimensions_out_of_range")
                if w * h > cfg.max_image_pixels:
                    raise InputRejected("too_many_pixels")
                if getattr(probe, "n_frames", 1) != 1:
                    raise InputRejected("animated_not_allowed")
                probe.verify()  # structural check; the image must be reopened afterwards
            with Image.open(io.BytesIO(data)) as img:
                img.load()
                img = ImageOps.exif_transpose(img)
                rgb = img.convert("RGB")
    except InputRejected:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise InputRejected("decompression_bomb") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise InputRejected("decode_failed") from None

    side = target_side or cfg.model_image_side
    rgb.thumbnail((side, side), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    # A fresh JPEG without exif/icc/comment: metadata from the upload is dropped here.
    rgb.save(out, format="JPEG", quality=90, optimize=True)
    return SanitizedImage(out.getvalue(), rgb.width, rgb.height, PIL_FORMATS[declared_type], w, h)
