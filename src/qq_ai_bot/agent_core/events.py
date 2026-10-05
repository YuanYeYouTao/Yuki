"""Bounded, non-blocking event projection.

Yuki's durable receipts must never wait on diagnostics. Events go into a
bounded queue: when it is full the oldest
diagnostic is dropped and counted (never the loop's own state). A failing
listener is isolated and cannot interrupt the turn.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Iterator

from qq_ai_bot.agent_core.types import AgentEvent

logger = logging.getLogger(__name__)

Listener = Callable[[AgentEvent], None]


class EventStream:
    def __init__(self, capacity: int = 256, listeners: tuple[Listener, ...] = ()) -> None:
        if capacity <= 0:
            raise ValueError("event capacity must be positive")
        self._queue: deque[AgentEvent] = deque(maxlen=capacity)
        self._listeners = listeners
        self.dropped = 0
        self.listener_failures = 0
        self.ended = False

    def emit(self, event: AgentEvent) -> None:
        """Synchronous and non-blocking: safe in ``finally`` of a cancelled task."""
        if self._queue.maxlen is not None and len(self._queue) == self._queue.maxlen:
            self.dropped += 1
        self._queue.append(event)
        if event.type == "agent_end":
            self.ended = True
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:
                # A projection failure must not change execution or receipts.
                self.listener_failures += 1
                logger.exception("agent_event_listener_failed type=%s", event.type)

    def drain(self) -> tuple[AgentEvent, ...]:
        events = tuple(self._queue)
        self._queue.clear()
        return events

    def __iter__(self) -> Iterator[AgentEvent]:
        return iter(tuple(self._queue))
