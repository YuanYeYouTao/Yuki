"""Retain actual semantic observations in the original expiring diagnostic store."""

from __future__ import annotations

import time

import httpx
from yuki_participation.controller import ActorIdentity, original_actor
from yuki_participation.models import Observation, Snapshot
from yuki_participation.observer import SemanticObserver

from yuki.execution_trace.recorder import TraceRecorder, record_trace, trace_span
from yuki.identity.person_identity import current_person_ids
from yuki.persistence.database import Database


def _source_event(snapshot: Snapshot) -> int | None:
    kind, _, value = snapshot.focus.ref.event_id.partition(":")
    if kind != "event" or not value.isascii() or not value.isdecimal() or len(value) > 19:
        return None
    identity = int(value)
    return identity if str(identity) == value and 1 <= identity <= 2**63 - 1 else None


class TracedSemanticObserver:
    """One existing observer call; diagnostics never select, retry or apply the result."""

    def __init__(
        self,
        observer: SemanticObserver,
        recorder: TraceRecorder | None,
        *,
        database: Database | None = None,
    ) -> None:
        self._observer = observer
        self._recorder = recorder
        self._database = database

    async def evaluate(
        self, snapshot: Snapshot, *, actor_identity: ActorIdentity = original_actor
    ) -> Observation:
        if self._database is not None:
            ids = {
                value
                for event in (*snapshot.context, snapshot.focus)
                for value in (event.author, event.target)
            }
            ids.update(option.target for option in snapshot.focus.unit_options)
            if snapshot.participation is not None:
                ids.update(unit.target for unit in snapshot.participation.units)
            async with self._database.sessions() as reader:
                roots = await current_person_ids(reader, ids)

            def current_actor(actor: str) -> str:
                return roots.get(actor, actor)

            actor_identity = current_actor
        if self._recorder is None:
            return await self._observer.evaluate(snapshot, actor_identity=actor_identity)
        started = time.monotonic()
        async with trace_span(
            "semantic_observation",
            {"snapshot": snapshot.model_dump(mode="json")},
            recorder=self._recorder,
            conversation_id=snapshot.scope.conversation_id,
            source_event_id=_source_event(snapshot),
            origin="semantic_observation",
        ) as span:
            try:
                result = await self._observer.evaluate(snapshot, actor_identity=actor_identity)
            except (httpx.HTTPError, ValueError) as exc:
                status = (
                    exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                )
                await record_trace(
                    "semantic_failure",
                    {
                        "error_category": type(exc).__name__,
                        "http_status": status,
                        "elapsed_seconds": time.monotonic() - started,
                    },
                )
                raise
            await record_trace(
                "semantic_result",
                {
                    "observation": result.model_dump(mode="json"),
                    "elapsed_seconds": time.monotonic() - started,
                },
            )
            span.result = {"observation_id": result.observation_id}
            return result
