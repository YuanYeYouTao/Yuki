"""Trusted subject metadata must remain valid until bounded receipt writes."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update
from tests.support.event_seed import seed_event
from tests.unit.test_rebuild_receipt_finalization import _seed

from yuki.domain.conversations import ScopeType
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel
from yuki.memory.extraction import source_event_fingerprint
from yuki.memory.rebuild.models import MemoryRebuildSelection
from yuki.memory.rebuild.repository import MemoryRebuildRepository
from yuki.persistence.database import Database
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.persistence.models import (
    MemoryJobModel,
    MemoryRebuildItemModel,
    MemoryRebuildRunModel,
)


async def _trusted_page(database: Database, *, target_active: bool = True):
    seed = await _seed(database, 2)
    ledger = EventLedgerRepository(database)
    await seed_event(
        ledger,
        bot_user_id="8000",
        platform_message_id=str(uuid.uuid4()),
        scope_type=ScopeType.PRIVATE,
        sender_user_id="3003",
        private_peer_user_id="3003",
        direction="inbound",
        content="引用目标已登记",
    )
    mentioned, _ = await seed_event(
        ledger,
        bot_user_id="8000",
        platform_message_id=str(uuid.uuid4()),
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        group_id="2001",
        content="带可信引用的历史事件",
        segments=({"type": "at", "data": {"qq": "3003"}},),
    )
    ordinary, _ = await seed_event(
        ledger,
        bot_user_id="8000",
        platform_message_id=str(uuid.uuid4()),
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        group_id="2001",
        content="同页普通历史事件",
    )
    mentioned = await ledger.get_event(mentioned.id)
    ordinary = await ledger.get_event(ordinary.id)
    assert mentioned is not None and ordinary is not None
    if not target_active:
        async with database.immediate_session() as session:
            await session.execute(
                update(IdentityBindingModel)
                .where(IdentityBindingModel.external_account_id == "3003")
                .values(status="disabled")
            )
    hydrated = await ledger.hydrate_rebuild_subjects(mentioned)
    assert hydrated.mentioned_user_ids == ("3003",)
    selection = MemoryRebuildSelection(all_events=True, third_party_mode="trusted_metadata")
    async with database.immediate_session() as session:
        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.external_account_id == "3003")
        )
        assert binding is not None
        target_ids = binding.id, binding.person_id
        await session.execute(
            update(MemoryRebuildRunModel)
            .where(MemoryRebuildRunModel.id == seed.run_id)
            .values(selection_json=selection.model_dump_json())
        )
        for item_id, event in zip(seed.item_ids, (hydrated, ordinary), strict=True):
            await session.execute(
                update(MemoryRebuildItemModel)
                .where(MemoryRebuildItemModel.id == item_id)
                .values(event_id=event.id, source_event_hash=source_event_fingerprint(event))
            )
    return replace(seed, event_ids=(mentioned.id, ordinary.id)), target_ids


async def _assert_only_unreferenced_event_completed(database: Database, seed) -> None:
    async with database.read_session() as session:
        receipts = tuple(
            await session.scalars(
                select(MemoryJobModel.event_id).where(MemoryJobModel.rebuild_run_id == seed.run_id)
            )
        )
        assert receipts == (seed.event_ids[1],)
        bad_item = await session.get(MemoryRebuildItemModel, seed.item_ids[0])
        good_item = await session.get(MemoryRebuildItemModel, seed.item_ids[1])
        assert bad_item is not None and bad_item.status != "committed"
        assert good_item is not None and good_item.status == "committed"


@pytest.mark.asyncio
async def test_disabled_mentioned_person_does_not_block_original_receipt_completion(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, (_binding_id, person_id) = await _trusted_page(database)
    async with database.immediate_session() as session:
        original_hash = await session.scalar(
            select(MemoryRebuildItemModel.source_event_hash).where(
                MemoryRebuildItemModel.id == seed.item_ids[0]
            )
        )
        await session.execute(
            update(CanonicalPersonModel)
            .where(CanonicalPersonModel.id == person_id)
            .values(enabled=False, revision=CanonicalPersonModel.revision + 1)
        )
    assert (
        await MemoryRebuildRepository(database).complete_item_receipts(
            seed.public_id, include_failed_live_jobs=False
        )
        == 2
    )
    async with database.read_session() as session:
        assert set(
            await session.scalars(
                select(MemoryJobModel.event_id).where(MemoryJobModel.rebuild_run_id == seed.run_id)
            )
        ) == set(seed.event_ids)
        assert (
            await session.scalar(
                select(MemoryRebuildItemModel.source_event_hash).where(
                    MemoryRebuildItemModel.id == seed.item_ids[0]
                )
            )
            == original_hash
        )
        selection_json = await session.scalar(
            select(MemoryRebuildRunModel.selection_json).where(
                MemoryRebuildRunModel.id == seed.run_id
            )
        )
    assert selection_json is not None
    assert MemoryRebuildSelection.model_validate_json(selection_json).third_party_mode.value == (
        "trusted_metadata"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["person", "binding", "rename", "merge", "transfer", "forget"])
async def test_original_reference_survives_current_changes_but_not_real_erasure(
    database: Database, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    seed, (binding_id, person_id) = await _trusted_page(database)
    original = EventLedgerRepository.hydrate_rebuild_subjects
    injected = False

    async def hydrate_and_change(self, event):
        nonlocal injected
        hydrated = await original(self, event)
        if event.id == seed.event_ids[0] and not injected:
            injected = True
            assert hydrated.mentioned_user_ids == ("3003",)
            if change == "transfer":
                from tests.support.memory_identity import transfer_account

                await transfer_account(database, "3003")
            elif change == "forget":
                from yuki.persistence.people_repository import PeopleRepository

                assert await PeopleRepository(database).delete_person_id(person_id)
            else:
                async with database.immediate_session() as writer:
                    if change == "person":
                        await writer.execute(
                            update(CanonicalPersonModel)
                            .where(CanonicalPersonModel.id == person_id)
                            .values(enabled=False, revision=CanonicalPersonModel.revision + 1)
                        )
                    elif change == "binding":
                        await writer.execute(
                            update(IdentityBindingModel)
                            .where(IdentityBindingModel.id == binding_id)
                            .values(status="disabled", revision=IdentityBindingModel.revision + 1)
                        )
                    elif change == "rename":
                        from yuki.identity.person_identity import rename_person

                        await rename_person(
                            writer, person_id, "新的当前名字", now=datetime.now(UTC)
                        )
                    else:
                        from tests.support.person_merge import merge_persons

                        from yuki.identity.canonical_repository import find_identity_binding

                        target = (await find_identity_binding(writer, "1002")).person_id
                        await merge_persons(writer, person_id, target)
        return hydrated

    monkeypatch.setattr(EventLedgerRepository, "hydrate_rebuild_subjects", hydrate_and_change)
    assert await MemoryRebuildRepository(database).complete_item_receipts(
        seed.public_id, include_failed_live_jobs=False
    ) == (0 if change == "forget" else 2)
    assert injected
    if change == "forget":
        # Erasure also advances the actual group source generation; prepare the
        # page again before completing its unaffected item.
        assert (
            await MemoryRebuildRepository(database).complete_item_receipts(
                seed.public_id, include_failed_live_jobs=False
            )
            == 1
        )
        await _assert_only_unreferenced_event_completed(database, seed)
    else:
        async with database.read_session() as session:
            assert set(
                await session.scalars(
                    select(MemoryJobModel.event_id).where(
                        MemoryJobModel.rebuild_run_id == seed.run_id
                    )
                )
            ) == set(seed.event_ids)


@pytest.mark.asyncio
async def test_original_historical_reference_not_reclassified_when_account_reactivates(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, (binding_id, _person_id) = await _trusted_page(database, target_active=False)
    original = EventLedgerRepository.hydrate_rebuild_subjects
    injected = False

    async def hydrate_and_activate(self, event):
        nonlocal injected
        hydrated = await original(self, event)
        if event.id == seed.event_ids[0] and not injected:
            injected = True
            assert hydrated.mentioned_user_ids == ("3003",)
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(IdentityBindingModel)
                    .where(IdentityBindingModel.id == binding_id)
                    .values(status="active", revision=IdentityBindingModel.revision + 1)
                )
        return hydrated

    monkeypatch.setattr(EventLedgerRepository, "hydrate_rebuild_subjects", hydrate_and_activate)
    assert (
        await MemoryRebuildRepository(database).complete_item_receipts(
            seed.public_id, include_failed_live_jobs=False
        )
        == 2
    )
    assert injected
    async with database.read_session() as session:
        assert set(
            await session.scalars(
                select(MemoryJobModel.event_id).where(MemoryJobModel.rebuild_run_id == seed.run_id)
            )
        ) == set(seed.event_ids)
