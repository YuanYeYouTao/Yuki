"""One bounded, coalesced chat wakeup after a changed rollup becomes usable."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalConversationRollupEmergencyOverlayModel,
    CanonicalConversationRollupJobModel,
    CanonicalConversationRollupModel,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.event_repository import ConversationReadVersion

logger = logging.getLogger(__name__)

rollup_wakeup_history: ContextVar[bool] = ContextVar("rollup_wakeup_history", default=False)


@dataclass
class _State:
    active: int = 0
    handled: int = 0
    pending: int = 0
    changed: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class Ticket:
    conversation_id: str | None
    sequence: int
    state: _State


rollup_wakeup_watermark: ContextVar[int] = ContextVar("rollup_wakeup_watermark", default=0)


class RollupWakeups:
    def __init__(self, database: Database, *, timeout: float = 900) -> None:
        self.database = database
        self.timeout = timeout
        self.states: dict[str | None, _State] = {}
        self.sequence = 0
        self.closed = False
        self.on_consumed: Callable[[str, int], None] | None = None

    def enter(self, conversation_id: str | None) -> Ticket:
        self.sequence += 1
        state = self.states.setdefault(conversation_id, _State())
        state.active += 1
        return Ticket(conversation_id, self.sequence, state)

    def handled(self, ticket: Ticket) -> None:
        state = ticket.state
        state.handled = max(state.handled, ticket.sequence)
        if state.pending <= state.handled:
            state.pending = 0
        state.changed.set()

    def leave(self, ticket: Ticket, *, deferred: bool = False) -> None:
        state = ticket.state
        state.active -= 1
        if deferred and ticket.sequence > state.handled and len(self.states) <= 256:
            state.pending = max(state.pending, ticket.sequence)
        state.changed.set()
        self._prune(ticket)

    def _prune(self, ticket: Ticket) -> None:
        state = ticket.state
        if not state.active and not state.pending:
            if self.states.get(ticket.conversation_id) is state:
                self.states.pop(ticket.conversation_id, None)

    def discard(self, ticket: Ticket) -> None:
        if ticket.state.pending == ticket.sequence:
            ticket.state.pending = 0
        self._prune(ticket)

    def notify(self, conversation_id: str) -> None:
        state = self.states.get(conversation_id)
        if state is not None:
            state.changed.set()

    def close(self) -> None:
        self.closed = True
        for state in self.states.values():
            state.changed.set()

    async def _status(self, version: ConversationReadVersion) -> tuple[bool, bool]:
        async with self.database.sessions() as session:
            source = await session.get(CanonicalConversationModel, version.conversation_id)
            if source is None or (source.generation, source.starts_after_event_id) != (
                version.generation,
                version.starts_after_event_id,
            ):
                return False, False
            semantic = await session.get(CanonicalConversationRollupModel, source.id)
            overlay = await session.get(CanonicalConversationRollupEmergencyOverlayModel, source.id)
            job = await session.get(CanonicalConversationRollupJobModel, source.id)
            stamp = (semantic.revision if semantic else 0, overlay.revision if overlay else 0)
            return stamp != version.rollup_stamp, job is None and overlay is None

    async def wait(self, version: ConversationReadVersion, ticket: Ticket) -> bool:
        state = ticket.state
        try:
            async with asyncio.timeout(self.timeout):
                while not self.closed and state.pending == ticket.sequence:
                    state.changed.clear()
                    changed, ready = await self._status(version)
                    if not changed:
                        return False
                    if ready and not state.active and state.pending == ticket.sequence:
                        logger.info("rollup_chat_wakeup_ready")
                        return True
                    await state.changed.wait()
        except TimeoutError:
            logger.warning("rollup_chat_wakeup_expired")
        finally:
            self.discard(ticket)
        return False
