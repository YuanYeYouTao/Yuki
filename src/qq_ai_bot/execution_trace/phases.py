"""Monotonic numeric observations; nested detail is never added to exclusive phases."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

current_metrics: ContextVar[dict[str, float] | None] = ContextVar("trace_metrics", default=None)
current_model_phases: ContextVar[ModelPhases | None] = ContextVar("model_phases", default=None)
_METRICS = frozenset(
    {
        "N",
        "F",
        "E",
        "K",
        "candidate_count",
        "estimate_count",
        "item_count",
        "payload_bytes",
        "protocol_lock_wait_seconds",
        "protocol_lock_held_seconds",
        "conversation_lock_wait_seconds",
        "work_restore_source_read_seconds",
        "source_guard_read_seconds",
        "source_guard_final_read_seconds",
        "projection_snapshot_read_seconds",
        "projection_snapshot_publish_seconds",
        "projection_sources_read_seconds",
    }
)


def collect_phase_metrics(**values: int | float) -> None:
    metrics = current_metrics.get()
    if metrics is not None:
        for name, value in values.items():
            if name in _METRICS:
                metrics[name] = metrics.get(name, 0.0) + value


@asynccontextmanager
async def timed_lock(
    context: AbstractAsyncContextManager[object], kind: str
) -> AsyncIterator[float]:
    started = time.perf_counter()
    acquired = False
    try:
        async with context:
            acquired = True
            waited = time.perf_counter() - started
            held = time.perf_counter()
            if kind != "protocol" or waited >= 1:
                logging.getLogger(__name__).info(
                    "runtime_lock_timing lock=%s outcome=acquired wait_seconds=%.6f", kind, waited
                )
            try:
                yield waited
            finally:
                if kind == "protocol":
                    collect_phase_metrics(
                        protocol_lock_wait_seconds=waited,
                        protocol_lock_held_seconds=time.perf_counter() - held,
                    )
    except (Exception, asyncio.CancelledError):
        if not acquired:
            logging.getLogger(__name__).info(
                "runtime_lock_timing lock=%s outcome=not_acquired wait_seconds=%.6f",
                kind,
                time.perf_counter() - started,
            )
        raise


@dataclass
class ModelPhases:
    started: float = field(default_factory=time.perf_counter)
    changed: float = field(default_factory=time.perf_counter)
    phase: str = "preparation"
    exclusive: dict[str, float] = field(default_factory=dict)
    nested: dict[str, float] = field(default_factory=dict)
    attempts: int = 0

    def switch(self, name: str) -> None:
        now = time.perf_counter()
        self.exclusive[self.phase] = self.exclusive.get(self.phase, 0.0) + now - self.changed
        self.phase, self.changed = name, now

    def snapshot(self, outcome: str) -> dict[str, object]:
        self.switch(self.phase)
        return {
            "phase_version": 1,
            "outcome": outcome,
            "logical_call_seconds": self.changed - self.started,
            "exclusive_seconds": dict(self.exclusive),
            "nested_seconds": dict(self.nested),
            "physical_attempt_count": self.attempts,
        }


def switch_model_phase(name: str) -> None:
    phases = current_model_phases.get()
    if phases is not None:
        phases.switch(name)


@contextmanager
def model_detail(name: str) -> Iterator[None]:
    phases = current_model_phases.get()
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - started
        if phases is not None:
            phases.nested[name] = phases.nested.get(name, 0.0) + elapsed
        collect_phase_metrics(**{f"{name}_seconds": elapsed})
