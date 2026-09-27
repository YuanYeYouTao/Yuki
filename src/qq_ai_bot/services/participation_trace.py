"""Retain actual semantic observations in the original expiring diagnostic store."""

from __future__ import annotations

import time

import httpx
from yuki_participation.models import Observation, Snapshot
from yuki_participation.observer import SemanticObserver

from qq_ai_bot.execution_trace.recorder import TraceRecorder, record_trace, trace_span


def _source_event(snapshot: Snapshot) -> int | None:
    kind, _, value = snapshot.focus.ref.event_id.partition(":")
    if kind != "event" or not value.isascii() or not value.isdecimal() or len(value) > 19:
        return None
    identity = int(value)
    return identity if str(identity) == value and 1 <= identity <= 2**63 - 1 else None


class TracedSemanticObserver:
    """One existing observer call; diagnostics never select, retry or apply the result."""

    def __init__(self, observer: SemanticObserver, recorder: TraceRecorder | None) -> None:
        self._observer = observer
        self._recorder = recorder

    def prepare_snapshot(self, snapshot: Snapshot) -> Snapshot:
        prepare = getattr(self._observer, "prepare_snapshot", None)
        return prepare(snapshot) if prepare is not None else snapshot

    async def evaluate(self, snapshot: Snapshot) -> Observation:
        if self._recorder is None:
            return await self._observer.evaluate(snapshot)
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
                result = await self._observer.evaluate(snapshot)
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
