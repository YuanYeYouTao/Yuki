"""Copy diagnostics without preserving transport media or opaque provider secrets."""

from __future__ import annotations

import gzip
import hashlib
import json
import zlib
from dataclasses import dataclass
from enum import Enum
from typing import Any

OPAQUE_KEYS = frozenset({"encrypted_content", "signature", "thoughtSignature"})


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
