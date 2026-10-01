"""Opt-in, content-free attribution of synthetic benchmark latency.

These temporary hooks observe the existing measured request once. They do not
warm caches, rerun queries, filter samples, or change wall-clock quality gates.
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any
from unittest.mock import patch

from sqlalchemy import event
from sqlalchemy.sql.elements import ClauseElement

_case: ContextVar[str] = ContextVar("quality_diagnostic_case", default="unknown")


@contextmanager
def diagnostic_case(case_id: str) -> Iterator[None]:
    token = _case.set(case_id)
    try:
        yield
    finally:
        _case.reset(token)


def benchmark_diagnostics[**P, T](
    function: Callable[P, Awaitable[T]],
) -> Callable[P, Awaitable[T]]:
    @wraps(function)
    async def run(*args: P.args, **kwargs: P.kwargs) -> T:
        if os.environ.get("YUKI_MEMORY_QUALITY_DIAGNOSTICS") != "1":
            return await function(*args, **kwargs)
        diagnostics = QualityLatencyDiagnostics()
        try:
            with diagnostics.install():
                return await function(*args, **kwargs)
        finally:
            diagnostics.emit()

    return run


class QualityLatencyDiagnostics:
    """Scope private-library probes to a single opt-in synthetic benchmark."""

    def __init__(self) -> None:
        self.active: dict[str, Any] | None = None
        self.samples: list[dict[str, Any]] = []
        self.dropped = 0
        self._gc_started: tuple[float, dict[str, Any] | None] | None = None
        self._engine_hooks: list[tuple[Any, Any, Any, Any, Any, Any, Any]] = []

    @staticmethod
    def _milliseconds(started: float) -> float:
        return (time.perf_counter() - started) * 1000

    @contextmanager
    def install(self) -> Iterator[None]:
        import aiosqlite

        from qq_ai_bot.memory.context import MemoryContextService
        from qq_ai_bot.persistence.database import Database

        execute: Callable[..., Awaitable[Any]] = aiosqlite.Connection._execute
        search = MemoryContextService.search
        database_init = Database.__init__
        compile_statement = ClauseElement._compile_w_cache

        async def measured_execute(connection: Any, fn: Any, *args: Any, **kwargs: Any) -> Any:
            sample = self.active
            if sample is None:
                return await execute(connection, fn, *args, **kwargs)
            queued = time.perf_counter()
            timing: dict[str, float] = {}
            operation = getattr(fn, "__name__", "unknown")
            if operation not in {
                "execute",
                "fetchall",
                "fetchone",
                "cursor",
                "close",
                "rollback",
                "commit",
                "create_function",
            }:
                operation = "other"

            def worker(*inner_args: Any, **inner_kwargs: Any) -> Any:
                timing["queue_ms"] = self._milliseconds(queued)
                started = time.perf_counter()
                cpu = time.thread_time()
                try:
                    return fn(*inner_args, **inner_kwargs)
                finally:
                    timing["worker_ms"] = self._milliseconds(started)
                    timing["worker_cpu_ms"] = (time.thread_time() - cpu) * 1000
                    timing["ended"] = time.perf_counter()

            try:
                return await execute(connection, worker, *args, **kwargs)
            finally:
                timing["wake_ms"] = self._milliseconds(timing.pop("ended", time.perf_counter()))
                sample["worker_operation_counts"][operation] += 1
                for name, value in timing.items():
                    sample[name] += value
                    sample[f"max_{name}"] = max(sample[f"max_{name}"], value)

        def measured_compile(statement: Any, *args: Any, **kwargs: Any) -> Any:
            sample = self.active
            if sample is None:
                return compile_statement(statement, *args, **kwargs)
            started = time.perf_counter()
            cpu = time.thread_time()
            try:
                result = compile_statement(statement, *args, **kwargs)
                sample["compile_cache_counts"][result[2].name] += 1
                return result
            finally:
                sample["compile_ms"] += self._milliseconds(started)
                sample["compile_cpu_ms"] += (time.thread_time() - cpu) * 1000

        def measured_init(database: Any, *args: Any, **kwargs: Any) -> None:
            database_init(database, *args, **kwargs)
            engine = database.engine.sync_engine

            def before(
                conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
            ) -> None:
                context._quality_diagnostic_started = time.perf_counter()

            def after(
                conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
            ) -> None:
                sample = self.active
                if sample is not None:
                    sample["sql_count"] += 1
                    sample["sql_roundtrip_ms"] += self._milliseconds(
                        context._quality_diagnostic_started
                    )
                    # Only a finite operation vocabulary; SQL/bind content is never retained.
                    operation = (
                        statement.lstrip().split(None, 1)[0].upper()
                        if statement.strip()
                        else "OTHER"
                    )
                    if operation not in {"SELECT", "INSERT", "UPDATE", "DELETE", "PRAGMA", "BEGIN"}:
                        operation = "OTHER"
                    sample["sql_operation_counts"][operation] += 1

            def checkout(*args: Any) -> None:
                if self.active is not None:
                    self.active["checkouts"] += 1

            event.listen(engine, "before_cursor_execute", before)
            event.listen(engine, "after_cursor_execute", after)
            event.listen(engine.pool, "checkout", checkout)
            ping = engine.dialect.do_ping
            pool_get = engine.pool._do_get

            def measured_ping(connection: Any) -> Any:
                started = time.perf_counter()
                try:
                    return ping(connection)
                finally:
                    if self.active is not None:
                        self.active["pre_ping_ms"] += self._milliseconds(started)

            def measured_pool_get() -> Any:
                started = time.perf_counter()
                try:
                    return pool_get()
                finally:
                    if self.active is not None:
                        self.active["pool_acquire_ms"] += self._milliseconds(started)

            engine.dialect.do_ping = measured_ping
            engine.pool._do_get = measured_pool_get
            self._engine_hooks.append(
                (engine, engine.pool, before, after, checkout, ping, pool_get)
            )

        async def measured_search(service: Any, *args: Any, **kwargs: Any) -> Any:
            sample = self._new_sample()
            self.active = sample
            started = time.perf_counter()
            cpu = time.process_time()
            main_cpu = time.thread_time()
            heartbeat = asyncio.create_task(self._loop_probe(sample, started))
            try:
                return await search(service, *args, **kwargs)
            finally:
                sample["wall_ms"] = self._milliseconds(started)
                sample["process_cpu_ms"] = (time.process_time() - cpu) * 1000
                sample["main_thread_cpu_ms"] = (time.thread_time() - main_cpu) * 1000
                self.active = None
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                if len(self.samples) < 512:
                    self.samples.append(sample)
                else:
                    self.dropped += 1

        gc.callbacks.append(self._gc_callback)
        try:
            with (
                patch.object(aiosqlite.Connection, "_execute", measured_execute),
                patch.object(ClauseElement, "_compile_w_cache", measured_compile),
                patch.object(Database, "__init__", measured_init),
                patch.object(MemoryContextService, "search", measured_search),
            ):
                yield
        finally:
            gc.callbacks.remove(self._gc_callback)
            for engine, pool, before, after, checkout, ping, pool_get in self._engine_hooks:
                event.remove(engine, "before_cursor_execute", before)
                event.remove(engine, "after_cursor_execute", after)
                event.remove(pool, "checkout", checkout)
                engine.dialect.do_ping = ping
                pool._do_get = pool_get
            self._engine_hooks.clear()

    def _new_sample(self) -> dict[str, Any]:
        sample: dict[str, Any] = {
            "case_id": _case.get(),
            "query_index": 1 + sum(item["case_id"] == _case.get() for item in self.samples),
            "worker_operation_counts": Counter(),
            "sql_operation_counts": Counter(),
            "compile_cache_counts": Counter(),
            "sql_count": 0,
            "checkouts": 0,
            "gc_count": 0,
        }
        for name in ("queue_ms", "worker_ms", "worker_cpu_ms", "wake_ms"):
            sample[name] = 0.0
            sample[f"max_{name}"] = 0.0
        for name in (
            "compile_ms",
            "compile_cpu_ms",
            "pool_acquire_ms",
            "pre_ping_ms",
            "sql_roundtrip_ms",
            "gc_ms",
            "max_gc_ms",
            "max_loop_lag_ms",
        ):
            sample[name] = 0.0
        return sample

    async def _loop_probe(self, sample: dict[str, Any], started: float) -> None:
        expected = started + 0.005
        while True:
            await asyncio.sleep(max(0.0, expected - time.perf_counter()))
            now = time.perf_counter()
            sample["max_loop_lag_ms"] = max(sample["max_loop_lag_ms"], (now - expected) * 1000)
            expected = now + 0.005

    def _gc_callback(self, phase: str, info: dict[str, Any]) -> None:
        if phase == "start":
            self._gc_started = (time.perf_counter(), self.active)
        elif self._gc_started is not None:
            started, sample = self._gc_started
            if sample is not None and sample is self.active:
                duration = self._milliseconds(started)
                sample["gc_count"] += 1
                sample["gc_ms"] += duration
                sample["max_gc_ms"] = max(sample["max_gc_ms"], duration)
            self._gc_started = None

    def emit(self) -> None:
        payload = {
            "schema_version": 1,
            "diagnostic_only": True,
            "phase_timings_overlap": True,
            "samples": self.samples,
            "dropped_samples": self.dropped,
        }
        print(
            "MEMORY_QUALITY_PHASES " + json.dumps(payload, separators=(",", ":")), file=sys.stderr
        )
