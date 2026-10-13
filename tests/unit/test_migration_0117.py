"""The unused diagnostic table retires without touching execution storage."""

import asyncio

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from qq_ai_bot.persistence.database import Database


def test_upgrade_removes_only_agent_action_diagnostics(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'actions.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0116")

    async def inspect(*, seed=False):
        database = Database(url)
        try:
            async with database.immediate_session() as session:
                if seed:
                    await session.execute(
                        text(
                            "INSERT INTO agent_actions "
                            "(actor_user_id, action, success, duration_seconds, created_at) "
                            "VALUES ('retired-account', 'get_version_info', 1, 0.2, "
                            "CURRENT_TIMESTAMP)"
                        )
                    )
                assert not list(await session.execute(text("PRAGMA foreign_key_check")))
                return {
                    row.name: row.sql
                    for row in await session.execute(
                        text("SELECT name, sql FROM sqlite_master WHERE type='table'")
                    )
                }
        finally:
            await database.close()

    original = asyncio.run(inspect(seed=True))
    command.upgrade(config, "head")
    current = asyncio.run(inspect())
    assert "agent_actions" not in current
    assert current == {name: sql for name, sql in original.items() if name != "agent_actions"}
    assert "runtime_work_effects" in current
    command.upgrade(config, "head")
    assert asyncio.run(inspect()) == current
