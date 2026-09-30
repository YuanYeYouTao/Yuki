"""Trusted subject metadata must remain valid until bounded receipt writes."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select, update
from tests.unit.test_rebuild_receipt_finalization import _seed

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.identity.db_models import CanonicalPersonModel, IdentityBindingModel, PresenceModel
from qq_ai_bot.memory.extraction import source_event_fingerprint
from qq_ai_bot.memory.rebuild import receipt_finalization
from qq_ai_bot.memory.rebuild.models import MemoryRebuildSelection
from qq_ai_bot.memory.rebuild.repository import MemoryRebuildRepository
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import (
    MemoryJobModel,
    MemoryRebuildItemModel,
    MemoryRebuildRunModel,
)


async def _trusted_page(database: Database, *, target_active: bool = True):
    seed = await _seed(database, 2)
    ledger = EventLedgerRepository(database)
    await ledger.append(
        bot_user_id="8000",
        platform_message_id=str(uuid.uuid4()),
        scope_type=ScopeType.PRIVATE,
        sender_user_id="3003",
        private_peer_user_id="3003",
        direction="inbound",
        content="引用目标已登记",
    )
    mentioned, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id=str(uuid.uuid4()),
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        group_id="2001",
        content="带可信引用的历史事件",
        segments=({"type": "at", "data": {"qq": "3003"}},),
    )
    ordinary, _ = await ledger.append(
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
        async with database.sessions() as session, session.begin():
            await session.execute(
                update(IdentityBindingModel)
                .where(IdentityBindingModel.external_account_id == "3003")
                .values(status="disabled")
            )
    hydrated = await ledger.hydrate_rebuild_subjects(mentioned)
    assert hydrated.mentioned_user_ids == (("3003",) if target_active else ())
    selection = MemoryRebuildSelection(all_events=True, third_party_mode="trusted_metadata")
    async with database.sessions() as session, session.begin():
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
    async with database.sessions() as session:
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
async def test_disabled_trusted_mention_does_not_abort_other_items_in_receipt_page(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, (_binding_id, person_id) = await _trusted_page(database)
    async with database.sessions() as session, session.begin():
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
    original_fingerprint = receipt_finalization.source_event_fingerprint

    def fingerprint_without_fallback(event):
        # Failed trusted hydration must not clear references and compare a
        # disabled-mode fingerprint as if it were the reviewed source.
        assert event.id != seed.event_ids[0]
        return original_fingerprint(event)

    monkeypatch.setattr(
        receipt_finalization, "source_event_fingerprint", fingerprint_without_fallback
    )
    assert (
        await MemoryRebuildRepository(database).complete_item_receipts(
            seed.public_id, include_failed_live_jobs=False
        )
        == 1
    )
    await _assert_only_unreferenced_event_completed(database, seed)
    async with database.sessions() as session:
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
@pytest.mark.parametrize("change", ["person", "binding", "presence"])
async def test_trusted_reference_change_after_hydration_rejects_old_receipt(
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
                else:
                    await writer.execute(
                        delete(IdentityBindingModel).where(IdentityBindingModel.id == binding_id)
                    )
                    now = datetime.now(UTC)
                    writer.add(
                        PresenceModel(
                            id=str(uuid.uuid4()),
                            platform="qq",
                            external_account_id="3003",
                            enabled=True,
                            ingest_eligible=True,
                            revision=1,
                            created_at=now,
                            updated_at=now,
                        )
                    )
        return hydrated

    monkeypatch.setattr(EventLedgerRepository, "hydrate_rebuild_subjects", hydrate_and_change)
    assert (
        await MemoryRebuildRepository(database).complete_item_receipts(
            seed.public_id, include_failed_live_jobs=False
        )
        == 1
    )
    assert injected
    await _assert_only_unreferenced_event_completed(database, seed)


@pytest.mark.asyncio
async def test_filtered_raw_reference_becoming_active_requires_fresh_preparation(
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
            assert hydrated.mentioned_user_ids == ()
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
        == 1
    )
    assert injected
    await _assert_only_unreferenced_event_completed(database, seed)
