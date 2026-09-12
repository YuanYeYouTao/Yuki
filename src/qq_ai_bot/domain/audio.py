"""Durable ASR data keeps quoted speakers distinct from the current speaker."""

import json
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class AudioTranscript:
    source: Literal["current", "reply"]
    segment_index: int
    text: str


def serialize_transcripts(items: tuple[AudioTranscript, ...]) -> str:
    return (
        json.dumps(
            [
                {"source": item.source, "segment_index": item.segment_index, "text": item.text}
                for item in items
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if items
        else ""
    )


def parse_transcripts(value: str) -> tuple[AudioTranscript, ...]:
    if not value:
        return ()
    if len(value) > 80000:
        raise ValueError("audio transcript exceeds its bound")
    items = json.loads(value)
    if not isinstance(items, list) or not 1 <= len(items) <= 3:
        raise ValueError("invalid audio transcript list")
    result = []
    for item in items:
        if (
            not isinstance(item, dict)
            or item.get("source") not in ("current", "reply")
            or not isinstance(item.get("segment_index"), int)
            or item["segment_index"] < 0
            or not isinstance(item.get("text"), str)
            or not 0 < len(item["text"]) <= 12000
        ):
            raise ValueError("invalid audio transcript")
        result.append(AudioTranscript(item["source"], item["segment_index"], item["text"]))
    return tuple(result)


def transcript_context(value: str, *, include_replies: bool = True) -> str:
    parts = []
    for item in parse_transcripts(value):
        if item.source == "reply" and not include_replies:
            continue
        label = "回复消息中的语音" if item.source == "reply" else "当前消息的语音"
        parts.append(f"[{label}转写（自动识别，可能有误）]\n{item.text}")
    return "\n\n".join(parts)
