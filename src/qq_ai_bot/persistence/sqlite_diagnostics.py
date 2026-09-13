"""Bounded, content-free attribution for SQLite writer contention."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)
_WRITE = re.compile(
    r"^\s*(INSERT(?: OR \w+)? INTO|UPDATE|DELETE FROM|REPLACE INTO)"
    r'\s+["`\[]?([A-Za-z_][A-Za-z_0-9]*)',
    re.I,
)
_KEY = "yuki_sqlite_writer"


def install_sqlite_diagnostics(engine: Engine) -> None:
    # One entry per checked-out writer connection, cleared at transaction/pool exit.
    writers: dict[int, dict[str, Any]] = {}

    def clear(conn: Any) -> None:
        # Connection.info may reconnect an invalidated connection during rollback,
        # raising PendingRollbackError and preventing the original rollback.
        writers.pop(id(conn), None)
        if conn.closed or conn.invalidated:
            return
        conn.info.pop(_KEY, None)

    def before(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        match = _WRITE.match(statement)
        operation = " ".join(match.groups()) if match else None
        if statement.strip().upper() == "BEGIN IMMEDIATE":
            operation = "BEGIN IMMEDIATE"
        context._yuki_write = (time.monotonic(), operation) if operation else None

    def after(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        write = getattr(context, "_yuki_write", None)
        if write is None:
            return
        started, operation = write
        if id(conn) not in writers:
            try:
                task = asyncio.current_task()
            except RuntimeError:
                task = None
            value = {
                "token": id(conn),
                # The statement may itself have waited for a different writer.
                # Do not report that wait as time this connection held the lock.
                "since": time.monotonic(),
                "operation": operation,
                "coroutine": getattr(task.get_coro(), "__qualname__", "unknown")
                if task
                else "sync",
            }
            conn.info[_KEY] = value
            writers[id(conn)] = value
        conn.info[_KEY]["operation"] = operation
        duration = time.monotonic() - started
        if duration >= 1:
            logger.warning("sqlite_slow_write operation=%s seconds=%.3f", operation, duration)

    def failure(context: Any) -> None:
        if "locked" not in str(context.original_exception).lower():
            return
        now = time.monotonic()
        holders = [
            {
                "operation": value["operation"],
                "coroutine": value["coroutine"],
                "held_seconds": round(now - value["since"], 3),
            }
            for value in writers.values()
        ]
        logger.warning(
            "sqlite_write_contended holders=%s", json.dumps(holders, separators=(",", ":"))
        )

    def checkin(dbapi_connection: Any, record: Any) -> None:
        value = record.info.pop(_KEY, None)
        if value is not None:
            writers.pop(value["token"], None)

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    event.listen(engine, "handle_error", failure)
    event.listen(engine, "commit", clear)
    event.listen(engine, "rollback", clear)
    event.listen(engine.pool, "checkin", checkin)
