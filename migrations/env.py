"""Async Alembic environment."""

from __future__ import annotations

import asyncio
import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from yuki.config import Settings
from yuki.persistence.metadata import Base

config = context.config
if config.config_file_name is not None and not logging.getLogger().handlers:
    fileConfig(config.config_file_name)

settings = Settings()
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without a live connection."""

    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    migration = context.get_context()
    starting = context.get_starting_revision_argument()
    steps = migration.opts["fn"]((starting,) if starting else (), migration)
    if any(step.is_upgrade and step.revision.revision == "0145" for step in steps):
        raise RuntimeError(
            "history search upgrade needs a live offline database; --sql cannot convert source text"
        )
    with context.begin_transaction():
        context.run_migrations()


def _run_sqlite_migrations(
    connection: Connection,
) -> None:
    """Run the frozen baseline or bridge in one explicit SQLite transaction."""

    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    connection.exec_driver_sql("PRAGMA busy_timeout=5000")
    connection.commit()
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        transactional_ddl=True,
    )
    migration = context.get_context()
    steps = tuple(migration.opts["fn"](migration.get_current_heads(), migration))
    connection.commit()
    complete = next(
        (
            index
            for index, step in enumerate(steps)
            if step.is_upgrade and step.revision.revision == "0145"
        ),
        None,
    )

    def apply(selected: tuple[object, ...]) -> None:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            transactional_ddl=True,
            fn=lambda _heads, _context: selected,
        )
        # Keep the frozen baseline and destructive 0049 bridge atomic.
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            context.run_migrations()
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    try:
        if complete is None:
            apply(steps)
        else:
            from yuki.persistence.history_search_index import rebuild_history_search

            if complete:
                apply(steps[:complete])
            rebuild_history_search(connection)
            apply(steps[complete:])
    finally:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations on a synchronous connection proxy."""

    if connection.dialect.name != "sqlite":
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            transactional_ddl=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    _run_sqlite_migrations(connection)


async def run_async_migrations() -> None:
    """Create an async engine and run its synchronous migration callback."""

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
