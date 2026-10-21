"""Extract a compact model-facing summary from OneBot share-card segments."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from yuki.services.renderer import sanitize_input

_MAX_CARD_JSON_CHARACTERS = 128_000
_MAX_FIELD_CHARACTERS = 500


@dataclass(frozen=True, slots=True)
class CardPreview:
    """Safe, bounded metadata projected from an untrusted share card."""

    text: str
    summary: str
    url: str | None = None


def parse_card_segment(segment_type: str, data: dict[str, Any]) -> CardPreview | None:
    """Parse known JSON cards without treating their payload as trusted instructions."""

    if segment_type != "json":
        return None
    payload = _json_payload(data.get("data"))
    if payload is None:
        return None

    prompt = _bounded_string(payload.get("prompt"))
    metadata = _first_metadata(payload.get("meta"))
    title = _bounded_string(metadata.get("title"))
    description = _bounded_string(metadata.get("desc") or metadata.get("description"))
    provider = _bounded_string(metadata.get("tag") or metadata.get("source"))
    url = _bounded_string(
        metadata.get("jumpUrl")
        or metadata.get("jump_url")
        or metadata.get("url")
        or payload.get("jumpUrl")
    )
    resource_type, resource_id = _netease_resource(url, prompt)

    fields: list[str] = []
    heading = "用户分享了一张卡片"
    if resource_type == "album":
        heading = "用户分享了一个网易云专辑"
    elif resource_type == "song":
        heading = "用户分享了一首网易云歌曲"
    fields.append(f"[{heading}]")
    if title:
        fields.append(f"标题：{title}")
    elif prompt:
        fields.append(f"标题：{prompt}")
    if description:
        fields.append(f"说明：{description}")
    if provider:
        fields.append(f"来源：{provider}")
    if resource_type and resource_id:
        fields.append(f"网易云{('专辑' if resource_type == 'album' else '歌曲')} ID：{resource_id}")
    if url:
        fields.append(f"链接：{url}")
    if len(fields) == 1:
        return None
    text = sanitize_input("\n".join(fields))[:2_000]
    summary = title or prompt or provider or "分享卡片"
    return CardPreview(text=text, summary=summary[:_MAX_FIELD_CHARACTERS], url=url)


def _json_payload(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value or len(value) > _MAX_CARD_JSON_CHARACTERS:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return dict(parsed) if isinstance(parsed, dict) else None


def _first_metadata(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    for key in ("news", "music", "detail_1", "miniapp"):
        candidate = value.get(key)
        if isinstance(candidate, dict):
            return dict(candidate)
    for candidate in value.values():
        if isinstance(candidate, dict):
            return dict(candidate)
    return {}


def _bounded_string(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return sanitize_input(value)[:_MAX_FIELD_CHARACTERS]


def _netease_resource(url: str, prompt: str) -> tuple[str | None, str | None]:
    if not url:
        return None, None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None, None
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    if hostname not in {"music.163.com", "y.music.163.com"}:
        return None, None
    path = parsed.path.casefold()
    resource_type: str | None = None
    if "/album" in path or "专辑" in prompt:
        resource_type = "album"
    elif "/song" in path or "歌曲" in prompt:
        resource_type = "song"
    values = parse_qs(parsed.query).get("id", ())
    resource_id = values[0] if values and values[0].isdigit() else None
    return resource_type, resource_id
