"""Content-free preparation timings within the existing chat trace scope."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from time import perf_counter
from typing import Literal

from qq_ai_bot.execution_trace.recorder import record_trace

Stage = Literal[
    "work_preparation",
    "runtime_snapshot",
    "memory_and_repair",
    "build_messages",
    "work_activation",
    "context_validation",
    "agent_setup",
]
Detail = Literal["context_assembly", "main_turn_composition"]

_STAGES: tuple[Stage, ...] = (
    "work_preparation",
    "runtime_snapshot",
    "memory_and_repair",
    "build_messages",
    "work_activation",
    "context_validation",
    "agent_setup",
)
_DETAILS: tuple[Detail, ...] = ("context_assembly", "main_turn_composition")
logger = logging.getLogger(__name__)


class ChatPreparationTimings:
    def __init__(self) -> None:
        self.started = self.boundary = perf_counter()
        self.stage: Stage = "work_preparation"
        self.stages = dict.fromkeys(_STAGES, 0.0)
        self.details = dict.fromkeys(_DETAILS, 0.0)
        self.emitted = False

    def advance(self, stage: Stage) -> None:
        now = perf_counter()
        self.stages[self.stage] += max(0.0, now - self.boundary)
        self.boundary, self.stage = now, stage

    async def emit(self, status: Literal["ready", "error"]) -> None:
        if self.emitted:
            return
        self.emitted = True
        now = perf_counter()
        self.stages[self.stage] += max(0.0, now - self.boundary)
        payload = {
            "status": status,
            "total_seconds": max(0.0, now - self.started),
            "stage_seconds": dict(self.stages),
            # These are subdivisions of build_messages, not additional time.
            "build_detail_seconds": dict(self.details),
        }
        try:
            await record_trace("chat_preparation", payload)
        except Exception as exc:
            logger.warning("chat_preparation_trace_failed category=%s", type(exc).__name__)


_current_preparation: ContextVar[ChatPreparationTimings | None] = ContextVar(
    "chat_preparation_timings", default=None
)


@asynccontextmanager
async def collect_chat_preparation() -> AsyncIterator[ChatPreparationTimings]:
    timings = ChatPreparationTimings()
    token = _current_preparation.set(timings)
    try:
        try:
            yield timings
        except asyncio.CancelledError:
            # Cancellation keeps its existing propagation and cleanup. Recording
            # after cancellation is not guaranteed by the trace contract.
            raise
        except Exception:
            await timings.emit("error")
            raise
    finally:
        _current_preparation.reset(token)


@contextmanager
def preparation_detail(detail: Detail) -> Iterator[None]:
    timings = _current_preparation.get()
    if timings is None or timings.emitted:
        yield
        return
    started = perf_counter()
    try:
        yield
    finally:
        timings.details[detail] += max(0.0, perf_counter() - started)


async def emit_chat_preparation() -> None:
    timings = _current_preparation.get()
    if timings is not None:
        await timings.emit("ready")
