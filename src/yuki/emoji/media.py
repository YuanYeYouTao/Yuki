"""Actual media facts for shared originals; no suitability classifier."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import struct
import warnings
import zlib
from typing import Any, BinaryIO

from PIL import Image, UnidentifiedImageError


def _ebml_value(stream: BinaryIO, *, identifier: bool = False) -> int:
    first = stream.read(1)
    if not first or not first[0]:
        raise ValueError("invalid_ebml")
    length = 9 - first[0].bit_length()
    tail = stream.read(length - 1)
    if len(tail) != length - 1:
        raise ValueError("incomplete_ebml")
    value = int.from_bytes(first + tail)
    return value if identifier else value & ((1 << (7 * length)) - 1)


def _ebml_format(stream: BinaryIO) -> str | None:
    stream.seek(4)
    try:
        size = _ebml_value(stream)
        end = stream.tell() + size
        while stream.tell() < end:
            identity = _ebml_value(stream, identifier=True)
            size = _ebml_value(stream)
            if identity == 0x4282:
                document = stream.read(min(size, 8))
                return (
                    {b"webm": "WEBM", b"matroska": "MKV"}.get(document)
                    if len(document) == size
                    else None
                )
            stream.seek(size, 1)
    except ValueError:
        return None
    return None


def media_facts(stream: BinaryIO, *, inspect_pixels: bool = True) -> dict[str, Any] | None:
    stream.seek(0)
    prefix = stream.read(16)
    stream.seek(0)
    if prefix.startswith(b"\x1aE\xdf\xa3"):
        kind = _ebml_format(stream)
        if kind is None:
            return None
        return {
            "image_format": kind,
            "mime_type": "video/webm" if kind == "WEBM" else "video/x-matroska",
            "animated": True,
        }
    if prefix.startswith(b"\x1f\x8b"):
        try:
            value = json.load(gzip.GzipFile(fileobj=stream))
        except (EOFError, OSError, ValueError, zlib.error):
            return None
        if isinstance(value, dict) and "layers" in value and "v" in value:
            return {
                "image_format": "TGS",
                "mime_type": "application/x-tgsticker",
                "animated": True,
                "width": value.get("w"),
                "height": value.get("h"),
            }
        return None
    # Original storage does not require pixels to fit a visual decoder's budget.
    # The real header remains useful even when optional Pillow inspection fails.
    known: dict[str, Any] | None = None
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        known = {"image_format": "PNG", "mime_type": "image/png"}
    elif prefix.startswith((b"GIF87a", b"GIF89a")):
        known = {"image_format": "GIF", "mime_type": "image/gif"}
    elif prefix.startswith(b"\xff\xd8\xff"):
        known = {"image_format": "JPEG", "mime_type": "image/jpeg"}
    elif prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        known = {"image_format": "WEBP", "mime_type": "image/webp"}
    elif prefix.startswith((b"II*\x00", b"MM\x00*")):
        known = {"image_format": "TIFF", "mime_type": "image/tiff"}
    elif prefix.startswith(b"BM"):
        known = {"image_format": "BMP", "mime_type": "image/bmp"}
    if not inspect_pixels:
        return known
    try:
        with warnings.catch_warnings(), Image.open(stream) as image:
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            kind = (image.format or "").upper()
            frames = int(getattr(image, "n_frames", 1))
            return {
                "image_format": kind,
                "mime_type": Image.MIME.get(kind, "application/octet-stream"),
                "width": image.width,
                "height": image.height,
                "frame_count": frames,
                "animated": frames > 1,
            }
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        struct.error,
    ):
        return known


def inspect_content(content: bytes) -> dict[str, Any]:
    facts = media_facts(io.BytesIO(content))
    if facts is None:
        facts = {"image_format": "BIN", "mime_type": "application/octet-stream"}
    return {**facts, "sha256": hashlib.sha256(content).hexdigest(), "byte_size": len(content)}


def inspect_path(
    fd: int, relative_path: str, *, inspect_pixels: bool = True
) -> dict[str, Any] | None:
    info = os.fstat(fd)
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while data := os.read(fd, 65536):
        digest.update(data)
    with os.fdopen(os.dup(fd), "rb") as stream:
        facts = media_facts(stream, inspect_pixels=inspect_pixels)
    after = os.fstat(fd)
    if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        return None
    return {
        **(facts or {"image_format": "BIN", "mime_type": "application/octet-stream"}),
        "sha256": digest.hexdigest(),
        "byte_size": info.st_size,
        "relative_path": relative_path,
    }


def extension(image_format: str) -> str:
    return {"JPEG": "jpg", "TIFF": "tiff"}.get(image_format, image_format.casefold())
