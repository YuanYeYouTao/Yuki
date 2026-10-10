"""Fresh and retained 0104 databases converge without rewriting execution facts."""

import asyncio
import json
import time
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from tests.support.social_identity_cases import social_env

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.subagent_repository import SubagentRepository
from qq_ai_bot.runtime.subagent_schema import children
from qq_ai_bot.runtime.work_budget_schema import budgets
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, journal, work
from qq_ai_bot.runtime.work_tree import budget_root_id


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
                assert (
                    await session.scalar(text("SELECT version_num FROM alembic_version")) == "0105"
                )
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
