"""Content-free SQL and writer phase timings, without connection ownership probes."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)
_BUCKETS = (0.001, 0.01, 0.1, 1.0, 5.0)


@dataclass
class TimingSummary:
    count: int = 0
    seconds: float = 0
    maximum: float = 0
    buckets: list[int] = field(default_factory=lambda: [0] * (len(_BUCKETS) + 1))

    def record(self, seconds: float) -> None:
        self.count += 1
        self.seconds += seconds
        self.maximum = max(self.maximum, seconds)
        index = next((i for i, bound in enumerate(_BUCKETS) if seconds <= bound), len(_BUCKETS))
        self.buckets[index] += 1

    def snapshot(self) -> dict[str, Any]:
        return dict(
            count=self.count,
            seconds=self.seconds,
            maximum=self.maximum,
            bounds_seconds=list(_BUCKETS),
            buckets=list(self.buckets),
        )


class SQLiteDiagnostics:
    """In-memory aggregates; the Database owner measures transaction boundaries."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.timings = {
            name: TimingSummary()
            for name in (
                "sql",
                "read_sql",
                "other_sql",
                "acquire",
                "commit",
                "rollback",
                "writer_admission_wait",
                "writer_connection_acquisition",
                "writer_transaction",
                "writer_cleanup",
            )
        }

    def record(self, phase: str, seconds: float) -> None:
        with self._lock:
            self.timings[phase].record(seconds)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {name: summary.snapshot() for name, summary in self.timings.items()}


def install_sqlite_diagnostics(engine: Engine) -> SQLiteDiagnostics:
    diagnostics = SQLiteDiagnostics()

    def before(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        command = statement.lstrip().upper()
        write = command.startswith(("INSERT ", "UPDATE ", "DELETE ", "REPLACE ")) or any(
            getattr(context, flag, False) for flag in ("isinsert", "isupdate", "isdelete")
        )
        phase = (
            "acquire"
            if command.startswith(("BEGIN IMMEDIATE", "BEGIN EXCLUSIVE"))
            else "sql"
            if write
            else "read_sql"
            if command.startswith(("SELECT ", "WITH "))
            else "other_sql"
        )
        context._yuki_sql_timing = (phase, time.monotonic())

    def finish(context: Any) -> str:
        phase, started = context._yuki_sql_timing
        diagnostics.record(phase, time.monotonic() - started)
        return str(phase)

    def after(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        finish(context)

    def failure(context: Any) -> None:
        if context.execution_context is not None:
            phase = finish(context.execution_context)
            code = getattr(context.original_exception, "sqlite_errorcode", None)
            logger.warning("sqlite_statement_failed phase=%s sqlite_errorcode=%s", phase, code)

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    event.listen(engine, "handle_error", failure)
    return diagnostics
