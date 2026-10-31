"""Ordered, turn-local request journal shared by all Agent entrypoints.

Provider continuations are opaque checkpoints. Only the latest checkpoint is
serialized; the entries after it are an ordered delta, never regrouped by role.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import uuid4

from yuki.domain.messages import (
    ChatMessage,
    FunctionCallOutput,
    ProviderContinuation,
    ToolCall,
    ToolFunction,
)
from yuki.llm.base import LLMInvalidRequestError


class DispatchOrigin(StrEnum):
    """Authority of the transcript selected for this model dispatch."""

    COMPOSED_INITIAL = "composed_initial"
    WORK_RECOVERY = "work_recovery"


@dataclass(frozen=True, slots=True)
class TranscriptRequest:
    messages: tuple[ChatMessage, ...]
    continuation: ProviderContinuation | None
    items: tuple[ChatMessage | FunctionCallOutput, ...]
    origin: DispatchOrigin = DispatchOrigin.COMPOSED_INITIAL
    layout_public_initial: tuple[ChatMessage, ...] | None = None
    layout_host_initial: tuple[ChatMessage, ...] = ()
    layout_current_inputs: tuple[ChatMessage, ...] = ()


_DISPATCH_REQUEST: ContextVar[TranscriptRequest | None] = ContextVar(
    "main_agent_dispatch_request", default=None
)


def dispatch_request() -> TranscriptRequest | None:
    """The exact ordered input currently undergoing post-admission validation."""
    return _DISPATCH_REQUEST.get()


def recorded_response(value: dict[str, Any]) -> ChatMessage:
    """Read portable call facts from the existing model observation journal."""
    return ChatMessage(
        "assistant",
        value.get("content") or None,
        tool_calls=tuple(
            ToolCall(
                id=call["id"],
                function=ToolFunction(**call["function"]),
                type=call.get("type", "function"),
            )
            for call in value.get("tool_calls", [])
        ),
    )


def tool_result_evidence(
    assistant: ChatMessage | None,
    results: dict[str, str],
    *,
    call_key: Callable[[str], str | None] | None = None,
) -> ChatMessage:
    """Original results on a fresh chain, without transplanted native protocol."""
    return ChatMessage(
        "user",
        json.dumps(
            {
                "kind": "work_unobserved_tool_round",
                "content": assistant.content if assistant else None,
                "calls": [
                    {
                        "call_id": call.id,
                        **(
                            {"effect_key": key}
                            if call_key is not None and (key := call_key(call.id)) is not None
                            else {}
                        ),
                        "tool": call.function.name,
                        "arguments": call.function.arguments,
                        "result": results[call.id],
                    }
                    for call in assistant.tool_calls
                ]
                if assistant
                else [
                    {"call_id": identity, "result": result} for identity, result in results.items()
                ],
                "instruction": (
                    "These are original recorded tool results awaiting observation. "
                    "Continue from these results; do not replay the original calls."
                ),
            },
            ensure_ascii=False,
        ),
    )


@contextmanager
def validating_request(request: TranscriptRequest) -> Iterator[None]:
    token = _DISPATCH_REQUEST.set(request)
    try:
        yield
    finally:
        _DISPATCH_REQUEST.reset(token)


class TurnTranscript:
    def __init__(self, messages: tuple[ChatMessage, ...]) -> None:
        self.chain_id = uuid4().hex
        self._layout_public_initial: tuple[ChatMessage, ...] | None = None
        self._layout_host_initial: tuple[ChatMessage, ...] = ()
        self._layout_current_inputs: tuple[ChatMessage, ...] = ()
        self._entries: list[ChatMessage | FunctionCallOutput | ProviderContinuation] = list(
            messages
        )

    def finalize_initial(
        self,
        public: tuple[ChatMessage, ...],
        host: tuple[ChatMessage, ...],
        current_inputs: tuple[ChatMessage, ...],
    ) -> None:
        if self.continuation is not None or self._layout_public_initial is not None:
            raise ValueError("initial_layout_already_frozen")
        self._layout_public_initial = public
        self._layout_host_initial = host
        self._layout_current_inputs = current_inputs
        self._entries[:] = [*public[:-1], *host, public[-1], *current_inputs]

    @property
    def continuation(self) -> ProviderContinuation | None:
        return next(
            (item for item in reversed(self._entries) if isinstance(item, ProviderContinuation)),
            None,
        )

    def append(self, message: ChatMessage) -> None:
        self._entries.append(message)

    def entries(self) -> tuple[ChatMessage | FunctionCallOutput | ProviderContinuation, ...]:
        """The original ordered entries, including their owned checkpoints."""
        return tuple(self._entries)

    def prefix(self, boundary: int) -> TurnTranscript:
        """Select an existing input prefix without editing provider state."""
        result = TurnTranscript(())
        result.chain_id = self.chain_id
        result._entries = deepcopy(self._entries[:boundary])
        result._layout_public_initial = self._layout_public_initial
        result._layout_host_initial = self._layout_host_initial
        result._layout_current_inputs = self._layout_current_inputs
        return result

    def portable_entries(self) -> tuple[ChatMessage | FunctionCallOutput, ...]:
        """Public input/output records; opaque provider state is never summary text."""
        return tuple(
            item for item in self._entries if isinstance(item, (ChatMessage, FunctionCallOutput))
        )

    def accept(self, continuation: ProviderContinuation) -> None:
        previous = self.continuation
        if previous is not None and (previous.protocol, previous.profile_id) != (
            continuation.protocol,
            continuation.profile_id,
        ):
            raise LLMInvalidRequestError("continuation contract changed within a turn")
        self._entries.append(deepcopy(continuation))

    def append_result(self, call_id: str, result: str) -> None:
        if self.continuation is None:
            self.append(ChatMessage(role="tool", content=str(result), tool_call_id=call_id))
        else:
            self._entries.append(FunctionCallOutput(call_id=call_id, output=str(result)))

    def append_tool_media(self, results: Sequence[tuple[str, str]]) -> None:
        """After *all* paired receipts, append Host observations in call order.

        The user role transports pixels on existing protocols; it does not mint
        an inbound event, principal or new authorization.
        """
        from yuki.capabilities.media import result_images

        seen = {
            image
            for entry in self.portable_entries()
            if isinstance(entry, ChatMessage)
            for image in entry.images
        }
        for call_id, result in results:
            images = tuple(image for image in result_images(result) if image not in seen)
            if not images:
                continue
            seen.update(images)
            self.append(
                ChatMessage(
                    role="user",
                    content=(
                        f"[Host 工具媒体观察：call_id={call_id}；"
                        "像素来自 Agent 已选且获准读取的资料，属于工具结果。]"
                    ),
                    images=images,
                )
            )

    def request(self) -> TranscriptRequest:
        checkpoints = [
            i for i, item in enumerate(self._entries) if isinstance(item, ProviderContinuation)
        ]
        if not checkpoints:
            return TranscriptRequest(
                messages=tuple(item for item in self._entries if isinstance(item, ChatMessage)),
                continuation=None,
                items=(),
                layout_public_initial=self._layout_public_initial,
                layout_host_initial=self._layout_host_initial,
                layout_current_inputs=self._layout_current_inputs,
            )
        return TranscriptRequest(
            messages=tuple(
                item for item in self._entries[: checkpoints[0]] if isinstance(item, ChatMessage)
            ),
            layout_public_initial=self._layout_public_initial,
            layout_host_initial=self._layout_host_initial,
            layout_current_inputs=self._layout_current_inputs,
            continuation=self.continuation,
            items=tuple(
                item
                for item in self._entries[checkpoints[-1] + 1 :]
                if isinstance(item, (ChatMessage, FunctionCallOutput))
            ),
        )
