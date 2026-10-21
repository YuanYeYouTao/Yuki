"""Request capacity policy shared by foreground and persistent workers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING

from yuki.domain.messages import (
    ChatImage,
    ChatMessage,
    FunctionCallOutput,
    ProviderContinuation,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from yuki.domain.messages import ChatRequest, ChatTool


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

    def input_budget(self, *, output_tokens: int | None = None) -> int | None:
        limits = []
        if self.input_tokens is not None:
            limits.append(self.input_tokens)
        if self.context_tokens is not None:
            limits.append(
                self.context_tokens
                - (self.output_tokens if output_tokens is None else output_tokens)
            )
        return min(limits) if limits else None


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
    return _estimate_input_tokens(_request_input(request))


def _request_input(request: ChatRequest) -> dict[str, object]:
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
    return value


@dataclass(frozen=True, slots=True)
class InputTokenCost:
    """Additive serialized character/media counts; round only the whole input."""

    ascii: int = 0
    non_ascii: int = 0
    media: int = 0

    def __add__(self, other: InputTokenCost) -> InputTokenCost:
        return InputTokenCost(
            self.ascii + other.ascii, self.non_ascii + other.non_ascii, self.media + other.media
        )

    @property
    def tokens(self) -> int:
        return (self.ascii + 2) // 3 + self.non_ascii * 2 + self.media


@dataclass(frozen=True, slots=True)
class InputSequenceCost:
    """JSON list contents, retaining the exact delimiter and whole-input rounding."""

    items: InputTokenCost = InputTokenCost()
    count: int = 0

    def __add__(self, other: InputSequenceCost) -> InputSequenceCost:
        return InputSequenceCost(self.items + other.items, self.count + other.count)

    @property
    def interior(self) -> InputTokenCost:
        return self.items + InputTokenCost(ascii=max(0, self.count - 1) * 2)


def message_token_cost(message: ChatMessage) -> InputSequenceCost:
    return InputSequenceCost(_input_token_cost(_message_input(message)), 1)


_EMPTY_SEQUENCE = InputSequenceCost()


class RequestTokenCounter:
    """Scan one transcript once, following its actual checkpoint replacement rules.

    Settings/declarations are fixed by the caller's normalized request. A new
    opaque checkpoint replaces the earlier checkpoint and delta, so estimates
    need not be monotonic; no candidate protocol is flattened or approximated.
    """

    def __init__(self, request: ChatRequest) -> None:
        self._base = _input_token_cost(
            _request_input(replace(request, messages=(), continuation=None, continuation_items=()))
        )
        self.messages = InputSequenceCost()
        self._continuation: InputTokenCost | None = None
        self._items = InputSequenceCost()

    def append(self, entry: ChatMessage | FunctionCallOutput | ProviderContinuation) -> None:
        if isinstance(entry, ProviderContinuation):
            self._continuation = _input_token_cost(entry)
            self._items = InputSequenceCost()
        elif self._continuation is None:
            if isinstance(entry, ChatMessage):
                self.messages += message_token_cost(entry)
        else:
            self._items += (
                message_token_cost(entry)
                if isinstance(entry, ChatMessage)
                else InputSequenceCost(_input_token_cost(asdict(entry)), 1)
            )

    def estimate(self, extra: InputSequenceCost = _EMPTY_SEQUENCE) -> int:
        addition = extra
        messages = self.messages + addition if self._continuation is None else self.messages
        total = self._base + messages.interior
        if self._continuation is not None:
            total += InputTokenCost(ascii=len(', "continuation": ')) + self._continuation
        items = self._items + addition if self._continuation is not None else self._items
        if items.count:
            total += InputTokenCost(ascii=len(', "continuation_items": []')) + items.interior
        return total.tokens

    def estimate_messages(self, messages: InputSequenceCost) -> int:
        """A fresh portable request under the same normalized settings."""
        return (self._base + messages.interior).tokens


def _estimate_input_tokens(value: object) -> int:
    return _input_token_cost(value).tokens


def _input_token_cost(value: object) -> InputTokenCost:
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
    ascii_count = sum(ord(character) < 128 for character in serialized)
    return InputTokenCost(ascii_count, len(serialized) - ascii_count, media_tokens)
