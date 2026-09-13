"""Migration preserves running work evidence and imports only unique provenance."""

import importlib
import json
from datetime import UTC, datetime

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select
from tests.support.social_identity_cases import social_env

from qq_ai_bot.persistence.models import ChatEventModel, WebSearchRunModel
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, inputs
from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository


@pytest.mark.asyncio
async def test_migration_preserves_work_and_receipts(database, tmp_path, monkeypatch):
    await social_env(database, tmp_path)
    async with database.sessions() as session:
        event = await session.scalar(select(ChatEventModel))
    conv = event.canonical_conversation_id
    repo = WorkRepository(database)
    lease = await repo.acquire(conv, 1)
    source = {
        "trigger_event_id": event.id,
        "trigger_id": event.platform_message_id,
        "actor_user_id": event.sender_user_id,
        "conversation_id": conv,
        "origin": "user_message",
    }
    item = await repo.accept(
        lease,
        source_key=f"message:{conv}:{event.platform_message_id}",
        source=source,
        goal="keep original execution",
    )
    await repo.checkpoint(
        lease, item["id"], {"pending_run_id": "original-execution"}, models=3, tools=2
    )
    await repo.enqueue(
        conv,
        1,
        f"message:{conv}:{event.platform_message_id}",
        kind="message",
        event_id=event.id,
        work_id=item["id"],
    )
    await repo.prepare_effect(lease, item["id"], "already-sent", "message")
    await repo.record_effect("already-sent", "accepted", {"message_id": "transport-receipt"})
    tasks = SandboxTaskRepository(database)
    await tasks.prepare("original-request", {"command": "already-running"}, source)
    async with database.sessions() as session, session.begin():
        for platform in (event.platform_message_id, "unknown-old-platform-id"):
            session.add(
                WebSearchRunModel(
                    conversation_key="same",
                    trigger_message_id=platform,
                    query="q",
                    provider="legacy",
                    created_at=datetime.now(UTC),
                    canonical_conversation_id=conv,
                )
            )
    before = await repo.get(item["id"])
    migration = importlib.import_module("migrations.versions.0058_internal_source_anchors")

    def upgrade(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()

    async with database.engine.begin() as connection:
        await connection.run_sync(upgrade)
    after = await repo.get(item["id"])
    for key in ("id", "state", "revision", "model_requests", "tool_calls", "checkpoint_json"):
        assert after[key] == before[key]
    assert json.loads(after["checkpoint_json"])["pending_run_id"] == "original-execution"
    assert after["source_key"] == f"event:{conv}:{event.id}"
    source.pop("trigger_id")
    replay = await tasks.prepare("original-request", {"command": "already-running"}, source)
    assert replay.request_id == "original-request"
    assert (
        await repo.accept(
            lease, source_key=after["source_key"], source=source, goal="keep original execution"
        )
    )["id"] == item["id"]
    async with database.sessions() as session:
        assert (await session.execute(select(inputs.c.source_key))).scalar_one() == after[
            "source_key"
        ]
        assert (await session.execute(select(effects.c.state))).scalar_one() == "accepted"
        rows = list(await session.scalars(select(WebSearchRunModel)))
        assert len(rows) == 1 and rows[0].trigger_event_id == event.id
