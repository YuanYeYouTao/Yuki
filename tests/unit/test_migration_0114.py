"""Retirement keeps disabled group state and original receipts intact."""

import asyncio
import json

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from tests.conftest import make_settings

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import RuntimeConfigOverrideModel


def test_0114_moves_disabled_spaces_to_editable_config(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'admin-retirement.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0113")

    async def verify():
        from qq_ai_bot.identity.canonical_repository import ensure_space
        from qq_ai_bot.identity.db_models import CanonicalSpaceModel

        database = Database(url)
        try:
            runtime = RuntimeConfigService(settings=make_settings(url), database=database)
            async with database.immediate_session() as writer:
                off = await ensure_space(writer, "20001")
                overridden = await ensure_space(writer, "20002")
                enabled = await ensure_space(writer, "20003")
                await writer.execute(
                    text("UPDATE spaces SET autonomous_enabled = 0 WHERE id IN (:a,:b)"),
                    {"a": off, "b": overridden},
                )
            await runtime.set_override(
                "conversation.autonomous_enabled",
                True,
                scope_type="group",
                scope_id="20002",
                actor_user_id="10001",
                trigger_message_id="original",
                conversation_key="test",
            )
            async with database.sessions() as reader:
                audits = [
                    tuple(row)
                    for row in await reader.execute(text("SELECT * FROM admin_operation_events"))
                ]
            await asyncio.to_thread(command.upgrade, config, "0114")
            async with database.sessions() as reader:
                rows = list(await reader.scalars(select(RuntimeConfigOverrideModel)))
                assert {row.canonical_space_id for row in rows} == {off, overridden}
                assert all(json.loads(row.value_json) is False for row in rows)
                assert {row.canonical_space_id: row.version for row in rows} == {
                    off: 1,
                    overridden: 2,
                }
                assert all(await reader.scalars(select(CanonicalSpaceModel.autonomous_enabled)))
                assert [
                    tuple(row)
                    for row in await reader.execute(text("SELECT * FROM admin_operation_events"))
                ] == audits
                assert not list(await reader.execute(text("PRAGMA foreign_key_check")))
            assert not (await runtime.snapshot(group_id="20001")).conversation.autonomous_enabled
            assert not (await runtime.snapshot(group_id="20002")).conversation.autonomous_enabled
            assert (await runtime.snapshot(group_id="20003")).conversation.autonomous_enabled
            # The former disabled state can now be changed through the existing store.
            await runtime.set_override(
                "conversation.autonomous_enabled",
                True,
                scope_type="group",
                scope_id="20001",
                actor_user_id="10001",
                trigger_message_id="enable",
                conversation_key="test",
            )
            assert (await runtime.snapshot(group_id="20001")).conversation.autonomous_enabled
            assert enabled not in {row.canonical_space_id for row in rows}
        finally:
            await database.close()

    asyncio.run(verify())
