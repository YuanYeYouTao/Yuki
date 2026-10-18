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
        "F",
        "E",
        "item_count",
        "payload_bytes",
        "protocol_lock_wait_seconds",
        "protocol_lock_held_seconds",
        "conversation_lock_wait_seconds",
        "work_restore_source_read_seconds",
        "source_guard_read_seconds",
        "source_guard_final_read_seconds",
        "projection_sources_read_seconds",
        "inbound_admission_seconds",
        "participation_binding_seconds",
        "wait_matching_seconds",
        "attachment_preparation_seconds",
        "participation_feedback_seconds",
        "social_preparation_seconds",
        "social_claim_seconds",
        "social_transport_seconds",
        "social_settlement_seconds",
        "tool_admission_seconds",
        "tool_result_persistence_seconds",
    }
)


def collect_phase_metrics(**values: int | float) -> None:
    metrics = current_metrics.get()
    if metrics is not None:
        for name, value in values.items():
            if name in _METRICS:
                metrics[name] = metrics.get(name, 0.0) + value


@contextmanager
def turn_detail(name: str) -> Iterator[None]:
    """Keep early numeric preparation timings on the original turn correlation.

    Main adopts them into its existing trace; observe-only input still creates
    no diagnostic row. This never changes execution or persistence authority.
    """
    from yuki.runtime.observability import current_runtime_turn_correlation

    started = time.perf_counter()
    try:
        yield
    finally:
        key = f"{name}_seconds"
        elapsed = time.perf_counter() - started
        if current_metrics.get() is not None:
            collect_phase_metrics(**{key: elapsed})
        elif key in _METRICS and (correlation := current_runtime_turn_correlation()) is not None:
            phases = correlation.phase_seconds
            phases[key] = phases.get(key, 0.0) + elapsed


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
    metadata: dict[str, object] = field(default_factory=dict)

    def switch(self, name: str) -> None:
        now = time.perf_counter()
        self.exclusive[self.phase] = self.exclusive.get(self.phase, 0.0) + now - self.changed
        self.phase, self.changed = name, now

    def snapshot(self, outcome: str) -> dict[str, object]:
        self.switch(self.phase)
        return {
            **self.metadata,
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
