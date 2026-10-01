"""Ordered, turn-local request journal shared by all Agent entrypoints.

Provider continuations are opaque checkpoints. Only the latest checkpoint is
serialized; the entries after it are an ordered delta, never regrouped by role.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from qq_ai_bot.domain.messages import ChatMessage, FunctionCallOutput, ProviderContinuation
from qq_ai_bot.llm.base import LLMInvalidRequestError


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
    # None keeps the ordinary, non-Work protocol projection contract. Work
    # activations explicitly select only their approved initial public suffix;
    # the remaining tools, inputs and continuation belong to their own journal.
    public_initial_suffix: tuple[ChatMessage, ...] | None = None


_DISPATCH_REQUEST: ContextVar[TranscriptRequest | None] = ContextVar(
    "main_agent_dispatch_request", default=None
)


def dispatch_request() -> TranscriptRequest | None:
    """The exact ordered input currently undergoing post-admission validation."""
    return _DISPATCH_REQUEST.get()


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
        self._entries: list[ChatMessage | FunctionCallOutput | ProviderContinuation] = list(
            messages
        )

    @property
    def continuation(self) -> ProviderContinuation | None:
        return next(
            (item for item in reversed(self._entries) if isinstance(item, ProviderContinuation)),
            None,
        )

    def append(self, message: ChatMessage) -> None:
        self._entries.append(message)

    def portable_entries(self) -> tuple[ChatMessage | FunctionCallOutput, ...]:
        """Public input/output records; opaque provider state is never summary text."""
        return tuple(
            item for item in self._entries if isinstance(item, (ChatMessage, FunctionCallOutput))
        )

    def accept(self, continuation: ProviderContinuation) -> None:
        previous = self.continuation
        if previous is not None and (previous.provider, previous.protocol, previous.profile_id) != (
            continuation.provider,
            continuation.protocol,
            continuation.profile_id,
        ):
            raise LLMInvalidRequestError("continuation contract changed within a turn")
        self._entries.append(deepcopy(continuation))

    def append_result(self, call_id: str, result: str) -> None:
        if self.continuation is None:
            self.append(ChatMessage(role="tool", content=result, tool_call_id=call_id))
        else:
            self._entries.append(FunctionCallOutput(call_id=call_id, output=result))

    def request(self) -> TranscriptRequest:
        checkpoints = [
            i for i, item in enumerate(self._entries) if isinstance(item, ProviderContinuation)
        ]
        if not checkpoints:
            return TranscriptRequest(
                messages=tuple(item for item in self._entries if isinstance(item, ChatMessage)),
                continuation=None,
                items=(),
            )
        return TranscriptRequest(
            messages=tuple(
                item for item in self._entries[: checkpoints[0]] if isinstance(item, ChatMessage)
            ),
            continuation=self.continuation,
            items=tuple(
                item
                for item in self._entries[checkpoints[-1] + 1 :]
                if isinstance(item, (ChatMessage, FunctionCallOutput))
            ),
        )
