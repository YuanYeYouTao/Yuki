"""Request capacity policy shared by foreground and persistent workers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qq_ai_bot.domain.messages import ChatRequest


def estimate_text_tokens(text: str) -> int:
    """Conservative local estimate, never a provider's exact token count."""
    # Non-ASCII text and opaque protocol signatures are deliberately expensive.
    ascii_count = sum(ord(character) < 128 for character in text)
    return (ascii_count + 2) // 3 + (len(text) - ascii_count) * 2


@dataclass(frozen=True)
class ModelCapacity:
    input_tokens: int | None = None
    context_tokens: int | None = None
    output_tokens: int = 8192

    def input_budget(self, active_tokens: int = 262144, *, output_tokens: int | None = None) -> int:
        limits = [active_tokens]
        if self.input_tokens is not None:
            limits.append(self.input_tokens)
        if self.context_tokens is not None:
            limits.append(self.context_tokens - (output_tokens or self.output_tokens))
        return max(1, min(limits))


def estimate_request_tokens(request: ChatRequest) -> int:
    """Count the complete neutral request, including tools and opaque items."""
    media_tokens = 0

    def without_binary(value: object) -> object:
        nonlocal media_tokens
        if isinstance(value, dict):
            result: dict[str, object] = {}
            for key, item in value.items():
                if key in {"data_url", "inlineData", "inline_data"}:
                    media_tokens += 4096
                    result[key] = "[prepared media]"
                else:
                    result[key] = without_binary(item)
            return result
        if isinstance(value, (list, tuple)):
            return [without_binary(item) for item in value]
        return value

    value = json.dumps(without_binary(asdict(request)), ensure_ascii=False, default=str)
    return estimate_text_tokens(value) + media_tokens
