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


def install_sqlite_diagnostics(engine: Engine) -> None:
    # One entry per physical writer connection, cleared after DBAPI completion.
    writers: dict[int, dict[str, Any]] = {}

    def physical(connection: Any) -> Any:
        # Dialect transaction hooks receive either the pool proxy or the DBAPI
        # connection itself (pool reset). Neither requires Connection.info.
        return getattr(connection, "dbapi_connection", connection)

    original_commit = engine.dialect.do_commit
    original_rollback = engine.dialect.do_rollback

    def commit(connection: Any) -> None:
        token = id(physical(connection))
        original_commit(connection)
        writers.pop(token, None)

    def rollback(connection: Any) -> None:
        token = id(physical(connection))
        original_rollback(connection)
        writers.pop(token, None)

    # Engine commit/rollback events run BEFORE the DBAPI call and would hide
    # precisely the slow commit tail. Failed calls retain their holder until
    # successful rollback, invalidation or physical/pool exit.
    dialect: Any = engine.dialect
    dialect.do_commit = commit
    dialect.do_rollback = rollback

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
        token = id(conn.connection.dbapi_connection)
        if token not in writers:
            try:
                task = asyncio.current_task()
            except RuntimeError:
                task = None
            value = {
                # The statement may itself have waited for a different writer.
                # Do not report that wait as time this connection held the lock.
                "since": time.monotonic(),
                "operation": operation,
                "coroutine": getattr(task.get_coro(), "__qualname__", "unknown")
                if task
                else "sync",
            }
            writers[token] = value
        writers[token]["operation"] = operation
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

    def release(dbapi_connection: Any, record: Any, *args: Any) -> None:
        writers.pop(id(dbapi_connection), None)

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    event.listen(engine, "handle_error", failure)
    event.listen(engine.pool, "checkin", release)
    event.listen(engine.pool, "invalidate", release)
    event.listen(engine.pool, "close", release)
