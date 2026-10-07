"""Request capacity policy shared by foreground and persistent workers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from qq_ai_bot.domain.messages import ChatImage, ChatMessage, ProviderContinuation

if TYPE_CHECKING:
    from collections.abc import Sequence

    from qq_ai_bot.domain.messages import ChatRequest, ChatTool


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


def _tool_input(tool: ChatTool) -> dict[str, object]:
    return {"name": tool.name, "description": tool.description, "parameters": tool.parameters}


def estimate_tools_tokens(tools: Sequence[ChatTool]) -> int:
    """Count the frozen model declarations, excluding host catalog metadata."""
    # Schema property names such as data_url are declarations, never media.
    return estimate_text_tokens(
        json.dumps([_tool_input(tool) for tool in tools], ensure_ascii=False, default=str)
    )


def _message_input(message: ChatMessage) -> dict[str, object]:
    if message.response_item is not None:
        # Preserve the original opaque item rather than its budgeting mirror.
        # The serializer sends that item; never decode its private signatures.
        return {"response_item": message.response_item}
    value: dict[str, object] = {"role": message.role}
    if message.content is not None:
        value["content"] = message.content
    if message.tool_calls:
        value["tool_calls"] = [asdict(call) for call in message.tool_calls]
    if message.tool_call_id is not None:
        value["tool_call_id"] = message.tool_call_id
    if message.reasoning_content is not None:
        value["reasoning_content"] = message.reasoning_content
    if message.images:
        value["images"] = list(message.images)
    return value


def estimate_request_tokens(request: ChatRequest) -> int:
    """Estimate model input, including schemas, media and complete opaque state.

    Host catalog fields and request diagnostics are never sent to a model. This
    accounting view does not modify the submitted request or provider protocol.
    """
    value: dict[str, object] = {
        "messages": [_message_input(message) for message in request.messages],
        "model": request.model,
    }
    for name in (
        "temperature",
        "max_output_tokens",
        "thinking_enabled",
        "reasoning_effort",
        "tool_choice",
        "response_format",
    ):
        item = getattr(request, name)
        if item is not None:
            value[name] = item
    if request.structured_output:
        value["structured_output"] = True
    if request.tools:
        value["tools"] = [_tool_input(tool) for tool in request.tools]
    if request.native_tools:
        value["native_tools"] = [asdict(tool) for tool in request.native_tools]
    if request.continuation is not None:
        value["continuation"] = request.continuation
    if request.continuation_items:
        value["continuation_items"] = [
            _message_input(item) if isinstance(item, ChatMessage) else asdict(item)
            for item in request.continuation_items
        ]
    return _estimate_input_tokens(value)


def _estimate_input_tokens(value: object) -> int:
    media_tokens = 0
    model_input = value

    def without_binary(value: object) -> object:
        nonlocal media_tokens
        if isinstance(value, ChatImage):
            media_tokens += 4096
            return {"data_url": "[prepared media]"}
        if isinstance(value, ProviderContinuation):
            payload = value.payload
            if value.protocol != "gemini" or not isinstance(payload, tuple):
                return payload
            # Only Gemini content.parts media slots are prepared binary input.
            # Business JSON inside calls/results and all unknown opaque shapes
            # stay complete; no recursive interpretation of their field names.
            contents: list[object] = []
            for content in payload:
                if (
                    not isinstance(content, dict)
                    or content.get("role") not in ("user", "model")
                    or not isinstance(content.get("parts"), list)
                ):
                    contents.append(content)
                    continue
                parts: list[object] = []
                for part in content["parts"]:
                    if not isinstance(part, dict):
                        parts.append(part)
                        continue
                    prepared = dict(part)
                    for key in ("inlineData", "inline_data"):
                        media = part.get(key)
                        if isinstance(media, dict) and isinstance(media.get("data"), str):
                            media_tokens += 4096
                            prepared[key] = "[prepared media]"
                    parts.append(prepared)
                contents.append({**content, "parts": parts})
            return contents
        if isinstance(value, dict):
            result: dict[str, object] = {}
            for key, item in value.items():
                if value is model_input and key in {"tools", "response_format"}:
                    # These known request fields hold complete declarations,
                    # not media. Their schemas must remain fully accounted.
                    result[key] = item
                else:
                    result[key] = without_binary(item)
            return result
        if isinstance(value, (list, tuple)):
            return [without_binary(item) for item in value]
        return value

    serialized = json.dumps(without_binary(value), ensure_ascii=False, default=str)
    return estimate_text_tokens(serialized) + media_tokens
