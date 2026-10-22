"""Retirement keeps disabled group state and original receipts intact."""

import asyncio
import json

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from tests.conftest import make_settings

from yuki.admin.config_service import RuntimeConfigService
from yuki.persistence.database import Database
from yuki.persistence.models import RuntimeConfigOverrideModel


def test_0114_moves_disabled_spaces_to_editable_config(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'admin-retirement.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0113")

    async def verify():
        from tests.support.canonical_identity import ensure_space

        from yuki.identity.db_models import CanonicalSpaceModel

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
            # Seed the historical 0113 format without today's Person ORM.
            async with database.immediate_session() as writer:
                await writer.execute(
                    text("""
                    INSERT INTO runtime_config_overrides (
                        config_key, scope_type, canonical_space_id, value_json, value_type,
                        apply_mode, version, created_at, updated_at, updated_by
                    ) VALUES (
                        'conversation.autonomous_enabled', 'group', :space_id, 'true',
                        'boolean', 'hot', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, '10001'
                    )
                """),
                    {"space_id": overridden},
                )
                await writer.execute(
                    text("""
                    INSERT INTO admin_operation_events (
                        actor_user_id, trigger_message_id, conversation_key, capability,
                        operation, target_type, target_id, before_json, after_json,
                        success, duration_seconds, created_at
                    ) VALUES (
                        '10001', 'original', 'test', 'runtime_config', 'set_override',
                        'config.group', 'conversation.autonomous_enabled', 'null',
                        :after_json, 1, 0, CURRENT_TIMESTAMP
                    )
                """),
                    {"after_json": json.dumps({"scope_id": overridden, "value": True})},
                )
            async with database.read_session() as reader:
                audits = [
                    tuple(row)
                    for row in await reader.execute(text("SELECT * FROM admin_operation_events"))
                ]
            await asyncio.to_thread(command.upgrade, config, "0114")
            async with database.read_session() as reader:
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
            # Preserve the 0114 assertions above, then use current services only
            # after the real remaining migration chain has been applied.
            await asyncio.to_thread(command.upgrade, config, "head")
            assert not (await runtime.snapshot(group_id=off)).conversation.autonomous_enabled
            assert not (await runtime.snapshot(group_id=overridden)).conversation.autonomous_enabled
            assert (await runtime.snapshot(group_id=enabled)).conversation.autonomous_enabled
            # The former disabled state can now be changed through the existing store.
            await runtime.set_override(
                "conversation.autonomous_enabled",
                True,
                scope_type="group",
                scope_id=off,
                actor_user_id="10001",
                trigger_message_id="enable",
                conversation_key="test",
            )
            assert (await runtime.snapshot(group_id=off)).conversation.autonomous_enabled
            assert enabled not in {row.canonical_space_id for row in rows}
        finally:
            await database.close()

    asyncio.run(verify())
