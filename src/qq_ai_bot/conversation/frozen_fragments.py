"""Keep submitted input fragments intact while selecting new ledger events."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

from qq_ai_bot.conversation.projections import ProjectionConflict
from qq_ai_bot.domain.messages import ChatMessage, ProviderContinuation, ToolCall, ToolFunction

EventFragment = tuple[tuple[int, ...], ChatMessage]


@dataclass(frozen=True, slots=True)
class FrozenFragments:
    """Model-input copies, explicitly distinct from platform delivery records."""

    items: tuple[dict[str, Any], ...]

    @property
    def event_ids(self) -> frozenset[int]:
        return frozenset(event_id for item in self.items for event_id in item["event_ids"])

    def messages(self) -> tuple[ChatMessage, ...]:
        return tuple(_message(item["message"]) for item in self.items)

    @classmethod
    def load(cls, items: list[dict[str, Any]]) -> FrozenFragments:
        seen: set[int] = set()
        for item in items:
            if set(item) != {"kind", "event_ids", "message"}:
                raise ProjectionConflict("unsupported frozen fragment schema")
            if item["kind"] != "model_input":
                raise ProjectionConflict("fragment is not a model input")
            ids = item["event_ids"]
            if not isinstance(ids, list) or any(type(i) is not int or i <= 0 for i in ids):
                raise ProjectionConflict("invalid fragment event identifiers")
            if len(set(ids)) != len(ids) or seen.intersection(ids):
                raise ProjectionConflict("duplicate frozen event")
            seen.update(ids)
            message = item["message"]
            if (
                not isinstance(message, dict)
                or not {"role", "content"} <= set(message)
                or not set(message)
                <= {"role", "content", "tool_calls", "tool_call_id", "response_item"}
                or message["role"] not in {"user", "assistant", "tool", "system"}
                or (message["content"] is not None and not isinstance(message["content"], str))
            ):
                raise ProjectionConflict("unsupported frozen message representation")
            try:
                _message(message)
            except (TypeError, ValueError, KeyError) as exc:
                raise ProjectionConflict("invalid frozen tool message") from exc
        return cls(tuple(deepcopy(items)))

    def extend_history(
        self,
        grouped: tuple[EventFragment, ...],
        individual: tuple[EventFragment, ...],
    ) -> FrozenFragments:
        """Split only newly rendered groups that overlap an already frozen batch.

        The individual views were rendered with the same selected reference table
        as the batch. No old fragment is rendered again, even when a nickname or
        newly visible reference changes the new batch's representation.
        """
        covered = set(self.event_ids)
        result = list(deepcopy(self.items))
        for ids, message in grouped:
            fresh = set(ids).difference(covered)
            if not fresh:
                continue
            selected = (
                ((ids, message),)
                if fresh == set(ids)
                else tuple((keys, value) for keys, value in individual if set(keys) <= fresh)
            )
            selected_ids = [key for keys, _ in selected for key in keys]
            if set(selected_ids) != fresh or len(selected_ids) != len(fresh):
                raise ProjectionConflict("new event fragment coverage is incomplete")
            for keys, value in selected:
                result.append(_input(keys, value))
            covered.update(fresh)
        return self.load(result)

    def append_current(self, event_id: int | None, message: ChatMessage) -> FrozenFragments:
        """Freeze the compiled current input, including its exact dynamic envelope."""
        if event_id is not None and event_id in self.event_ids:
            raise ProjectionConflict("trigger already exists in frozen input")
        return self.load(
            [
                *deepcopy(self.items),
                _input((event_id,) if event_id is not None else (), message),
            ]
        )

    def append_protocol(self, messages: tuple[ChatMessage, ...]) -> FrozenFragments:
        return self.load([*deepcopy(self.items), *(_input((), m) for m in messages)])

    def append_responses(self, continuation: ProviderContinuation) -> FrozenFragments:
        if continuation.protocol != "responses" or not isinstance(continuation.payload, tuple):
            raise ProjectionConflict("unsupported Responses replay")
        messages = []
        for item in continuation.payload:
            if not isinstance(item, dict) or item.get("type") not in {
                "function_call",
                "function_call_output",
                "message",
                "web_search_call",
            }:
                raise ProjectionConflict("Responses item requires an explicit boundary")
            content = item.get("content")
            if isinstance(content, list) and any(
                not isinstance(part, dict)
                or part.get("type") not in {"input_text", "output_text", "text"}
                for part in content
            ):
                raise ProjectionConflict("Responses media requires an explicit boundary")
            messages.append(
                ChatMessage(
                    role="user",
                    # Used for context budgeting and hashing only. The Responses
                    # serializer emits the tagged original item instead of this text.
                    content=json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                    response_item=ProviderContinuation(
                        provider=continuation.provider,
                        protocol=continuation.protocol,
                        profile_id=continuation.profile_id,
                        payload=(deepcopy(item),),
                    ),
                )
            )
        return self.append_protocol(tuple(messages))


def _input(ids: tuple[int, ...], message: ChatMessage) -> dict[str, Any]:
    # Ephemeral images and provider reasoning are not silently stripped: callers
    # must establish an explicit representation boundary before persisting them.
    if message.images or message.reasoning_content:
        raise ProjectionConflict("message requires an explicit projection representation")
    data: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        data["tool_calls"] = [asdict(call) for call in message.tool_calls]
    if message.tool_call_id is not None:
        data["tool_call_id"] = message.tool_call_id
    if message.response_item is not None:
        data["response_item"] = asdict(message.response_item)
    return {
        "kind": "model_input",
        "event_ids": list(ids),
        "message": data,
    }


def _message(data: dict[str, Any]) -> ChatMessage:
    values = deepcopy(data)
    if "response_item" in values:
        raw = values["response_item"]
        values["response_item"] = ProviderContinuation(
            provider=raw["provider"],
            protocol=raw["protocol"],
            profile_id=raw["profile_id"],
            payload=tuple(raw["payload"]),
        )
    if "tool_calls" in values:
        values["tool_calls"] = tuple(
            ToolCall(id=call["id"], type=call["type"], function=ToolFunction(**call["function"]))
            for call in values["tool_calls"]
        )
    return ChatMessage(**values)
