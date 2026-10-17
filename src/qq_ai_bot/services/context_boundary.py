"""A prepared chat delta, committed only at an admitted model request boundary."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.frozen_fragments import EventFragment
from qq_ai_bot.conversation.ordinary_admission import PreparedOrdinaryAdmission
from qq_ai_bot.domain.messages import ChatMessage

Publication = Callable[[AsyncSession], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class OrdinaryContextInput:
    prepared: PreparedOrdinaryAdmission
    message: ChatMessage | None = None
    admission_allowed: Callable[[AsyncSession], Awaitable[bool]] | None = None
    on_committed: Callable[[], Awaitable[None]] | None = None


OrdinaryContextReader = Callable[[tuple[int, ...]], Awaitable[tuple[OrdinaryContextInput, ...]]]


@dataclass(frozen=True, slots=True)
class PreparedContextBoundary:
    publication: Publication
    stage: Callable[[], None]
    rollback: Callable[[], None]
    finalize: Callable[[], None]


@dataclass(frozen=True, slots=True)
class ContextBoundary:
    fragments: tuple[EventFragment, ...]
    commit: Callable[[bool, tuple[OrdinaryContextInput, ...]], Awaitable[None]]
    prepare: (
        Callable[[bool, tuple[OrdinaryContextInput, ...]], Awaitable[PreparedContextBoundary]]
        | None
    ) = None
    ordinary_inputs: tuple[OrdinaryContextInput, ...] = ()

    @property
    def current_inputs(self) -> tuple[ChatMessage, ...]:
        return tuple(
            candidate.message for candidate in self.ordinary_inputs if candidate.message is not None
        )

    @property
    def event_ids(self) -> frozenset[int]:
        return frozenset(key for ids, _ in self.fragments for key in ids)


ContextBoundaryReader = Callable[[frozenset[int]], Awaitable[ContextBoundary | None]]
