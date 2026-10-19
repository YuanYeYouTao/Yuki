"""Disposable post-commit wakeups; no facts, retries, or durable event log."""

import asyncio
from dataclasses import dataclass

from sqlalchemy import event
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class CommittedHint:
    kind: str
    reference: str | int


class CommitHints:
    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue[bool | CommittedHint]] = set()
        self.detailed: set[asyncio.Queue[bool | CommittedHint]] = set()

    def subscribe(self, *, details: bool = False) -> asyncio.Queue[bool | CommittedHint]:
        queue: asyncio.Queue[bool | CommittedHint] = asyncio.Queue(64)
        self.subscribers.add(queue)
        if details:
            self.detailed.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[bool | CommittedHint]) -> None:
        self.subscribers.discard(queue)
        self.detailed.discard(queue)

    def publish(self) -> None:
        for queue in tuple(self.subscribers):
            self._put(queue, True)

    def publish_steps(self, work_id: str) -> None:
        for queue in tuple(self.detailed):
            self._put(queue, CommittedHint("work_steps", work_id))

    def publish_artifacts(self, event_id: int) -> None:
        for queue in tuple(self.detailed):
            self._put(queue, CommittedHint("artifacts", event_id))

    def _put(self, queue: asyncio.Queue[bool | CommittedHint], value: bool | CommittedHint) -> None:
        if queue.full():
            self.unsubscribe(queue)
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(False)
        else:
            queue.put_nowait(value)


class HintSession(Session):
    """Session-local SQLAlchemy hook also covers explicit session.begin writers."""


@event.listens_for(HintSession, "after_commit")
def committed(session: HintSession) -> None:
    session.info["commit_hints"].publish()
    for work_id in session.info.pop("work_step_hints", ()):
        session.info["commit_hints"].publish_steps(work_id)
    for event_id in session.info.pop("artifact_event_hints", ()):
        session.info["commit_hints"].publish_artifacts(event_id)


@event.listens_for(HintSession, "after_rollback")
def rolled_back(session: HintSession) -> None:
    session.info.pop("work_step_hints", None)
    session.info.pop("artifact_event_hints", None)
