"""Database engine lifecycle and health checks."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from yuki.persistence.commit_hints import CommitHints
from yuki.persistence.metadata import Base
from yuki.persistence.sqlite_connection import SQLiteConnection

if TYPE_CHECKING:
    from yuki.admin.models import WorkStorageRuntimeConfig
    from yuki.persistence.sqlite_diagnostics import SQLiteDiagnostics
    from yuki.sandbox.client import SandboxClient

_SQLITE_BUSY_TIMEOUT_MS = 5_000
logger = logging.getLogger(__name__)


def write_failure_phase(error: BaseException) -> str | None:
    return next(
        (
            note.removeprefix("database_write_phase:")
            for note in getattr(error, "__notes__", ())
            if note.startswith("database_write_phase:")
        ),
        None,
    )


def write_failure_committed(error: BaseException) -> bool | None:
    """Retain the actual committed, rolled-back, or unknown transaction outcome."""
    notes = getattr(error, "__notes__", ())
    if "database_commit:confirmed" in notes:
        return True
    if (
        write_failure_phase(error) in {"begin", "execute", "flush"}
        and "database_rollback:confirmed" in notes
    ):
        return False
    return None


def write_failure_uncertain(error: BaseException) -> bool:
    return write_failure_committed(error) is None


def write_failure_retryable(error: BaseException) -> bool:
    original = getattr(error, "orig", error)
    code = getattr(original, "sqlite_errorcode", None)
    return (
        code is not None
        and code & 255 == sqlite3.SQLITE_BUSY
        and not write_failure_uncertain(error)
        and "database_commit:confirmed" not in getattr(error, "__notes__", ())
    )


async def _complete[T](operation: Awaitable[T]) -> tuple[T, asyncio.CancelledError | None]:
    """Finish this one driver/session operation, remembering caller cancellation."""
    pending = asyncio.ensure_future(operation)
    cancellation = None
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
        except BaseException as error:
            if cancellation is not None:
                raise cancellation from error
            raise
    return pending.result(), cancellation


class Database:
    """Own the async SQLAlchemy engine and explicit session factory."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.subagents_enabled = False
        self.sandbox_client: SandboxClient | None = None
        self.work_result_store: Any = None
        self.protocol_storage_policy: Callable[[], Awaitable[WorkStorageRuntimeConfig]] | None = (
            None
        )
        self._protocol_store_path: Path | None = None
        self._protocol_storage_lock: asyncio.Lock | None = None
        self._protocol_gc_iterator: Iterator[Path] | None = None
        self._protocol_gc_lock: asyncio.Lock | None = None
        self._protocol_gc_scans: dict[bool, Any] = {}
        self._writer = asyncio.Lock()
        self._writer_failure: BaseException | None = None
        self._closed = False
        self._ensure_sqlite_parent(url)
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_pre_ping=True,
            **({"async_creator": self._connect_sqlite} if url.startswith("sqlite+") else {}),
        )
        self.sqlite_diagnostics: SQLiteDiagnostics | None = None
        if url.startswith("sqlite+aiosqlite:///"):
            event.listen(self.engine.sync_engine, "connect", self._configure_sqlite_connection)
            from yuki.persistence.sqlite_diagnostics import install_sqlite_diagnostics

            self.sqlite_diagnostics = install_sqlite_diagnostics(self.engine.sync_engine)
        self.commit_hints = CommitHints()
        self._sessions = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

    async def _connect_sqlite(self) -> SQLiteConnection:
        args, options = self.engine.sync_engine.dialect.create_connect_args(self.engine.url)
        driver = SQLiteConnection(lambda: sqlite3.connect(args[0], **options))
        try:
            await driver
            return driver
        except BaseException as failure:
            try:
                stopped = driver.stop()
                if stopped is not None:
                    await _complete(stopped)
            except BaseException as cleanup:
                failure.add_note(f"invalidation_failed:{type(cleanup).__name__}")
                self._writer_failure = cleanup
            raise

    @property
    def writer_available(self) -> bool:
        return not self._closed and self._writer_failure is None

    @asynccontextmanager
    async def read_session(self) -> AsyncIterator[AsyncSession]:
        """Own a reader without auto-commit; caller-owned sessions keep their mode."""
        async with self._sessions(autoflush=False) as session:
            yield session

    @staticmethod
    def _configure_sqlite_connection(
        dbapi_connection: Any,
        _connection_record: Any,
    ) -> None:
        """Enable integrity and bounded writer waiting for concurrent workers."""

        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    @staticmethod
    def _ensure_sqlite_parent(url: str) -> None:
        prefix = "sqlite+aiosqlite:///"
        if not url.startswith(prefix):
            return
        path = Path(url.removeprefix(prefix))
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)

    async def create_schema(self) -> None:
        """Create all tables for tests; deployments use Alembic migrations."""
        # Declare current read indexes without changing the frozen 0056 schema.
        from sqlalchemy import Index

        from yuki.runtime import work_recovery_schema as _recovery  # noqa: F401
        from yuki.runtime import work_schema_v1 as _work_schema

        if not any(
            index.name == "ix_runtime_work_state_updated" for index in _work_schema.work.indexes
        ):
            Index(
                "ix_runtime_work_state_updated",
                _work_schema.work.c.state,
                _work_schema.work.c.updated,
            )

        async with self.engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[
                        table
                        for table in Base.metadata.sorted_tables
                        if table.name != "runtime_work_budgets"
                    ],
                )
            )
            from yuki.runtime.work_budget_schema import create_current_budget_tables

            await connection.run_sync(create_current_budget_tables)
            from yuki.runtime.effect_schema import install_indexes

            await connection.run_sync(install_indexes)
            await connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_runtime_media_refs_sha "
                "ON runtime_work_media_refs(sha256)"
            )
            from yuki.runtime.protocol_schema import install_quota as install_protocol_quota

            await connection.run_sync(install_protocol_quota)
            await self._create_fts_schema(connection)

    @staticmethod
    async def _create_fts_schema(connection: Any) -> None:
        """Create the current history index and the independent Memory FTS."""

        from yuki.persistence.history_search_index import CREATE_HISTORY_FTS, DELETE_HISTORY_TRIGGER

        statements = (
            CREATE_HISTORY_FTS,
            DELETE_HISTORY_TRIGGER,
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_facts_fts USING fts5(
                content,
                memory_key,
                category,
                content='memory_facts',
                content_rowid='id',
                tokenize='trigram'
            )
            """,
            """
            CREATE TRIGGER IF NOT EXISTS memory_facts_fts_ai
            AFTER INSERT ON memory_facts BEGIN
                INSERT INTO memory_facts_fts(rowid, content, memory_key, category)
                VALUES (new.id, new.content, new.memory_key, new.category);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS memory_facts_fts_ad
            AFTER DELETE ON memory_facts BEGIN
                INSERT INTO memory_facts_fts(
                    memory_facts_fts, rowid, content, memory_key, category
                ) VALUES ('delete', old.id, old.content, old.memory_key, old.category);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS memory_facts_fts_au
            AFTER UPDATE OF content, memory_key, category ON memory_facts BEGIN
                INSERT INTO memory_facts_fts(
                    memory_facts_fts, rowid, content, memory_key, category
                ) VALUES ('delete', old.id, old.content, old.memory_key, old.category);
                INSERT INTO memory_facts_fts(rowid, content, memory_key, category)
                VALUES (new.id, new.content, new.memory_key, new.category);
            END
            """,
        )
        for statement in statements:
            await connection.execute(text(statement))

    async def ping(self) -> bool:
        """Check database connectivity without exposing its path."""

        try:
            async with self.read_session() as session:
                await session.execute(text("SELECT 1"))
            return True
        except (OSError, RuntimeError, SQLAlchemyError):
            return False

    @asynccontextmanager
    async def immediate_session(self) -> AsyncIterator[AsyncSession]:
        """Admit one writer before checkout; release only after its actual exit."""
        started = time.monotonic()
        committed = False
        failure: BaseException | None = None
        cancellation: asyncio.CancelledError | None = None
        async with self._writer:
            self._record_timing("writer_admission_wait", time.monotonic() - started)
            if not self.writer_available:
                raise RuntimeError("database_writer_unavailable") from self._writer_failure
            session = self._sessions()
            driver: SQLiteConnection | None = None
            phase = "begin"
            connection_started = time.monotonic()
            transaction_started = None
            cleanup_started = None
            try:
                connection = await session.connection()
                self._record_timing(
                    "writer_connection_acquisition", time.monotonic() - connection_started
                )
                raw = await connection.get_raw_connection()
                if isinstance(raw.driver_connection, SQLiteConnection):
                    driver = raw.driver_connection
                transaction_started = time.monotonic()
                if self.url.startswith("sqlite+"):
                    await session.execute(text("BEGIN IMMEDIATE"))
                phase = "execute"
                yield session
                phase = "flush"
                _, cancellation = await _complete(session.flush())
                if cancellation is not None:
                    raise cancellation
                phase = "commit"
                commit_started = time.monotonic()
                try:
                    _, cancellation = await _complete(session.commit())
                finally:
                    self._record_timing("commit", time.monotonic() - commit_started)
                committed = True
            except BaseException as original:
                failure = original
                original.add_note(f"database_write_phase:{phase}")
                # SQLAlchemy's failed flush can replace an incoming cancellation
                # with its own rollback error. Keep the actual chained request.
                cause: BaseException | None = original
                while cause is not None:
                    if isinstance(cause, asyncio.CancelledError):
                        cancellation = cancellation or cause
                        break
                    cause = cause.__cause__ or cause.__context__
                cleanup_started = time.monotonic()
                try:
                    try:
                        _, cancelled = await _complete(session.rollback())
                    finally:
                        self._record_timing("rollback", time.monotonic() - cleanup_started)
                    cancellation = cancellation or cancelled
                    original.add_note("database_rollback:confirmed")
                except BaseException as cleanup:
                    original.add_note(f"rollback_failed:{type(cleanup).__name__}")
                    try:
                        _, cancelled = await _complete(session.invalidate())
                        cancellation = cancellation or cancelled
                    except BaseException as invalidation:
                        original.add_note(f"invalidation_failed:{type(invalidation).__name__}")
            finally:
                close_failure = None
                try:
                    _, cancelled = await _complete(session.close())
                    cancellation = cancellation or cancelled
                except BaseException as cleanup:
                    close_failure = cleanup
                    cleanup_started = cleanup_started or time.monotonic()
                    if failure is not None:
                        failure.add_note(f"invalidation_failed:{type(cleanup).__name__}")
                    logger.error("database_writer_cleanup_failed error=%s", type(cleanup).__name__)
                try:
                    if driver is not None:
                        stopped = driver.stop() if close_failure else driver.physical_exit
                        if stopped is not None:
                            _, cancelled = await _complete(stopped)
                            cancellation = cancellation or cancelled
                            if failure is not None and phase != "commit":
                                failure.add_note("database_rollback:confirmed")
                    elif close_failure is not None:
                        raise close_failure
                except BaseException as cleanup:
                    self._writer_failure = cleanup
                    if failure is not None:
                        failure.add_note(f"invalidation_failed:{type(cleanup).__name__}")
                    elif not committed:
                        failure = cleanup
                        failure.add_note(f"database_write_phase:{phase}")
                if cleanup_started is not None:
                    self._record_timing("writer_cleanup", time.monotonic() - cleanup_started)
                if transaction_started is not None:
                    self._record_timing(
                        "writer_transaction", time.monotonic() - transaction_started
                    )
        # This is synchronous after owner release: cancellation cannot lose a
        # successful final wakeup, and savepoint release never publishes one.
        if committed:
            self.commit_hints.publish()
        if cancellation is not None:
            cancellation.add_note(f"database_write_phase:{phase}")
            if committed:
                cancellation.add_note("database_commit:confirmed")
            raise cancellation from failure
        if failure is not None:
            raise failure

    def _record_timing(self, phase: str, seconds: float) -> None:
        if self.sqlite_diagnostics is not None:
            self.sqlite_diagnostics.record(phase, seconds)

    async def close(self) -> None:
        """Dispose pooled database connections."""
        async with self._writer:
            self._closed = True
            _, cancellation = await _complete(self.engine.dispose())
        if cancellation is not None:
            raise cancellation
