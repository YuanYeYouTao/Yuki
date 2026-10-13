"""Fresh and retained 0104 databases converge without rewriting execution facts."""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Column, Integer, MetaData, Table, select, text
from tests.support.social_identity_cases import social_env

from qq_ai_bot.automation.authority import DelegatedAuthority, PermissionLevel
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.control_plane.paging import PageRequest
from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_space
from qq_ai_bot.persistence.control_automation_query import ControlAutomationQueryAdapter
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import AutomationVersionModel, RuntimeConfigOverrideModel
from qq_ai_bot.runtime.subagent_repository import SubagentRepository
from qq_ai_bot.runtime.subagent_schema import children
from qq_ai_bot.runtime.work_budget_schema import budgets
from qq_ai_bot.runtime.work_recovery_schema import invocations
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, journal, work
from qq_ai_bot.runtime.work_tree import budget_root_id

# Historical 0087 schema belongs to this upgrade fixture, not live runtime metadata.
automation_budgets = Table(
    "runtime_automation_budgets",
    MetaData(),
    Column("run_id", Integer, primary_key=True),
    Column("models", Integer, nullable=False),
    Column("tools", Integer, nullable=False),
    Column("model_limit", Integer),
    Column("tool_limit", Integer),
)


def test_0106_query_migration_removes_retired_overrides_without_removing_other_config(
    tmp_path, monkeypatch
):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'queries.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0106")

    async def seed():
        database = Database(url)
        try:
            async with database.immediate_session() as writer:
                person = await ensure_person(writer, "10001")
                space = await ensure_space(writer, "20001")
                now = datetime.now(UTC)
                for scope, person_id, space_id, key in (
                    ("global", None, None, "memory.max_referenced_targets"),
                    ("user", person, None, "memory.max_referenced_targets"),
                    ("group", None, space, "memory.max_referenced_targets"),
                    ("global", None, None, "context.compaction_window_tokens"),
                ):
                    writer.add(
                        RuntimeConfigOverrideModel(
                            config_key=key,
                            scope_type=scope,
                            canonical_person_id=person_id,
                            canonical_space_id=space_id,
                            value_json="60000" if key.startswith("context.") else "5",
                            value_type="integer",
                            apply_mode="hot",
                            created_at=now,
                            updated_at=now,
                            updated_by="test",
                        )
                    )
        finally:
            await database.close()

    asyncio.run(seed())
    command.upgrade(config, "head")

    async def verify():
        database = Database(url)
        try:
            async with database.sessions() as reader:
                rows = (await reader.scalars(select(RuntimeConfigOverrideModel))).all()
                assert [(row.config_key, row.value_json) for row in rows] == [
                    ("context.compaction_window_tokens", "60000")
                ]
                assert not list(await reader.execute(text("PRAGMA foreign_key_check")))
        finally:
            await database.close()

    asyncio.run(verify())


