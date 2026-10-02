"""A prepared chat delta, committed only at an admitted model request boundary."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.frozen_fragments import EventFragment

Publication = Callable[[AsyncSession], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PreparedContextBoundary:
    publication: Publication
    stage: Callable[[], None]
    rollback: Callable[[], None]
    finalize: Callable[[], None]


@dataclass(frozen=True, slots=True)
class ContextBoundary:
    fragments: tuple[EventFragment, ...]
    commit: Callable[[], Awaitable[None]]
    prepare: Callable[[], Awaitable[PreparedContextBoundary]] | None = None

    @property
    def event_ids(self) -> frozenset[int]:
        return frozenset(key for ids, _ in self.fragments for key in ids)


ContextBoundaryReader = Callable[[frozenset[int]], Awaitable[ContextBoundary | None]]
