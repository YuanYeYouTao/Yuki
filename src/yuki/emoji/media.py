"""Actual media facts for shared originals; no suitability classifier."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
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


_STRUCTURE = re.compile(rb'["{}\[\],:]')
_NONSPACE = re.compile(rb"\S")
_NUMBER_PART = re.compile(rb"[-+0-9.eE]+")
_JSON_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")


class _TgsSniff:
    """Read root facts; skip unrelated strings and scalar values in C-sized spans."""

    def __init__(self) -> None:
        self.depth = 0
        self.started = False
        self.ended = False
        self.in_string = False
        self.escape = False
        self.unicode_left = 0
        self._unicode = ""
        self.expect_key = False
        self.key_string = False
        self.collecting_key = False
        self.key = ""
        self.after_colon = False
        self.number = ""
        self.reading_number = False
        self.saw_v = False
        self.saw_layers = False
        self.width: int | float | None = None
        self.height: int | float | None = None

    def _key_part(self, value: str) -> None:
        if self.collecting_key:
            if len(self.key) + len(value) <= 6:
                self.key += value
            else:
                self.key = ""
                self.collecting_key = False

    def feed(self, data: bytes) -> bool:
        index, size = 0, len(data)
        quote_index = None
        while index < size:
            if self.complete or self.ended:
                return self.ready
            if self.reading_number:
                found = _NUMBER_PART.match(data, index)
                if found:
                    self.number += found[0].decode("ascii")
                    index = found.end()
                    if index == size:
                        break
                self._finish_number()
                continue
            if self.in_string:
                if self.unicode_left:
                    length = min(self.unicode_left, size - index)
                    if self.collecting_key:
                        self._unicode += data[index : index + length].decode("latin1")
                    self.unicode_left -= length
                    index += length
                    if not self.unicode_left and self.collecting_key:
                        try:
                            self._key_part(chr(int(self._unicode, 16)))
                        except ValueError:
                            self.collecting_key = False
                            self.key = ""
                        self._unicode = ""
                    continue
                if self.escape:
                    char = chr(data[index])
                    index += 1
                    self.escape = False
                    if char == "u":
                        self.unicode_left = 4
                        self._unicode = ""
                    else:
                        self._key_part(
                            {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}.get(char, char)
                        )
                    continue
                if quote_index is None or (quote_index >= 0 and quote_index < index):
                    quote_index = data.find(b'"', index)
                quote_end = quote_index if quote_index >= 0 else size
                escape_index = data.find(b"\\", index, quote_end)
                end = escape_index if escape_index >= 0 else quote_end
                if self.collecting_key:
                    # No unrelated key or value is accumulated across chunks.
                    length = end - index
                    if len(self.key) + length <= 6:
                        self._key_part(data[index:end].decode("latin1"))
                    else:
                        self.collecting_key = False
                        self.key = ""
                index = end
                if end == size:
                    break
                index += 1
                if escape_index >= 0:
                    self.escape = True
                else:
                    self.in_string = False
                    quote_index = None
                    if self.key_string:
                        self.key_string = False
                        self.collecting_key = False
                        self.expect_key = False
                    elif self.depth == 1 and self.after_colon:
                        self._note_scalar()
                continue
            if not self.started or (self.depth == 1 and self.after_colon):
                token = _NONSPACE.search(data, index)
            else:
                token = _STRUCTURE.search(data, index)
            if token is None:
                break
            index = token.start() + 1
            char = chr(data[token.start()])
            if not self.started:
                if char != "{":
                    self.ended = True
                    return False
                self.started = True
                self.depth = 1
                self.expect_key = True
                continue
            if char == '"':
                self.in_string = True
                self.key_string = self.depth == 1 and self.expect_key
                self.collecting_key = self.key_string
                if self.key_string:
                    self.key = ""
                continue
            if char == ":" and self.depth == 1:
                self.after_colon = True
                self.expect_key = False
                continue
            if char in "{[":
                if self.depth == 1 and self.after_colon:
                    self._note_scalar()
                self.depth += 1
                self.after_colon = False
                continue
            if char in "}]":
                self.depth -= 1
                if self.depth == 0:
                    self.ended = True
                    return self.ready
                if self.depth == 1:
                    self.expect_key = True
                continue
            if char == ",":
                if self.depth == 1:
                    self.expect_key = True
                    self.after_colon = False
                continue
            if self.depth == 1 and self.after_colon:
                if self.key in {"w", "h"} and char in "-0123456789":
                    self.reading_number = True
                    self.number = char
                else:
                    self._note_scalar()
        return self.ready

    def _note_scalar(self) -> None:
        if self.key == "v":
            self.saw_v = True
        elif self.key == "layers":
            self.saw_layers = True
        self.key = ""
        self.after_colon = False

    def _finish_number(self) -> None:
        value = None
        if _JSON_NUMBER.fullmatch(self.number):
            value = json.loads(self.number)
            if isinstance(value, float) and value.is_integer():
                value = int(value)
        if self.key == "w":
            self.width = value
        else:
            self.height = value
        self.number = ""
        self.reading_number = False
        self.key = ""
        self.after_colon = False

    @property
    def complete(self) -> bool:
        return self.saw_v and self.saw_layers and self.width is not None and self.height is not None

    @property
    def ready(self) -> bool:
        return (
            self.saw_v
            and self.saw_layers
            and (self.width is not None or self.height is not None or self.ended)
        )


def _gzip_payload(stream: BinaryIO) -> bool:
    """Position at raw deflate, leaving optional header fields and CRC behind."""
    stream.seek(0)
    header = stream.read(10)
    if len(header) != 10 or header[:3] != b"\x1f\x8b\x08":
        return False
    flags = header[3]
    if flags & 4:
        length = stream.read(2)
        if len(length) != 2:
            return False
        stream.seek(int.from_bytes(length, "little"), 1)
    for flag in (8, 16):
        if flags & flag:
            while True:
                part = stream.read(4096)
                if not part:
                    return False
                end = part.find(b"\0")
                if end >= 0:
                    stream.seek(end + 1 - len(part), 1)
                    break
    if flags & 2:
        stream.seek(2, 1)
    return True


def _tgs_header(stream: BinaryIO) -> dict[str, Any] | None:
    if not _gzip_payload(stream):
        return None
    decoder = zlib.decompressobj(-zlib.MAX_WBITS)
    sniff = _TgsSniff()
    try:
        while not (sniff.complete or sniff.ended or decoder.eof):
            pending = stream.read(4096)
            if not pending:
                break
            full = False
            while (pending or full) and not (sniff.complete or sniff.ended):
                produced = decoder.decompress(pending, 65536)
                sniff.feed(produced)
                pending = decoder.unconsumed_tail
                full = len(produced) == 65536
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
