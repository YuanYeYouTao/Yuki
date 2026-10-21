"""Copy diagnostics without preserving transport media or opaque provider secrets."""

from __future__ import annotations

import gzip
import hashlib
import json
import zlib
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from typing import Any

OPAQUE_KEYS = frozenset({"encrypted_content", "signature", "thoughtSignature"})


class PayloadCapacityError(ValueError):
    """The raw diagnostic is not admitted; its encoded size/hash are unknown."""


@dataclass(frozen=True, slots=True)
class HTTPResponseSnapshot:
    status: int
    content: bytes


def freeze_payload(value: object, capacity: int) -> tuple[object, int]:
    """Bound BEFORE copying. Reserve snapshot, codec copies and temporary output.

    The conservative bound includes six JSON characters per input character,
    UTF-8/string/compression temporaries and container/redaction path overhead.
    The first pass creates no independent payload. The second pass only copies
    an admitted tree; immutable strings/bytes need not be duplicated.
    """
    import sys

    used, nodes = 512 * 1024, 0
    ancestors: set[int] = set()

    def inspect(item: Any, depth: int = 0, path_chars: int = 1) -> None:
        nonlocal used, nodes
        nodes += 1
        if depth > 48 or nodes > 65536:
            raise PayloadCapacityError("diagnostic_structure_limit")
        used += 512 + sys.getsizeof(item) + path_chars * 30
        if isinstance(item, HTTPResponseSnapshot):
            # Parsing arbitrary JSON can produce many small Python objects.
            used += len(item.content) * 160
        elif isinstance(item, str):
            used += len(item) * 30
        elif isinstance(item, Enum):
            inspect(item.value, depth + 1, path_chars)
        elif isinstance(item, (dict, list, tuple)) or (
            is_dataclass(item) and not isinstance(item, type)
        ):
            count = len(fields(item)) if is_dataclass(item) else len(item)
            if used + count * 512 > capacity or id(item) in ancestors:
                raise PayloadCapacityError("diagnostic_structure_limit")
            ancestors.add(id(item))
            try:
                if isinstance(item, dict):
                    for key, child in item.items():
                        if not isinstance(key, str):
                            raise TypeError("diagnostic_key_type")
                        inspect(key, depth + 1, path_chars)
                        inspect(child, depth + 1, path_chars + len(key) + 1)
                elif is_dataclass(item):
                    for field in fields(item):
                        inspect(field.name, depth + 1, path_chars)
                        inspect(
                            getattr(item, field.name), depth + 1, path_chars + len(field.name) + 1
                        )
                else:
                    for child in item:
                        inspect(child, depth + 1, path_chars + 20)
            finally:
                ancestors.remove(id(item))
        elif item is not None and not isinstance(item, (bool, int, float)):
            raise TypeError("diagnostic_payload_type")
        if used > capacity:
            raise PayloadCapacityError("diagnostic_raw_capacity")

    inspect(value)

    def copy(item: Any) -> Any:
        if isinstance(item, Enum):
            return copy(item.value)
        if isinstance(item, dict):
            return {key: copy(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [copy(child) for child in item]
        if is_dataclass(item) and not isinstance(item, (type, HTTPResponseSnapshot)):
            return {field.name: copy(getattr(item, field.name)) for field in fields(item)}
        return item

    return copy(value), used


def _reference(value: object, reason: str) -> dict[str, object]:
    raw = json.dumps(value, ensure_ascii=False, default=str).encode()
    return {"trace_omitted": reason, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _copy(value: Any, redactions: list[str], path: str = "$") -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        # JSON Schema allows an array in `type`; only provider media blocks
        # use string type tags for the redaction rules below.
        raw_type = value.get("type")
        block_type = raw_type if isinstance(raw_type, str) else None
        for key, item in value.items():
            child = f"{path}.{key}"
            opaque = key in OPAQUE_KEYS or (
                block_type in {"redacted_thinking", "reasoning.encrypted"} and key == "data"
            )
            media = (
                key in {"inlineData", "inline_data", "fileData", "file_data", "data_url"}
                or (key == "image_url")
                or (
                    block_type in {"image", "input_image", "input_audio"}
                    and key in {"source", "url", "data", "image_url"}
                )
            )
            if opaque or media:
                redactions.append(child)
                result[key] = _reference(
                    item, "opaque_provider_state" if opaque else "transport_media"
                )
            else:
                result[key] = _copy(item, redactions, child)
        return result
    if isinstance(value, (list, tuple)):
        return [_copy(item, redactions, f"{path}[{i}]") for i, item in enumerate(value)]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"unsupported trace payload type: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class EncodedPayload:
    status: str
    compressed: bytes | None
    sha256: str
    size: int


def encode_payload(value: object, limit: int) -> EncodedPayload:
    if isinstance(value, HTTPResponseSnapshot):
        try:
            body = json.loads(value.content)
        except (ValueError, UnicodeError):
            body = {"trace_omitted": "non_json_response", "bytes": len(value.content)}
        value = {"http_status": value.status, "dispatch": "response_received", "body": body}
    redactions: list[str] = []
    copied = _copy(value, redactions)
    raw = json.dumps(
        {"data": copied, "redactions": redactions},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) > limit:
        return EncodedPayload("omitted_size", None, digest, len(raw))
    return EncodedPayload(
        "redacted" if redactions else "recorded", gzip.compress(raw, mtime=0), digest, len(raw)
    )


def decode_payload(blob: bytes, *, size: int, digest: str) -> dict[str, Any]:
    # Bound expansion even when a diagnostic row has been corrupted.
    if size < 0 or size > 64 * 1024 * 1024:
        raise ValueError("invalid trace payload size")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    raw = decoder.decompress(blob, size + 1)
    if len(raw) != size or not decoder.eof or decoder.unused_data:
        raise ValueError("invalid trace payload compression")
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("invalid trace payload digest")
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"data", "redactions"}:
        raise ValueError("invalid trace payload envelope")
    return value
