"""Atomic forget preserves valid JSON and rejects stale prepared owners."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from tests.support.event_seed import seed_event
from tests.unit.test_rebuild_privacy_preparation import _selection
from tests.unit.test_rebuild_receipt_finalization import _seed

from yuki.domain.conversations import ScopeType
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel
from yuki.memory.rebuild.models import MemoryRebuildSelection
from yuki.memory.rebuild.repository import MemoryRebuildRepository
from yuki.persistence.models import (
    AdminOperationEventModel,
    ChatEventModel,
    MemoryRebuildRunModel,
)
from yuki.persistence.people_repository import PeopleRepository
from yuki.persistence.repositories import EventLedgerRepository


@pytest.fixture(autouse=True)
async def pre_0121_binding(database):
    """These legacy redaction tests retain the pre-0121 unbounded semantics."""
    async with database.immediate_session() as writer:
        await writer.execute(
            update(IdentityBindingModel)
            .where(IdentityBindingModel.external_account_id == "1001")
            .values(history_starts_at=None)
        )


async def test_forget_redacts_numeric_json_and_keeps_other_person_event(database):
    event, _ = await seed_event(
        EventLedgerRepository(database),
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


@pytest.mark.parametrize("races", [1, 4])
async def test_forget_reprepares_changed_rebuild_selection_before_atomic_write(
    database, monkeypatch, races
):
    seeded = await _seed(database, 1, proposals=True)
    rebuilds = MemoryRebuildRepository(database)
    prepare = rebuilds.prepare_forget_people
    attempts = 0
    async with database.sessions() as reader:
        people = dict(
            (
                await reader.execute(
                    select(
                        IdentityBindingModel.external_account_id, IdentityBindingModel.person_id
                    ).where(IdentityBindingModel.external_account_id.in_(("1001", "1002")))
                )
            ).all()
        )
    changing_ids = [str(uuid4()) for _ in range(races)]

    async def race(aliases, **kwargs):
        nonlocal attempts
        plan = await prepare(aliases, **kwargs)
        attempts += 1
        if attempts <= races:
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(MemoryRebuildRunModel)
                    .where(
                        MemoryRebuildRunModel.id == seeded.run_id,
                    )
                    .values(
                        **_selection(
                            person_ids=(people["1001"], people["1002"], changing_ids[attempts - 1])
                        )
                    )
                )
        return plan

    monkeypatch.setattr(rebuilds, "prepare_forget_people", race)
    assert await PeopleRepository(database, memory_rebuilds=rebuilds).delete_person("1001")
    assert attempts == races + 1
    async with database.sessions() as reader:
        run = await reader.get(MemoryRebuildRunModel, seeded.run_id)
        selected = MemoryRebuildSelection.model_validate_json(run.selection_json)
        assert set(selected.person_ids) == {people["1002"], changing_ids[-1]}


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
