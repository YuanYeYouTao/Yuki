"""Atomic forget preserves valid JSON and rejects stale prepared owners."""

import json
from contextlib import asynccontextmanager
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
from yuki.persistence.people_repository import PeopleRepository, PrivacyPreparationChanged
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
    async with database.read_session() as reader:
        remaining = await reader.get(ChatEventModel, event.id)
        assert remaining is not None
        segment = json.loads(remaining.segments_json)[0]["data"]
        assert segment == {"qq": "[已删除用户]", "note": 'escaped "[已删除用户]"'}
        audit = await reader.scalar(select(AdminOperationEventModel))
        assert json.loads(audit.before_json) == {"owner": "[已删除用户]", "keep": True}


async def test_forget_preserves_unknown_commit_instead_of_repreparing_as_source_change(
    database, monkeypatch
):
    from unittest.mock import AsyncMock

    from yuki.identity.canonical_repository import find_identity_binding
    from yuki.persistence.database import write_failure_committed, write_failure_phase

    async with database.read_session() as reader:
        person_id = (await find_identity_binding(reader, "1001")).person_id
    repository = PeopleRepository(database)
    prepare = AsyncMock(wraps=repository.prepare_person_forget)
    monkeypatch.setattr(repository, "prepare_person_forget", prepare)
    original_writer = database.immediate_session
    failure = PrivacyPreparationChanged()

    @asynccontextmanager
    async def lose_ack():
        async with original_writer() as writer:
            commit = writer.commit

            async def commit_then_lose_ack():
                await commit()
                raise failure

            monkeypatch.setattr(writer, "commit", commit_then_lose_ack)
            yield writer

    monkeypatch.setattr(database, "immediate_session", lose_ack)
    with pytest.raises(PrivacyPreparationChanged) as caught:
        await repository.delete_person_id(person_id)
    assert caught.value is failure
    assert write_failure_phase(failure) == "commit"
    assert write_failure_committed(failure) is None
    prepare.assert_awaited_once()
    async with database.read_session() as reader:
        assert await reader.get(CanonicalPersonModel, person_id) is None


@pytest.mark.parametrize("races", [1, 4])
async def test_forget_reprepares_changed_rebuild_selection_before_atomic_write(
    database, monkeypatch, races
):
    seeded = await _seed(database, 1, proposals=True)
    rebuilds = MemoryRebuildRepository(database)
    prepare = rebuilds.prepare_forget_people
    attempts = 0
    async with database.read_session() as reader:
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
    async with database.read_session() as reader:
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
    async with database.read_session() as reader:
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


@pytest.mark.parametrize("change", ["event", "audit", "redaction"])
async def test_prepared_forget_rejects_actual_new_sources_before_dml(database, change):
    from sqlalchemy import event as sql_event
    from sqlalchemy import text

    from yuki.identity.canonical_repository import find_identity_binding
    from yuki.persistence.people_repository import PrivacyPreparationChanged

    repository = PeopleRepository(database)
    original, _ = await seed_event(
        EventLedgerRepository(database),
        bot_user_id="9999",
        platform_message_id="source-proof",
        scope_type=ScopeType.GROUP,
        group_id="2001",
        sender_user_id="1002",
        direction="inbound",
        content="mention 1001",
        segments=({"type": "at", "data": {"qq": "1001"}},),
    )
    async with database.read_session() as reader:
        await reader.execute(text("BEGIN"))
        person = (await find_identity_binding(reader, "1001")).person_id
        prepared = await repository.prepare_person_forget(person, session=reader)
    if change == "event":
        await seed_event(
            EventLedgerRepository(database),
            bot_user_id="9999",
            platform_message_id="new-source",
            scope_type=ScopeType.GROUP,
            group_id="2001",
            sender_user_id="1002",
            direction="inbound",
            content="new mention 1001",
            segments=({"type": "at", "data": {"qq": "1001"}},),
        )
    elif change == "audit":
        async with database.immediate_session() as writer:
            writer.add(
                AdminOperationEventModel(
                    actor_user_id="1002",
                    capability="test",
                    operation="test",
                    target_type="test",
                    before_json='{"owner":"1001"}',
                    after_json="null",
                    success=True,
                    duration_seconds=0,
                    created_at=datetime.now(UTC),
                )
            )
    else:
        async with database.immediate_session() as writer:
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == original.id)
                .values(content="a newer actual mention of 1001")
            )
    writes = []

    def inspect(_connection, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    sql_event.listen(database.engine.sync_engine, "before_cursor_execute", inspect)
    try:
        with pytest.raises(PrivacyPreparationChanged):
            async with database.immediate_session() as writer:
                await repository.apply_person_forget(writer, prepared)
    finally:
        sql_event.remove(database.engine.sync_engine, "before_cursor_execute", inspect)
    assert not writes
    async with database.read_session() as reader:
        assert await reader.get(CanonicalPersonModel, person) is not None
