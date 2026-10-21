"""Bounded, content-free timings; observed holders are not all SQLite writers."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)
_WRITE = re.compile(
    r"^\s*(INSERT(?: OR \w+)? INTO|UPDATE|DELETE FROM|REPLACE INTO)"
    r'\s+["`\[]?([A-Za-z_][A-Za-z_0-9]*)',
    re.I,
)
_BUCKETS = (0.001, 0.01, 0.1, 1.0, 5.0)


@dataclass
class TimingSummary:
    """Fixed bins, not unbounded samples or high-cardinality metrics labels."""

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
    """Process-local aggregates; never writes diagnostic SQL or owns transactions."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.timings = {
            name: TimingSummary()
            for name in (
                "sql",
                "read_sql",
                "other_sql",
                "connection_acquisition",
                "acquire",
                "first_write",
                "commit",
                "rollback",
                "held_lower_bound",
                "held_release_unknown",
            )
        }

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                **{name: value.snapshot() for name, value in self.timings.items()},
                "driver_queue_seconds": None,
                "pool_wait_seconds": None,
                "sqlite_wait_seconds": None,
                "holder_scope": "installed_engine",
            }


def install_sqlite_diagnostics(engine: Engine) -> SQLiteDiagnostics:
    diagnostics = SQLiteDiagnostics()
    writers: dict[int, dict[str, Any]] = {}
    sequence = 0

    def physical(connection: Any) -> Any:
        return getattr(connection, "dbapi_connection", connection)

    def finish(token: int, outcome: str, completed_at: float | None = None) -> None:
        with diagnostics.lock:
            holder = writers.pop(token, None)
            if holder is None:
                return
            elapsed = (time.monotonic() if completed_at is None else completed_at) - holder["since"]
            diagnostics.timings["held_lower_bound"].record(elapsed)
        if elapsed >= 1:
            logger.warning(
                "sqlite_transaction_timing transaction=%d outcome=%s held_lower_bound_seconds=%.3f "
                "precision=after_first_write holder_scope=installed_engine",
                holder["transaction"],
                outcome,
                elapsed,
            )

    # Engine transaction events run before DBAPI completion. Failed calls keep
    # their holder until rollback/invalidation/physical exit succeeds.
    def transaction_call(connection: Any, call: Any, phase: str) -> None:
        token, started = id(physical(connection)), time.monotonic()
        outcome = "success"
        try:
            call(connection)
        except BaseException:
            outcome = "failed"
            raise
        finally:
            completed_at = time.monotonic()
            elapsed = completed_at - started
            with diagnostics.lock:
                diagnostics.timings[phase].record(elapsed)
                holder = writers.get(token)
                transaction = holder["transaction"] if holder else None
            if outcome == "success":
                # Clear at actual completion, before any slow diagnostic logging.
                finish(token, phase, completed_at)
            if elapsed >= 1 or outcome != "success":
                logger.warning(
                    "sqlite_transaction_call phase=%s transaction=%s outcome=%s seconds=%.3f "
                    "driver_queue_seconds=unknown",
                    phase,
                    transaction,
                    outcome,
                    elapsed,
                )

    def observation_end_unknown(token: int, phase: str, observed_until: float) -> None:
        # The pool has abandoned this physical connection. Do not retain stale
        # integer identities indefinitely or count an unconfirmed exit as release.
        with diagnostics.lock:
            holder = writers.pop(token, None)
            if holder is None:
                return
            elapsed = observed_until - holder["since"]
            diagnostics.timings["held_release_unknown"].record(elapsed)
        logger.warning(
            "sqlite_transaction_observation_end transaction=%d phase=%s "
            "held_lower_bound_seconds=%.3f release_confirmed=false "
            "precision=before_physical_exit holder_scope=installed_engine",
            holder["transaction"],
            phase,
            elapsed,
        )

    def physical_exit(connection: Any, call: Any, phase: str) -> None:
        dbapi = physical(connection)
        token, started = id(dbapi), time.monotonic()
        try:
            call(connection)
        except BaseException:
            observation_end_unknown(token, f"{phase}_failed", started)
            raise
        # aiosqlite's force terminate merely queues stop() on its worker thread.
        # Its graceful path awaits close(); the driver then has no connection.
        driver = getattr(dbapi, "driver_connection", None)
        if (
            phase == "terminate"
            and driver is not None
            and getattr(driver, "_connection", True) is not None
        ):
            observation_end_unknown(token, "terminate_unconfirmed", started)
        else:
            finish(token, f"physical_{phase}_complete")

    # Start at the application-to-pool call, before queue checkout/pre_ping.
    # This is inclusive acquisition, not an estimate of either subcomponent.
    original_connection = engine.raw_connection

    def acquire_connection() -> Any:
        started = time.monotonic()
        try:
            return original_connection()
        finally:
            elapsed = time.monotonic() - started
            with diagnostics.lock:
                diagnostics.timings["connection_acquisition"].record(elapsed)
            if elapsed >= 1:
                logger.warning(
                    "sqlite_connection_acquisition seconds=%.6f pool_wait_seconds=unknown "
                    "driver_queue_seconds=unknown",
                    elapsed,
                )

    observed_engine: Any = engine
    observed_engine.raw_connection = acquire_connection

    original_commit, original_rollback = engine.dialect.do_commit, engine.dialect.do_rollback
    dialect: Any = engine.dialect
    dialect.do_commit = lambda connection: transaction_call(connection, original_commit, "commit")
    dialect.do_rollback = lambda connection: transaction_call(
        connection, original_rollback, "rollback"
    )
    original_close, original_terminate = dialect.do_close, dialect.do_terminate
    dialect.do_close = lambda connection: physical_exit(connection, original_close, "close")
    dialect.do_terminate = lambda connection: physical_exit(
        connection, original_terminate, "terminate"
    )

    def before(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        match = _WRITE.match(statement)
        operation = " ".join(match.groups()) if match else None
        command = statement.strip().upper()
        if command in {"BEGIN IMMEDIATE", "BEGIN EXCLUSIVE"}:
            operation = command
        # Compiled CTE DML may not begin with INSERT/UPDATE/DELETE.
        if operation is None and any(
            getattr(context, flag, False) for flag in ("isinsert", "isupdate", "isdelete")
        ):
            operation = "COMPILED DML"
        started = time.monotonic()
        context._yuki_cursor = (
            started,
            "read_sql"
            if (command.startswith(("SELECT ", "WITH ")) and operation is None)
            else "other_sql",
        )
        context._yuki_write = (started, operation) if operation else None
        context._yuki_connection_token = (
            id(physical(conn.connection)) if operation or command.startswith("RELEASE ") else None
        )

    def statement_done(
        conn: Any, context: Any, *, succeeded: bool, error_code: int | None = None
    ) -> None:
        nonlocal sequence
        write = getattr(context, "_yuki_write", None)
        if write is None:
            cursor = getattr(context, "_yuki_cursor", None)
            context._yuki_cursor = None
            if cursor is not None:
                started, phase = cursor
                elapsed = time.monotonic() - started
                with diagnostics.lock:
                    diagnostics.timings[phase].record(elapsed)
                if elapsed >= 1 or not succeeded:
                    logger.warning(
                        "sqlite_cursor_timing phase=%s seconds=%.6f outcome=%s "
                        "sqlite_errorcode=%s driver_queue_seconds=unknown "
                        "sqlite_wait_seconds=unknown",
                        phase,
                        elapsed,
                        "success" if succeeded else "failed",
                        error_code,
                    )
            return
        context._yuki_write = None
        context._yuki_cursor = None
        started, operation = write
        now, token = time.monotonic(), context._yuki_connection_token
        elapsed = now - started
        with diagnostics.lock:
            first = token not in writers
            acquire = operation.startswith("BEGIN ")
            diagnostics.timings["acquire" if acquire else "sql"].record(elapsed)
            if first and not acquire:
                # This overlaps SQL, since deferred DML acquisition is inseparable.
                diagnostics.timings["first_write"].record(elapsed)
            if succeeded:
                if first:
                    try:
                        task = asyncio.current_task()
                    except RuntimeError:
                        task = None
                    sequence += 1
                    writers[token] = dict(
                        since=now,
                        transaction=sequence,
                        coroutine=getattr(task.get_coro(), "__qualname__", "unknown")
                        if task
                        else "sync",
                    )
                writers[token]["operation"] = operation
            holder = writers.get(token)
            transaction = holder["transaction"] if holder else None
        if elapsed >= 1 or not succeeded:
            logger.warning(
                "sqlite_slow_write operation=%s seconds=%.3f transaction=%s outcome=%s "
                "phase=%s sqlite_errorcode=%s driver_queue_seconds=unknown "
                "sqlite_wait_seconds=unknown",
                operation,
                elapsed,
                transaction,
                "success" if succeeded else "failed",
                "acquire_inclusive" if acquire else "first_write_inclusive" if first else "sql",
                error_code,
            )

    def after(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        statement_done(conn, context, succeeded=True)
        if (
            statement.lstrip().upper().startswith("RELEASE ")
            or getattr(context, "_yuki_connection_token", None) is not None
        ):
            dbapi = physical(conn.connection)
            driver = getattr(dbapi, "driver_connection", dbapi)
            # Outermost SAVEPOINT may itself be the physical transaction.
            # Inner RELEASE/ROLLBACK TO must not clear the outer holder.
            if getattr(driver, "in_transaction", None) is False:
                finish(id(dbapi), "physical_transaction_complete")

    def failure(context: Any) -> None:
        code = getattr(context.original_exception, "sqlite_errorcode", None)
        if context.connection is not None and context.execution_context is not None:
            statement_done(
                context.connection, context.execution_context, succeeded=False, error_code=code
            )
        if code is None or code & 255 not in {5, 6}:
            return
        now = time.monotonic()
        with diagnostics.lock:
            holders = [
                dict(
                    transaction=value["transaction"],
                    operation=value["operation"],
                    coroutine=value["coroutine"],
                    held_seconds=round(now - value["since"], 3),
                    precision="lower_bound_after_first_write",
                )
                for value in writers.values()
            ]
        logger.warning(
            "sqlite_write_contended holders=%s", json.dumps(holders, separators=(",", ":"))
        )

    def release(dbapi_connection: Any, record: Any, *args: Any) -> None:
        finish(id(dbapi_connection), "pool_exit")

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    event.listen(engine, "handle_error", failure)
    event.listen(engine.pool, "checkin", release)
    # Pool invalidate/close events precede DBAPI physical exit. They do not
    # certify release; the dialect completion wrappers above observe that boundary.
    return diagnostics
