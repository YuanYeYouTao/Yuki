"""Keep submitted input fragments intact while selecting new ledger events."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from qq_ai_bot.conversation.projections import ProjectionConflict
from qq_ai_bot.domain.messages import ChatMessage

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
            if not {"kind", "event_ids", "message"} <= set(item) or not set(item) <= {
                "kind",
                "event_ids",
                "message",
            }:
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
                or not set(message) <= {"role", "content"}
                or message["role"] not in {"user", "assistant", "tool", "system"}
                or (message["content"] is not None and not isinstance(message["content"], str))
            ):
                raise ProjectionConflict("unsupported frozen message representation")
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
        result = list(self.items)
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

    def append_current(self, event_id: int, message: ChatMessage) -> FrozenFragments:
        """Freeze the adopted event rendering without activation-only material."""
        if event_id in self.event_ids:
            raise ProjectionConflict("trigger already exists in frozen input")
        return self.load(
            [
                *deepcopy(self.items),
                _input((event_id,), message),
            ]
        )


def _input(ids: tuple[int, ...], message: ChatMessage) -> dict[str, Any]:
    # Public event text/reference is separate from the current request pixels and private protocol.
    return {
        "kind": "model_input",
        "event_ids": list(ids),
        "message": {"role": message.role, "content": message.content},
    }


def _message(data: dict[str, Any]) -> ChatMessage:
    return ChatMessage(**deepcopy(data))
