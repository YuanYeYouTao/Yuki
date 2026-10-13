"""Retire diagnostics while retaining canonical owner, source watermarks and backlog."""

import asyncio
from datetime import UTC, datetime

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from qq_ai_bot.identity.canonical_repository import ensure_person
from qq_ai_bot.persistence.database import Database


def test_upgrade_preserves_reflection_work_state(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'reflection.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0114")

    async def seed():
        db = Database(url)
        try:
            async with db.immediate_session() as writer:
                person = await ensure_person(writer, "1001")
                await writer.execute(
                    text(
                        "INSERT INTO memory_self_reflection_states "
                        "(id, conversation_key_hash, bot_user_id, canonical_person_id, "
                        "last_event_id, latest_event_id, pending_events, pending_characters, "
                        "pending_since, updated_at, has_yuki_reply, has_tool_result, "
                        "high_value_signal, last_policy_reason, last_policy_event_id) "
                        "VALUES (1, :hash, '8000', :person, 2, 7, 3, 42, :now, :now, "
                        "1, 1, 0, 'old_diagnostic', 7)"
                    ),
                    {"hash": "a" * 64, "person": person, "now": datetime.now(UTC)},
                )
            async with db.sessions() as reader:
                return dict(
                    (
                        await reader.execute(
                            text(
                                "SELECT id, conversation_key_hash, bot_user_id, "
                                "canonical_person_id, last_event_id, latest_event_id, "
                                "pending_events, pending_characters, pending_since, updated_at "
                                "FROM memory_self_reflection_states"
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
        finally:
            await db.close()

    original = asyncio.run(seed())
    command.upgrade(config, "head")

    async def verify():
        db = Database(url)
        try:
            async with db.sessions() as reader:
                saved = dict(
                    (await reader.execute(text("SELECT * FROM memory_self_reflection_states")))
                    .mappings()
                    .one()
                )
                assert saved == {**original, "canonical_space_id": None}
                assert not list(await reader.execute(text("PRAGMA foreign_key_check")))
        finally:
            await db.close()

    asyncio.run(verify())