@pytest.mark.parametrize("from_0104", [False, True])
def test_fresh_and_0104_upgrade_preserve_work_identity_source_receipts_and_budget(
    tmp_path, monkeypatch, from_0104
):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'migration.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0104" if from_0104 else "head")
    root_id, child_id = str(uuid4()), str(uuid4())
    source = json.dumps({"origin": "user_message", "retained": "original source"})
    child_source = json.dumps({"worker": True, "parent_work_id": root_id})
    original_checkpoint = json.dumps({"protocol": "original response", "call_key": "original-call"})
    original_receipt = json.dumps({"result": "uncertain", "outcome": {"uncertain": True}})
    original_payload = json.dumps({"response": "original bytes", "provider_opaque": "preserved"})

    async def seed_0104():
        database = Database(url)
        try:
            env = await social_env(database, tmp_path)
            async with database.immediate_session() as session:
                for identity, parent, state, checkpoint in (
                    (
                        root_id,
                        None,
                        "waiting_external",
                        json.dumps(
                            {
                                "accepted_control": {"action": "complete", "call_key": "finish"},
                                "communication": {
                                    "reporting": "quiet",
                                    "stage_feedback_batch": "retired",
                                    "input_feedback_through_id": 9,
                                },
                            }
                        ),
                    ),
                    (child_id, root_id, "waiting_user", original_checkpoint),
                ):
                    await session.execute(
                        text(
                            "INSERT INTO runtime_work "
                            "(id, conversation_id, generation, source_key, source_json, goal, "
                            "state, model_requests, tool_calls, checkpoint_json, created, updated) "
                            "VALUES (:id, :conversation, 1, :id, :source, 'original goal', "
                            ":state, :models, :tools, :checkpoint, :now, :now)"
                        ),
                        {
                            "id": identity,
                            "conversation": env.context.conversation_id,
                            "source": child_source if parent else source,
                            "state": state,
                            "checkpoint": checkpoint,
                            "models": 2 if parent else 5,
                            "tools": 3 if parent else 8,
                            "now": time.time(),
                        },
                    )
                await session.execute(
                    text(
                        "INSERT INTO runtime_subagents (work_id, root_id, source_key, brief_json) "
                        "VALUES (:child, :root, 'original-spawn', '{\"goal\":\"original goal\"}')"
                    ),
                    {"child": child_id, "root": root_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO runtime_work_budgets "
                        "(root_id, models, tools, model_limit, tool_limit) "
                        "VALUES (:root, 7, 11, 12, 13)"
                    ),
                    {"root": root_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO runtime_work_effects "
                        "(effect_key, work_id, kind, state, receipt_json, created, updated) "
                        "VALUES ('original-call', :child, 'tool', 'unknown', :receipt, :now, :now)"
                    ),
                    {"child": child_id, "receipt": original_receipt, "now": time.time()},
                )
                await session.execute(
                    text(
                        "INSERT INTO runtime_work_journal "
                        "(work_id, chain_id, contract, source_revision, phase, payload_json, "
                        "updated) "
                        "VALUES (:child, :chain, 'worker:4', 1, 'response', :payload, :now)"
                    ),
                    {
                        "child": child_id,
                        "chain": str(uuid4()),
                        "payload": original_payload,
                        "now": time.time(),
                    },
                )
                for state in ("planned", "blocked", "accepted", "dispatching", "unknown"):
                    await session.execute(
                        text(
                            "INSERT INTO runtime_delivery_intents "
                            "(id, work_id, kind, state, created, updated) "
                            "VALUES (:state, :root, 'notice', :state, :now, :now)"
                        ),
                        {"root": root_id, "state": state, "now": time.time()},
                    )
        finally:
            await database.close()

    if from_0104:
        asyncio.run(seed_0104())
        command.upgrade(config, "head")

    async def verify_current():
        database = Database(url)
        try:
            async with database.sessions() as session:
                columns = {
                    row[1] for row in await session.execute(text("PRAGMA table_info(runtime_work)"))
                }
                assert "parent_work_id" in columns
                assert not {"output_kind", "deliver_artifacts"} & columns
                assert "root_id" not in {
                    row[1]
                    for row in await session.execute(text("PRAGMA table_info(runtime_subagents)"))
                }
                assert not list(await session.execute(text("PRAGMA foreign_key_check")))
                if from_0104:
                    original = (
                        (await session.execute(select(work).where(work.c.id == child_id)))
                        .mappings()
                        .one()
                    )
                    assert original["parent_work_id"] == root_id
                    assert original["source_json"] == child_source
                    assert original["checkpoint_json"] == original_checkpoint
                    assert original["state"] == "waiting_user"
                    assert await budget_root_id(session, child_id) == root_id
                    assert await session.scalar(select(effects.c.receipt_json)) == original_receipt
                    assert await session.scalar(select(effects.c.state)) == "unknown"
                    assert await session.scalar(select(journal.c.payload_json)) == original_payload
                    root_checkpoint = json.loads(
                        await session.scalar(
                            select(work.c.checkpoint_json).where(work.c.id == root_id)
                        )
                    )
                    assert root_checkpoint["accepted_control"] == {
                        "action": "complete",
                        "call_key": "finish",
                    }
                    assert root_checkpoint["communication"] == {"reporting": "quiet"}
                    budget = (await session.execute(select(budgets))).mappings().one()
                    assert (
                        budget["models"],
                        budget["tools"],
                        budget["model_limit"],
                        budget["tool_limit"],
                    ) == (7, 11, 12, 13)
                    assert set(
                        await session.scalars(text("SELECT state FROM runtime_delivery_intents"))
                    ) == {"accepted", "dispatching", "unknown"}
                    assert await session.scalar(select(children.c.source_key)) == "original-spawn"
            if not from_0104:
                env = await social_env(database, tmp_path)
                repository = WorkRepository(database)
                lease = await repository.acquire(env.context.conversation_id, 1)
                parent = await repository.accept(
                    lease, source_key="new-root", source={}, goal="new work"
                )
                derived = await repository.derive(lease, parent["id"], "new-derived", "new subgoal")
                identity = await SubagentRepository(repository).start(
                    lease, derived["id"], "new-worker", {"goal": "new worker"}
                )
                async with database.sessions() as session:
                    assert await budget_root_id(session, identity) == parent["id"]
                await repository.release(lease)
        finally:
            await database.close()

    asyncio.run(verify_current())


