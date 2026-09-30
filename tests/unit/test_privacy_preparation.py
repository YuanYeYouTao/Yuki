"""Atomic forget preserves valid JSON and rejects stale prepared owners."""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update
from tests.unit.test_rebuild_privacy_preparation import _selection
from tests.unit.test_rebuild_receipt_finalization import _seed

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.identity.db_models import CanonicalPersonModel, IdentityBindingModel
from qq_ai_bot.memory.rebuild.models import MemoryRebuildSelection
from qq_ai_bot.memory.rebuild.repository import MemoryRebuildRepository
from qq_ai_bot.persistence.models import (
    AdminOperationEventModel,
    ChatEventModel,
    MemoryRebuildRunModel,
)
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.persistence.repositories import EventLedgerRepository


async def test_forget_redacts_numeric_json_and_keeps_other_person_event(database):
    event, _ = await EventLedgerRepository(database).append(
        bot_user_id="9999",
        platform_message_id="numeric-privacy",
        scope_type=ScopeType.GROUP,
        group_id="2001",
        sender_user_id="1002",
        direction="inbound",
        content="mention 1001",
        segments=({"type": "at", "data": {"qq": 1001, "note": 'escaped "1001"'}},),
    )
    async with database.immediate_session() as writer:
        writer.add(
            AdminOperationEventModel(
                actor_user_id="1002",
                capability="test",
                operation="test",
                target_type="test",
                before_json='{"owner":1001,"keep":true}',
                after_json="null",
                success=True,
                duration_seconds=0,
                created_at=datetime.now(UTC),
            )
        )
    assert await PeopleRepository(database).delete_person("1001")
    async with database.sessions() as reader:
        remaining = await reader.get(ChatEventModel, event.id)
        assert remaining is not None
        segment = json.loads(remaining.segments_json)[0]["data"]
        assert segment == {"qq": "[已删除用户]", "note": 'escaped "[已删除用户]"'}
        audit = await reader.scalar(select(AdminOperationEventModel))
        assert json.loads(audit.before_json) == {"owner": "[已删除用户]", "keep": True}


async def test_forget_reprepares_changed_rebuild_selection_before_atomic_write(
    database, monkeypatch
):
    seeded = await _seed(database, 1, proposals=True)
    rebuilds = MemoryRebuildRepository(database)
    prepare = rebuilds.prepare_forget_people
    attempts = 0

    async def race(aliases):
        nonlocal attempts
        plan = await prepare(aliases)
        attempts += 1
        if attempts == 1:
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(MemoryRebuildRunModel)
                    .where(
                        MemoryRebuildRunModel.id == seeded.run_id,
                    )
                    .values(**_selection(sender_user_ids=("1001", "10011")))
                )
        return plan

    monkeypatch.setattr(rebuilds, "prepare_forget_people", race)
    assert await PeopleRepository(database, memory_rebuilds=rebuilds).delete_person("1001")
    assert attempts == 2
    async with database.sessions() as reader:
        run = await reader.get(MemoryRebuildRunModel, seeded.run_id)
        assert MemoryRebuildSelection.model_validate_json(run.selection_json).sender_user_ids == (
            "10011",
        )


async def test_invalid_legacy_json_rolls_back_entire_privacy_transaction(database):
    async with database.immediate_session() as writer:
        writer.add(
            AdminOperationEventModel(
                actor_user_id="1001",
                capability="test",
                operation="test",
                target_type="test",
                before_json='{"owner":1001',
                after_json="null",
                success=True,
                duration_seconds=0,
                created_at=datetime.now(UTC),
            )
        )
    with pytest.raises(json.JSONDecodeError):
        await PeopleRepository(database).delete_person("1001")
    async with database.sessions() as reader:
        assert (
            await reader.scalar(
                select(CanonicalPersonModel).where(
                    CanonicalPersonModel.id.in_(
                        select(IdentityBindingModel.person_id).where(
                            IdentityBindingModel.external_account_id == "1001"
                        )
                    ),
                )
            )
            is not None
        )
