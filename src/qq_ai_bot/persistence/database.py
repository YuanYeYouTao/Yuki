"""Database engine lifecycle and health checks."""

from __future__ import annotations

import asyncio
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

from qq_ai_bot.persistence.metadata import Base

if TYPE_CHECKING:
    from qq_ai_bot.admin.models import WorkStorageRuntimeConfig

_SQLITE_BUSY_TIMEOUT_MS = 5_000


class Database:
    """Own the async SQLAlchemy engine and explicit session factory."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.subagents_enabled = False
        self.work_result_store: Any = None
        self.protocol_storage_policy: Callable[[], Awaitable[WorkStorageRuntimeConfig]] | None = (
            None
        )
        self.subagent_concurrency = 2
        self.subagent_max_queued = 8
        self.subagent_max_active_per_root = 8
        self._protocol_store_path: Path | None = None
        self._protocol_storage_lock: asyncio.Lock | None = None
        self._protocol_gc_iterator: Iterator[Path] | None = None
        self._ensure_sqlite_parent(url)
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        if url.startswith("sqlite+aiosqlite:///"):
            event.listen(self.engine.sync_engine, "connect", self._configure_sqlite_connection)
            from qq_ai_bot.persistence.sqlite_diagnostics import install_sqlite_diagnostics

            install_sqlite_diagnostics(self.engine.sync_engine)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

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

        from qq_ai_bot.conversation.rollup import signals as _signals  # noqa: F401
        from qq_ai_bot.runtime import work_recovery_schema as _recovery
        from qq_ai_bot.runtime import work_schema_v1 as _work_schema

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
                        if table.name not in {"runtime_work_budgets", "runtime_automation_budgets"}
                    ],
                )
            )
            from qq_ai_bot.runtime.work_budget_schema import create_current_budget_tables

            await connection.run_sync(create_current_budget_tables)
            from qq_ai_bot.runtime.effect_schema import install_indexes

            await connection.run_sync(install_indexes)
            await connection.run_sync(_recovery.install_quota)
            from qq_ai_bot.runtime.protocol_schema import install_quota as install_protocol_quota

            await connection.run_sync(install_protocol_quota)
            await self._create_fts_schema(connection)

    @staticmethod
    async def _create_fts_schema(connection: Any) -> None:
        """Create external-content FTS indexes used by isolated test databases."""

        from qq_ai_bot.asr.schema import CHAT_FTS_0055

        statements = (
            *CHAT_FTS_0055,
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
            async with self.sessions() as session:
                await session.execute(text("SELECT 1"))
            return True
        except (OSError, RuntimeError, SQLAlchemyError):
            return False

    @asynccontextmanager
    async def immediate_session(self) -> AsyncIterator[AsyncSession]:
        """Open one short writer transaction, using BEGIN IMMEDIATE on SQLite."""

        async with self.sessions() as session:
            try:
                if self.url.startswith("sqlite+"):
                    await session.execute(text("BEGIN IMMEDIATE"))
                else:
                    await session.begin()
                yield session
                await session.commit()
            except BaseException as original:
                try:
                    await session.rollback()
                except BaseException as cleanup:
                    # Never replace the error that determines execution certainty.
                    original.add_note(f"rollback_failed:{type(cleanup).__name__}")
                    try:
                        await session.invalidate()
                    except BaseException as invalidation:
                        original.add_note(f"invalidation_failed:{type(invalidation).__name__}")
                raise

    async def close(self) -> None:
        """Dispose pooled database connections."""

        await self.engine.dispose()
