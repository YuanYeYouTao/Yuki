"""Actual media facts for shared originals; no suitability classifier."""

from __future__ import annotations

import hashlib
import io
import os
import struct
import warnings
import zlib
from typing import Any, BinaryIO


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


class _TgsSniff:
    """Top-level v/layers/w/h only. It does not validate the rest of the JSON or the gzip CRC."""

    def __init__(self) -> None:
        self.depth = 0
        self.started = False
        self.ended = False
        self.in_string = False
        self.escape = False
        self.unicode_left = 0
        self._unicode = ""
        self.expect_key = False
        self.collecting_key = False
        self.key = ""
        self.after_colon = False
        self.number = ""
        self.reading_number = False
        self.saw_v = False
        self.saw_layers = False
        self.width: int | None = None
        self.height: int | None = None

    def feed(self, data: bytes) -> bool:
        for raw in data:
            if (
                self.width is not None
                and self.height is not None
                and self.saw_v
                and self.saw_layers
            ):
                return True
            if self.ended:
                return self.ready
            char = chr(raw)
            if self.unicode_left:
                self.unicode_left -= 1
                if self.collecting_key:
                    self._unicode += char
                    if self.unicode_left == 0:
                        try:
                            self.key += chr(int(self._unicode, 16))
                        except ValueError:
                            pass
                        self._unicode = ""
                continue
            if self.in_string:
                if self.escape:
                    self.escape = False
                    if char == "u":
                        self.unicode_left = 4
                        self._unicode = ""
                        continue
                    if self.collecting_key and len(self.key) < 16:
                        self.key += {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}.get(
                            char, char
                        )
                    continue
                if char == "\\":
                    self.escape = True
                    continue
                if char == '"':
                    self.in_string = False
                    if self.collecting_key:
                        self.collecting_key = False
                        self.expect_key = False
                    elif self.depth == 1 and self.after_colon:
                        self._note_scalar()
                    continue
                if self.collecting_key:
                    if len(self.key) < 8:
                        self.key += char
                    else:
                        self.collecting_key = False
                        self.key = ""
                continue
            if char in " \t\r\n":
                if self.reading_number:
                    self._finish_number()
                continue
            if not self.started:
                if char != "{":
                    self.ended = True
                    return False
                self.started = True
                self.depth = 1
                self.expect_key = True
                continue
            if self.reading_number and char not in "-0123456789.eE+":
                self._finish_number()
            if char == '"':
                self.in_string = True
                if self.depth == 1 and self.expect_key:
                    self.collecting_key = True
                    self.key = ""
                continue
            if char == ":" and self.depth == 1:
                self.after_colon = True
                self.expect_key = False
                continue
            if char == "{":
                if self.depth == 1 and self.after_colon:
                    self._note_container()
                self.depth += 1
                self.after_colon = False
                continue
            if char == "[":
                if self.depth == 1 and self.after_colon:
                    self._note_container()
                self.depth += 1
                self.after_colon = False
                continue
            if char == "}":
                self.depth -= 1
                if self.depth == 0:
                    self.ended = True
                    return self.ready
                if self.depth == 1:
                    self.expect_key = True
                continue
            if char == "]":
                self.depth -= 1
                if self.depth == 1:
                    self.expect_key = True
                continue
            if char == ",":
                if self.depth == 1:
                    self.expect_key = True
                    self.after_colon = False
                continue
            if self.depth == 1 and self.after_colon and char in "-0123456789":
                self.reading_number = True
                self.number += char
                continue
            if self.depth == 1 and self.after_colon and char in "tfn":
                self._note_scalar()
        return self.ready

    def _note_container(self) -> None:
        if self.key == "v":
            self.saw_v = True
        elif self.key == "layers":
            self.saw_layers = True
        self.key = ""
        self.after_colon = False

    def _note_scalar(self) -> None:
        if self.key in {"v", "layers"}:
            if self.key == "v":
                self.saw_v = True
            else:
                self.saw_layers = True
        self.key = ""
        self.after_colon = False

    def _finish_number(self) -> None:
        if self.key == "w":
            self.width = _plain_int(self.number)
        elif self.key == "h":
            self.height = _plain_int(self.number)
        elif self.key == "v":
            self.saw_v = True
        elif self.key == "layers":
            self.saw_layers = True
        self.number = ""
        self.reading_number = False
        self.key = ""
        self.after_colon = False

    @property
    def ready(self) -> bool:
        return (
            self.saw_v
            and self.saw_layers
            and (self.width is not None or self.height is not None or self.ended)
        )


def _plain_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _tgs_header(stream: BinaryIO) -> dict[str, Any] | None:
    stream.seek(0)
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    sniff = _TgsSniff()
    try:
        pending = b""
        complete = False
        while not complete:
            complete = (
                sniff.saw_v
                and sniff.saw_layers
                and sniff.width is not None
                and sniff.height is not None
            )
            if complete:
                break
            if not pending:
                pending = stream.read(4096)
                if not pending:
                    sniff.feed(decoder.flush())
                    break
            produced = decoder.decompress(pending, 256)
            pending = decoder.unconsumed_tail
            if produced:
                sniff.feed(produced)
                continue
            more = stream.read(4096)
            if not more:
                sniff.feed(decoder.flush())
                break
            pending += more
    except zlib.error:
        if not (sniff.saw_v and sniff.saw_layers):
            return None
    if not (sniff.started and sniff.saw_v and sniff.saw_layers):
        return None
    return {
        "image_format": "TGS",
        "mime_type": "application/x-tgsticker",
        "animated": True,
        "width": sniff.width,
        "height": sniff.height,
    }


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
        return _tgs_header(stream)
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
    from PIL import Image, UnidentifiedImageError

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


def header_facts(stream: BinaryIO) -> dict[str, Any] | None:
    """Name a file from its header. GIF frames are not enumerated."""
    facts = media_facts(stream, inspect_pixels=False)
    if facts is not None and facts.get("image_format") in {"TGS", "WEBM", "MKV"}:
        return facts
    if facts is None:
        facts = {}
    from PIL import Image, UnidentifiedImageError

    stream.seek(0)
    try:
        with warnings.catch_warnings(), Image.open(stream) as image:
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            kind = (image.format or facts.get("image_format") or "").upper()
            return {
                **facts,
                "image_format": kind or facts.get("image_format", "BIN"),
                "mime_type": Image.MIME.get(
                    kind, facts.get("mime_type", "application/octet-stream")
                ),
                "width": image.width,
                "height": image.height,
            }
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return facts or None


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
