"""The frozen upgrade preserves Memory facts and permits real execution receipts."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from tests.support.legacy_conversation_seed import frozen_social_env, insert_frozen

from yuki.persistence.database import Database


def test_0111_preserves_original_receipts_and_accepts_execution_sources(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'memory-sources.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0110")

    async def verify():
        database = Database(url)
        try:
            env = await frozen_social_env(database)
            now = datetime.now(UTC)
            async with database.immediate_session() as writer:
                event = await writer.scalar(text("SELECT id FROM chat_events"))
                await insert_frozen(
                    writer,
                    "memory_facts",
                    {
                        "id": 1,
                        "scope_type": "self",
                        "visibility_type": "group",
                        "canonical_visibility_space_id": env.space,
                        "kind": "episode",
                        "memory_key": "original-paid-episode",
                        "category": "self_episode",
                        "content": "已生成并校验曲线绘图",
                        "normalized_content": "已生成并校验曲线绘图",
                        "importance": 3,
                        "confidence": 1.0,
                        "source_type": "explicit",
                        "authority": "agent_reflection",
                        "status": "active",
                        "conflict_state": "clear",
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                tool = {
                    "id": 1,
                    "conversation_key_hash": "a" * 64,
                    "trigger_event_id": event,
                    "bot_user_id": "80001",
                    "canonical_space_id": env.space,
                    "provider_id": "core",
                    "tool_name": "terminal_exec",
                    "success": True,
                    "result_excerpt": "原付费结果",
                    "result_characters": 5,
                    "execution_id": "original-execution",
                    "tool_call_id": "original-call",
                    "source_call_key": "b" * 64,
                    "created_at": now,
                    "expires_at": now + timedelta(days=7),
                }
                mutation = {
                    "id": 1,
                    "mutation_id": str(uuid4()),
                    "idempotency_key": "c" * 64,
                    "claim_fingerprint": "d" * 64,
                    "target_fingerprint": "e" * 64,
                    "trigger_source_type": "chat_event",
                    "trigger_event_id": event,
                    "conversation_key": "bot:80001:group:20001",
                    "current_group_id": "20001",
                    "turn_origin": "user_message",
                    "delegation_mode": "main_agent",
                    "trigger_actor_user_id": "10001",
                    "decision_actor_type": "agent",
                    "decision_actor_id": "original-execution",
                    "executed_by_bot_user_id": "80001",
                    "requested_operation": "create",
                    "applied_operation": "create",
                    "new_fact_id": 1,
                    "outcome": "committed",
                    "reason_code": "created",
                    "created_at": now,
                }
                await insert_frozen(writer, "memory_tool_receipts", tool)
                await insert_frozen(writer, "memory_mutation_receipts", mutation)
                await insert_frozen(
                    writer,
                    "memory_evidence",
                    {
                        "id": 1,
                        "fact_id": 1,
                        "tool_receipt_id": 1,
                        "source_speaker_user_id": "80001",
                        "relation": "agent_reflection",
                        "authority": "agent_reflection",
                        "excerpt": "原付费结果",
                        "created_at": now,
                    },
                )
                await insert_frozen(
                    writer,
                    "memory_fact_state_events",
                    {
                        "id": 1,
                        "fact_id": 1,
                        "action": "created",
                        "to_status": "active",
                        "reason_code": "created",
                        "created_at": now,
                    },
                )
            tables = (
                "memory_tool_receipts",
                "memory_mutation_receipts",
                "memory_facts",
                "memory_evidence",
                "memory_fact_state_events",
                "runtime_work",
                "runtime_work_effects",
                "runtime_automation_cursors",
            )
            async with database.read_session() as reader:
                # Later migrations may append source facts; preserve every
                # original field without treating those additions as data loss.
                original_columns = {
                    name: ",".join(
                        f'"{row[1]}"'
                        for row in await reader.execute(text(f'PRAGMA table_info("{name}")'))
                    )
                    for name in tables
                }

            async def snapshot():
                async with database.read_session() as reader:
                    return {
                        name: [
                            tuple(row)
                            for row in await reader.execute(
                                text(
                                    f'SELECT {original_columns[name]} FROM "{name}" ORDER BY rowid'
                                )
                            )
                        ]
                        for name in tables
                    }

            original = await snapshot()
            await asyncio.to_thread(command.upgrade, config, "0111")
            assert await snapshot() == original
            async with database.immediate_session() as writer:
                sql = await writer.scalar(
                    text(
                        "SELECT group_concat(sql) FROM sqlite_master "
                        "WHERE name IN ('memory_tool_receipts','memory_mutation_receipts')"
                    )
                )
                assert "ck_memory_tool_receipts_source" not in sql
                assert "ck_memory_mutation_trigger_source" not in sql
                await insert_frozen(
                    writer,
                    "memory_tool_receipts",
                    {
                        **tool,
                        "id": 2,
                        "trigger_event_id": None,
                        "execution_id": "scheduled-execution",
                        "source_call_key": "f" * 64,
                    },
                )
                await insert_frozen(
                    writer,
                    "memory_mutation_receipts",
                    {
                        **mutation,
                        "id": 2,
                        "mutation_id": str(uuid4()),
                        "idempotency_key": "9" * 64,
                        "claim_fingerprint": "8" * 64,
                        "trigger_source_type": "execution",
                        "trigger_event_id": None,
                        "decision_actor_id": "scheduled-execution",
                    },
                )
            retained = await snapshot()
            await asyncio.to_thread(command.upgrade, config, "head")
            assert await snapshot() == retained
            async with database.read_session() as reader:
                assert not list(await reader.execute(text("PRAGMA foreign_key_check")))
                rows = (
                    await reader.execute(
                        text(
                            "SELECT trigger_source_type,trigger_event_id,initiative_run_id "
                            "FROM memory_mutation_receipts ORDER BY id"
                        )
                    )
                ).all()
                assert rows == [("chat_event", event, None), ("execution", None, None)]
        finally:
            await database.close()

    asyncio.run(verify())