def test_0105_upgrade_keeps_missing_original_version_and_new_admission_freezes_current_facts(
    tmp_path, monkeypatch
):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'automation-migration.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0105")
    original_work_id = str(uuid4())
    started = datetime.now(UTC) - timedelta(days=40)
    script = {
        "version": 1,
        "name": "retained definition",
        "timezone": "UTC",
        "schedule": {"type": "interval", "seconds": 60},
        "steps": [
            {
                "id": "original_agent",
                "call": "yuki.agent",
                "arguments": {"instruction": "original target"},
            }
        ],
        "limits": {
            "max_steps": 1,
            "max_llm_calls": 8,
            "max_tool_calls": 12,
            "max_messages": 10,
            "timeout_seconds": 600,
        },
    }
    script_hash = "original-immutable-script-hash"
    cursor = json.dumps(
        {"next_step": 0, "work_id": original_work_id, "llm_calls": 2, "tool_calls": 3}
    )
    receipt = json.dumps({"original_call": "unknown", "run_id": 1})

    async def seed():
        database = Database(url)
        try:
            env = await social_env(database, tmp_path)
            authority = DelegatedAuthority(
                creator_user_id="10001",
                bot_user_id="80001",
                created_from_message_id="1",
                created_at=started.isoformat(),
                permission_level=PermissionLevel.USER,
                granted_capabilities=("yuki.agent",),
                capability_schema_versions={"yuki.agent": 1},
                current_group_id="20001",
                canonical_creator_person_id=env.person,
                canonical_conversation_id=env.context.conversation_id,
                conversation_generation=1,
                canonical_presence_id=env.presence,
                canonical_space_id=env.space,
            )
            async with database.immediate_session() as writer:
                await writer.execute(
                    text(
                        "INSERT INTO automations (id, creator_kind, creator_user_id, bot_user_id, "
                        "name, status, timezone, schedule_json, script_json, script_hash, "
                        "required_capabilities_json, authority_snapshot_json, "
                        "created_from_message_id, creation_source_key, next_run_at, run_count, "
                        "max_runs, consecutive_failures, misfire_grace_seconds, created_at, "
                        "updated_at, canonical_creator_person_id, canonical_target_space_id, "
                        "canonical_presence_id) VALUES (1, 'person', '10001', '80001', "
                        "'retained definition', 'active', 'UTC', :schedule, :script, :hash, "
                        "'[\"yuki.agent\"]', :authority, '1', 'original-create', "
                        ":future, 3, NULL, 0, 1800, :started, :started, :person, :space, :presence)"
                    ),
                    {
                        "schedule": json.dumps(script["schedule"]),
                        "script": json.dumps(script),
                        "hash": script_hash,
                        "authority": authority.model_dump_json(),
                        "future": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
                        "started": started.isoformat(),
                        "person": env.person,
                        "space": env.space,
                        "presence": env.presence,
                    },
                )
                await writer.execute(
                    text(
                        "INSERT INTO automation_versions (id, automation_id, version, "
                        "script_json, script_hash, updated_by, created_at) "
                        "VALUES (1, 1, 1, :script, :hash, '10001', :started)"
                    ),
                    {
                        "script": json.dumps(script),
                        "hash": script_hash,
                        "started": started.isoformat(),
                    },
                )
                await writer.execute(
                    text(
                        "INSERT INTO automation_runs (id, automation_id, scheduled_for, "
                        "actual_started_at, finished_at, status, idempotency_key, "
                        "steps_completed, llm_calls, tool_calls, messages_sent, "
                        "result_summary_json, created_at) VALUES (1, 1, :started, :started, "
                        ":started, 'succeeded', 'original-slot', 1, 2, 3, 0, '{}', :started)"
                    ),
                    {"started": started.isoformat()},
                )
                await writer.execute(
                    text(
                        "INSERT INTO runtime_automation_budgets "
                        "(run_id, models, tools, model_limit, tool_limit) VALUES (1, 2, 3, 8, 12)"
                    )
                )
                await writer.execute(
                    text(
                        "INSERT INTO runtime_automation_cursors "
                        "(run_id, script_hash, phase, payload_json, updated) "
                        "VALUES (1, :hash, 'ready', :cursor, :updated)"
                    ),
                    {"hash": script_hash, "cursor": cursor, "updated": started.timestamp()},
                )
                await writer.execute(
                    text(
                        "INSERT INTO runtime_work (id, conversation_id, generation, source_key, "
                        "source_json, goal, state, model_requests, tool_calls, checkpoint_json, "
                        "created, updated) VALUES (:id, :conversation, 1, 'original-work', "
                        ":source, 'retained target', 'waiting_user', 2, 3, '{}', "
                        ":updated, :updated)"
                    ),
                    {
                        "id": original_work_id,
                        "conversation": env.context.conversation_id,
                        "source": json.dumps(
                            {"owner": "automation", "automation_id": 1, "automation_run_id": 1}
                        ),
                        "updated": started.timestamp(),
                    },
                )
                await writer.execute(
                    text(
                        "INSERT INTO runtime_work_effects (effect_key, work_id, kind, state, "
                        "receipt_json, created, updated) VALUES ('original-effect', :id, 'tool', "
                        "'unknown', :receipt, :updated, :updated)"
                    ),
                    {"id": original_work_id, "receipt": receipt, "updated": started.timestamp()},
                )
                await writer.execute(
                    text(
                        "INSERT INTO automation_step_runs (id, run_id, step_id, capability, "
                        "status, input_summary_json, output_summary_json, started_at, finished_at) "
                        "VALUES (1, 1, 'original_agent', 'yuki.agent', 'succeeded', '{}', '{}', "
                        ":started, :started)"
                    ),
                    {"started": started.isoformat()},
                )
            async with database.sessions() as reader:
                return dict(
                    (await reader.execute(text("SELECT * FROM automation_runs WHERE id=1")))
                    .mappings()
                    .one()
                )
        finally:
            await database.close()

    original_run = asyncio.run(seed())
    command.upgrade(config, "head")

    async def verify():
        database = Database(url)
        repository = AutomationRepository(database)
        try:
            async with database.sessions() as reader:
                migrated = dict(
                    (await reader.execute(text("SELECT * FROM automation_runs WHERE id=1")))
                    .mappings()
                    .one()
                )
                assert migrated.pop("version_id") is None and migrated == original_run
                old_version = await reader.get(AutomationVersionModel, 1)
                assert old_version.authority_snapshot_json is None
                assert old_version.canonical_target_space_id is None
                assert await reader.scalar(select(invocations.c.payload_json)) == cursor
                assert await reader.scalar(select(effects.c.receipt_json)) == receipt
                assert await reader.scalar(select(effects.c.state)) == "unknown"
                old_budget = dict(
                    (await reader.execute(select(automation_budgets))).mappings().one()
                )
                assert old_budget == {
                    "run_id": 1,
                    "models": 2,
                    "tools": 3,
                    "model_limit": 8,
                    "tool_limit": 12,
                }
                assert not list(await reader.execute(text("PRAGMA foreign_key_check")))
            assert await repository.get_for_run(1) is None
            current = await repository.get(1)
            admitted = await repository.create_run(
                1, scheduled_for=current.next_run_at, actual_started_at=datetime.now(UTC)
            )
            accepted = await repository.get_for_run(admitted.id)
            assert admitted.version_id != 1 and accepted.script["goal"] == "original target"
            assert accepted.canonical_target_space_id == current.canonical_target_space_id
            assert accepted.authority_snapshot == current.authority_snapshot
            async with database.sessions() as reader:
                assert (await reader.get(AutomationVersionModel, 1)).authority_snapshot_json is None
                assert (
                    await reader.scalar(text("SELECT version_id FROM automation_runs WHERE id=1"))
                    is None
                )
                assert (
                    dict(
                        (
                            await reader.execute(
                                select(automation_budgets).where(automation_budgets.c.run_id == 1)
                            )
                        )
                        .mappings()
                        .one()
                    )
                    == old_budget
                )
                assert not (
                    await reader.execute(
                        select(automation_budgets).where(automation_budgets.c.run_id == admitted.id)
                    )
                ).first()
            audits = await ControlAutomationQueryAdapter(database.sessions).list_automation_steps(
                PageRequest(), automation_id=1
            )
            assert [item.resource_id for item in audits.items] == ["1"]
            assert audits.items[0].fields["run_id"] == 1
            assert (await repository.get(1)).run_count == 3
        finally:
            await database.close()

    asyncio.run(verify())
