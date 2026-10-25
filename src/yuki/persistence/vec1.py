"""Pinned Vec1 extension on the original SQLite connection and transaction."""

from __future__ import annotations

import sys
from pathlib import Path

import aiosqlite
from sqlalchemy import Integer, LargeBinary, column, table, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import TableClause


def extension_path() -> Path:
    suffix = ".dll" if sys.platform == "win32" else ".dylib" if sys.platform == "darwin" else ".so"
    return Path(__file__).resolve().parents[1] / "_native" / f"vec1{suffix}"


async def load_vec1(driver: aiosqlite.Connection) -> None:
    await driver.enable_load_extension(True)
    try:
        await driver.load_extension(str(extension_path()))
    finally:
        await driver.enable_load_extension(False)


def vector_table(profile_id: int) -> TableClause:
    return table(
        f"memory_vectors_{int(profile_id)}",
        column("rowid", Integer),
        column("fact_id", Integer),
        column("vector", LargeBinary),
    )


def delete_trigger(profile_id: int) -> str:
    name = vector_table(profile_id).name
    return (
        f"CREATE TRIGGER {name}_delete AFTER DELETE ON memory_embeddings "
        f"WHEN old.profile_id={int(profile_id)} BEGIN "
        f"DELETE FROM {name} WHERE rowid=old.id; END"
    )


async def create_profile_vectors(session: AsyncSession, profile_id: int) -> None:
    name = vector_table(profile_id).name
    await session.execute(text(f"CREATE VIRTUAL TABLE {name} USING vec1(vector, fact_id)"))
    await session.execute(
        text(f"INSERT INTO {name}(cmd,arg) VALUES('rebuild', :config)"),
        {"config": '{"index":"flat","distance":"cos"}'},
    )
    await session.execute(text(delete_trigger(profile_id)))


async def drop_profile_vectors(session: AsyncSession, profile_id: int) -> None:
    name = vector_table(profile_id).name
    await session.execute(text(f"DROP TRIGGER {name}_delete"))
    await session.execute(text(f"DROP TABLE {name}"))
