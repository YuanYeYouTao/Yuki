"""Adversarial complete-v2 reflection history and fact-owner shape tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from tests.conftest import build_harness, make_settings
from tests.unit.test_complete_v2_processor_runtime import (
    IngressSender,
    _Bot,
    _flip_v2,
    _inbound,
    _person_id_for,
    _space_id_for,
    _wire_ingress,
)

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.dual_write import ensure_canonical_presence_preconfig as ensure_v2_presence
from qq_ai_bot.identity.dual_write import ensure_v2_space
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.identity.memory_guard import refuse_legacy_live_fact
from qq_ai_bot.memory.partition import MemoryPartitionResolutionError
from qq_ai_bot.memory.self_reflection.models import SelfReflectionBatch, SelfReflectionState
from qq_ai_bot.memory.self_reflection.repository import SelfReflectionRepository
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemoryFactModel,
    MemorySelfReflectionRuntimeModel,
    MemorySelfReflectionStateModel,
    MemoryToolReceiptModel,
)
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.persistence.repository_records import EventRecord

_NOW = datetime(2026, 8, 24, tzinfo=UTC)
_CLAIM = {
    "scheduled_slot": "2026-08-24:04",
    "local_date": "2026-08-24",
    "event_threshold": 1,
    "character_threshold": 1,
    "max_wait_seconds": 1,
    "max_sessions": 5,
    "max_daily_calls": 9,
    "max_events": 40,
    "max_characters": 8000,
    "force": True,
}


async def _seed_reflection_runtime(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        session.add(
            MemorySelfReflectionRuntimeModel(id=1, last_scanned_event_id=0, updated_at=_NOW)
        )


async def _wire_v2(database: Database, *, group: bool = False):
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings)
    registry, _router = _wire_ingress(harness, database)
    await _flip_v2(database)
    await _seed_reflection_runtime(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
        if group:
            await ensure_v2_space(session, "2001")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    return harness, bot


def _fact_row(**overrides: object) -> MemoryFactModel:
    values: dict[str, object] = {
        "scope_type": "person",
        "subject_user_id": "1001",
        "kind": "fact",
        "memory_key": f"k-{uuid4()}",
        "category": "test",
        "content": "x",
        "normalized_content": "x",
        "importance": 3,
        "confidence": 1.0,
        "source_type": "explicit",
        "authority": "self_report",
        "status": "active",
        "conflict_state": "clear",
        "created_at": _NOW,
        "updated_at": _NOW,
        "last_confirmed_at": _NOW,
        "review_state": "verified",
    }
    values.update(overrides)
    return MemoryFactModel(**values)


@pytest.mark.asyncio
async def test_private_reflection_excludes_same_person_group_events(
    database: Database,
) -> None:
    harness, bot = await _wire_v2(database, group=True)
    sender = IngressSender(bot)
    assert (
        await harness.processor.handle(_inbound(message_id="ref-priv-1", text="私聊一句"), sender)
    ).handled
    assert (
        await harness.processor.handle(
            _inbound(
                message_id="ref-grp-1",
                text="群里一句",
                group_id="2001",
                mentions_bot=True,
            ),
            sender,
        )
    ).handled
    scanned = await SelfReflectionRepository(database).scan_new_events()
    assert scanned >= 1
    person_id = await _person_id_for(database, "1001")
    space_id = await _space_id_for(database, "2001")
    async with database.sessions() as session:
        states = list(await session.scalars(select(MemorySelfReflectionStateModel)))
        private = next(row for row in states if row.canonical_person_id == person_id)
        space = next(row for row in states if row.canonical_space_id == space_id)
        private_ids = {
            row.id
            for row in await session.scalars(
                select(ChatEventModel).where(ChatEventModel.scope_type == "private")
            )
        }
        group_ids = {
            row.id
            for row in await session.scalars(
                select(ChatEventModel).where(ChatEventModel.scope_type == "group")
            )
        }
    assert private.pending_events >= 1
    assert space.pending_events >= 1
    assert private.latest_event_id in private_ids
    assert private.latest_event_id not in group_ids
    batches = await SelfReflectionRepository(database).claim_due(**_CLAIM)
    private_batch = next(item for item in batches if item.state.id == private.id)
    claimed_ids = {event.id for event in (*private_batch.events, *private_batch.context_events)}
    assert claimed_ids <= private_ids
    assert claimed_ids.isdisjoint(group_ids)


@pytest.mark.asyncio
async def test_disabled_binding_still_retrieves_canonical_private_history(
    database: Database,
) -> None:
    harness, bot = await _wire_v2(database)
    sender = IngressSender(bot)
    assert (
        await harness.processor.handle(_inbound(message_id="ref-old-1", text="旧号一句"), sender)
    ).handled
    person_id = await _person_id_for(database, "1001")
    await SelfReflectionRepository(database).scan_new_events()
    async with database.sessions() as session, session.begin():
        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.external_account_id == "1001")
        )
        assert binding is not None
        binding.status = "disabled"
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=person_id,
                platform=IDENTITY_PLATFORM,
                external_account_id="1002",
                display_name="",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    first = await SelfReflectionRepository(database).claim_due(**_CLAIM)
    assert first
    old_ids = {event.id for event in first[0].events}
    assert old_ids
    assert (
        await harness.processor.handle(
            _inbound(message_id="ref-new-1", user_id="1002", text="新号一句"),
            sender,
        )
    ).handled
    await SelfReflectionRepository(database).scan_new_events()
    second = await SelfReflectionRepository(database).claim_due(
        **{**_CLAIM, "scheduled_slot": "2026-08-24:12"}
    )
    assert second
    combined = {event.id for event in second[0].events} | {
        event.id for event in second[0].context_events
    }
    assert old_ids <= combined
    async with database.sessions() as session:
        states = list(await session.scalars(select(MemorySelfReflectionStateModel)))
    assert [row.canonical_person_id for row in states if row.canonical_person_id] == [person_id]


@pytest.mark.asyncio
async def test_disabled_space_binding_still_retrieves_canonical_space_history(
    database: Database,
) -> None:
    harness, bot = await _wire_v2(database, group=True)
    sender = IngressSender(bot)
    assert (
        await harness.processor.handle(
            _inbound(
                message_id="ref-space-1",
                text="旧群一句",
                group_id="2001",
                mentions_bot=True,
            ),
            sender,
        )
    ).handled
    space_id = await _space_id_for(database, "2001")
    await SelfReflectionRepository(database).scan_new_events()
    async with database.sessions() as session, session.begin():
        binding = await session.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.external_space_id == "2001")
        )
        assert binding is not None
        binding.status = "disabled"
        session.add(
            SpaceBindingModel(
                id=str(uuid4()),
                space_id=space_id,
                platform=IDENTITY_PLATFORM,
                external_space_id="2002",
                display_name="",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    first = await SelfReflectionRepository(database).claim_due(**_CLAIM)
    assert first
    old_ids = {event.id for event in first[0].events}
    assert old_ids
    assert (
        await harness.processor.handle(
            _inbound(
                message_id="ref-space-2",
                text="新群一句",
                group_id="2002",
                mentions_bot=True,
            ),
            sender,
        )
    ).handled
    await SelfReflectionRepository(database).scan_new_events()
    second = await SelfReflectionRepository(database).claim_due(
        **{**_CLAIM, "scheduled_slot": "2026-08-24:12"}
    )
    assert second
    combined = {event.id for event in second[0].events} | {
        event.id for event in second[0].context_events
    }
    assert old_ids <= combined
    async with database.sessions() as session:
        spaces = [
            row.canonical_space_id
            for row in await session.scalars(select(MemorySelfReflectionStateModel))
        ]
    assert spaces == [space_id]


@pytest.mark.asyncio
async def test_space_reflection_excludes_other_space_events(
    database: Database,
) -> None:
    harness, bot = await _wire_v2(database, group=True)
    sender = IngressSender(bot)
    async with database.sessions() as session, session.begin():
        await ensure_v2_space(session, "2003")
    assert (
        await harness.processor.handle(
            _inbound(
                message_id="ref-space-a",
                text="本群一句",
                group_id="2001",
                mentions_bot=True,
            ),
            sender,
        )
    ).handled
    assert (
        await harness.processor.handle(
            _inbound(
                message_id="ref-space-b",
                text="它群一句",
                group_id="2003",
                mentions_bot=True,
            ),
            sender,
        )
    ).handled
    await SelfReflectionRepository(database).scan_new_events()
    space_id = await _space_id_for(database, "2001")
    other_space_id = await _space_id_for(database, "2003")
    async with database.sessions() as session:
        states = list(await session.scalars(select(MemorySelfReflectionStateModel)))
        own = next(row for row in states if row.canonical_space_id == space_id)
        other = next(row for row in states if row.canonical_space_id == other_space_id)
        own_ids = {
            row.id
            for row in await session.scalars(
                select(ChatEventModel).where(ChatEventModel.group_id == "2001")
            )
        }
        other_ids = {
            row.id
            for row in await session.scalars(
                select(ChatEventModel).where(ChatEventModel.group_id == "2003")
            )
        }
    batches = await SelfReflectionRepository(database).claim_due(**_CLAIM)
    own_batch = next(item for item in batches if item.state.id == own.id)
    other_batch = next(item for item in batches if item.state.id == other.id)
    own_claimed = {event.id for event in (*own_batch.events, *own_batch.context_events)}
    other_claimed = {event.id for event in (*other_batch.events, *other_batch.context_events)}
    assert own_claimed <= own_ids
    assert own_claimed.isdisjoint(other_ids)
    assert other_claimed <= other_ids
    assert other_claimed.isdisjoint(own_ids)


@pytest.mark.asyncio
async def test_v2_does_not_claim_null_owner_state_or_fallback_receipts(
    database: Database,
) -> None:
    harness, bot = await _wire_v2(database)
    sender = IngressSender(bot)
    assert (
        await harness.processor.handle(_inbound(message_id="ref-live-1", text="活路径"), sender)
    ).handled
    repo = SelfReflectionRepository(database)
    await repo.scan_new_events()
    person_id = await _person_id_for(database, "1001")
    async with database.sessions() as session, session.begin():
        live = await session.scalar(
            select(MemorySelfReflectionStateModel).where(
                MemorySelfReflectionStateModel.canonical_person_id == person_id
            )
        )
        assert live is not None
        session.add(
            MemorySelfReflectionStateModel(
                conversation_key_hash="a" * 64,
                bot_user_id=live.bot_user_id,
                canonical_person_id=None,
                canonical_space_id=None,
                scope_type="private",
                private_peer_user_id="1001",
                last_event_id=0,
                latest_event_id=live.latest_event_id,
                pending_events=9,
                pending_characters=90,
                pending_since=_NOW,
                has_yuki_reply=True,
                has_tool_result=True,
                high_value_signal=False,
                updated_at=_NOW,
            )
        )
        extra_space_id = str(uuid4())
        session.add(
            CanonicalSpaceModel(
                id=extra_space_id,
                name="",
                enabled=True,
                autonomous_enabled=True,
                require_mention=True,
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        await session.execute(
            text("DROP TRIGGER IF EXISTS trg_memory_self_reflection_states_memory_owner_insert")
        )
        session.add(
            MemorySelfReflectionStateModel(
                conversation_key_hash="b" * 64,
                bot_user_id=live.bot_user_id,
                canonical_person_id=person_id,
                canonical_space_id=extra_space_id,
                scope_type="private",
                private_peer_user_id="1001",
                last_event_id=0,
                latest_event_id=live.latest_event_id,
                pending_events=9,
                pending_characters=90,
                pending_since=_NOW,
                has_yuki_reply=True,
                has_tool_result=True,
                high_value_signal=False,
                updated_at=_NOW,
            )
        )
        event = await session.scalar(select(ChatEventModel).order_by(ChatEventModel.id.asc()))
        assert event is not None
        session.add(
            MemoryToolReceiptModel(
                conversation_key_hash=live.conversation_key_hash,
                trigger_event_id=event.id,
                bot_user_id=live.bot_user_id,
                canonical_person_id=None,
                canonical_space_id=None,
                provider_id="test",
                tool_name="legacy",
                success=True,
                result_excerpt="legacy",
                result_characters=6,
                created_at=_NOW,
                expires_at=_NOW + timedelta(days=30),
            )
        )
        session.add(
            MemoryToolReceiptModel(
                conversation_key_hash="other",
                trigger_event_id=event.id,
                bot_user_id="8001",
                canonical_person_id=person_id,
                canonical_space_id=None,
                provider_id="test",
                tool_name="owned",
                success=True,
                result_excerpt="owned",
                result_characters=5,
                created_at=_NOW,
                expires_at=_NOW + timedelta(days=30),
            )
        )
        live_id = live.id
        event_id = event.id
    batches = await repo.claim_due(**_CLAIM)
    assert batches
    assert {item.state.id for item in batches} == {live_id}
    receipts = await repo.tool_receipts(batches[0])
    assert [row.tool_name for row in receipts] == ["owned"]
    missing = SelfReflectionBatch(
        state=SelfReflectionState(
            id=live_id + 999,
            conversation_key_hash=batches[0].state.conversation_key_hash,
            bot_user_id=batches[0].state.bot_user_id,
            scope_type=ScopeType.PRIVATE,
            group_id=None,
            private_peer_user_id="1001",
            last_event_id=0,
            latest_event_id=event_id,
            pending_events=1,
            pending_characters=1,
            pending_since=_NOW,
            has_yuki_reply=True,
            has_tool_result=False,
            high_value_signal=False,
        ),
        events=(
            EventRecord(
                id=event_id,
                bot_user_id="8000",
                platform_message_id="ref-live-1",
                scope_type=ScopeType.PRIVATE,
                sender_user_id="1001",
                direction="inbound",
                content="活路径",
                visual_summary="",
                segments=(),
                occurred_at=_NOW,
            ),
        ),
        context_events=(),
        trigger_reason="manual",
        scheduled_slot="2026-08-24:04",
        run_id=1,
        max_input_characters=8000,
    )
    with pytest.raises(MemoryPartitionResolutionError) as exc:
        await repo.tool_receipts(missing)
    assert exc.value.reason == "missing_owner"
    async with database.sessions() as session, session.begin():
        live_row = await session.get(MemorySelfReflectionStateModel, live_id)
        assert live_row is not None
        live_row.canonical_person_id = None
        live_row.canonical_space_id = None
    with pytest.raises(MemoryPartitionResolutionError) as illegal:
        await repo.tool_receipts(batches[0])
    assert illegal.value.reason == "owner_shape"


@pytest.mark.asyncio
async def test_v1_scan_still_counts_duplicate_suppressed_unknown(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    await ledger.append(
        bot_user_id="8000",
        platform_message_id="v1-keep",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        direction="inbound",
        content="v1 keeper",
        private_peer_user_id="1001",
    )
    async with database.sessions() as session, session.begin():
        for status, message_id in (
            ("duplicate", "v1-dup"),
            ("suppressed", "v1-sup"),
            ("unknown", "v1-unk"),
        ):
            session.add(
                ChatEventModel(
                    bot_user_id="8000",
                    platform_message_id=message_id,
                    scope_type="private",
                    private_peer_user_id="1001",
                    sender_user_id="1001",
                    direction="inbound",
                    event_kind="message",
                    content=status,
                    visual_summary="",
                    segments_json="[]",
                    origin="user_message",
                    occurred_at=_NOW,
                    observed_at=_NOW,
                    suppression_status=status,
                )
            )
    assert await repository.scan_new_events() == 4
    async with database.sessions() as session:
        v1_state = await session.scalar(select(MemorySelfReflectionStateModel))
    assert v1_state is not None
    assert v1_state.pending_events == 4
    async with database.sessions() as session:
        last_id = int(
            await session.scalar(select(ChatEventModel.id).order_by(ChatEventModel.id.desc()))
        )
    assert v1_state.latest_event_id == last_id
    async with database.sessions() as session:
        runtime = await session.get(MemorySelfReflectionRuntimeModel, 1)
    assert runtime is not None
    assert runtime.last_scanned_event_id == last_id


@pytest.mark.asyncio
async def test_v2_scan_does_not_advance_pending_for_non_keeper(
    database: Database,
) -> None:
    harness, bot = await _wire_v2(database)
    sender = IngressSender(bot)
    repo = SelfReflectionRepository(database)
    assert (
        await harness.processor.handle(_inbound(message_id="v2-keep", text="v2 keeper"), sender)
    ).handled
    await repo.scan_new_events()
    person_id = await _person_id_for(database, "1001")
    async with database.sessions() as session:
        before = await session.scalar(
            select(MemorySelfReflectionStateModel).where(
                MemorySelfReflectionStateModel.canonical_person_id == person_id
            )
        )
        live = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.platform_message_id == "v2-keep")
        )
    assert before is not None
    assert live is not None
    conversation_id = live.canonical_conversation_id
    pending_before = before.pending_events
    yuki_before = before.has_yuki_reply
    async with database.sessions() as session, session.begin():
        conversation = await session.get(CanonicalConversationModel, conversation_id)
        assert conversation is not None
        session.add(
            ChatEventModel(
                bot_user_id="8000",
                platform_message_id="v2-null",
                scope_type="private",
                private_peer_user_id="1001",
                sender_user_id="1001",
                direction="inbound",
                event_kind="message",
                content="legacy-null live",
                visual_summary="",
                segments_json="[]",
                origin="user_message",
                occurred_at=_NOW,
                observed_at=_NOW,
                canonical_event_id=str(uuid4()),
                canonical_conversation_id=conversation_id,
                author_kind="person",
                author_person_id=live.author_person_id,
                suppression_status=None,
            )
        )
        for status, message_id, direction, content in (
            ("duplicate", "v2-dup", "inbound", "duplicate"),
            ("suppressed", "v2-sup", "inbound", "suppressed"),
            ("unknown", "v2-unk", "outbound", "yuki-looking"),
        ):
            session.add(
                ChatEventModel(
                    bot_user_id="8000",
                    platform_message_id=message_id,
                    scope_type="private",
                    private_peer_user_id="1001",
                    sender_user_id="8000",
                    direction=direction,
                    event_kind="message",
                    content=content,
                    visual_summary="",
                    segments_json="[]",
                    origin="user_message",
                    occurred_at=_NOW,
                    observed_at=_NOW,
                    canonical_event_id=str(uuid4()),
                    canonical_conversation_id=conversation_id,
                    author_kind="yuki" if direction == "outbound" else "person",
                    author_person_id=None if direction == "outbound" else live.author_person_id,
                    suppression_status=status,
                )
            )
        conversation.last_event_id = max(conversation.last_event_id, live.id + 20)
        conversation.covered_through_event_id = conversation.last_event_id
    await repo.scan_new_events()
    async with database.sessions() as session:
        v2_state = await session.scalar(
            select(MemorySelfReflectionStateModel).where(
                MemorySelfReflectionStateModel.canonical_person_id == person_id
            )
        )
        null_live = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.platform_message_id == "v2-null")
        )
        non_keepers = list(
            await session.scalars(
                select(ChatEventModel).where(
                    ChatEventModel.platform_message_id.in_(("v2-dup", "v2-sup", "v2-unk"))
                )
            )
        )
        runtime = await session.get(MemorySelfReflectionRuntimeModel, 1)
        last_event_id = int(
            await session.scalar(select(ChatEventModel.id).order_by(ChatEventModel.id.desc()))
        )
    assert v2_state is not None
    assert null_live is not None
    assert non_keepers
    assert v2_state.pending_events == pending_before + 1
    assert v2_state.latest_event_id == null_live.id
    assert v2_state.has_yuki_reply == yuki_before
    assert v2_state.latest_event_id < max(row.id for row in non_keepers)
    assert runtime is not None
    assert runtime.last_scanned_event_id == last_event_id
    assert runtime.last_scanned_event_id >= max(row.id for row in non_keepers)


@pytest.mark.asyncio
async def test_fact_owner_shape_is_strict_and_evidence_cannot_substitute(
    database: Database,
) -> None:
    await _flip_v2(database)
    person_id = str(uuid4())
    space_id = str(uuid4())
    async with database.sessions() as session, session.begin():
        session.add(
            CanonicalPersonModel(
                id=person_id, enabled=True, revision=1, created_at=_NOW, updated_at=_NOW
            )
        )
        session.add(
            CanonicalSpaceModel(
                id=space_id,
                name="",
                enabled=True,
                autonomous_enabled=True,
                require_mention=True,
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        event = ChatEventModel(
            bot_user_id="8000",
            platform_message_id="shape-ev",
            scope_type="private",
            private_peer_user_id="1001",
            sender_user_id="1001",
            direction="inbound",
            event_kind="message",
            content="evidence",
            visual_summary="",
            segments_json="[]",
            origin="user_message",
            occurred_at=_NOW,
            observed_at=_NOW,
            canonical_event_id=str(uuid4()),
            suppression_status="keeper",
        )
        session.add(event)
        await session.flush()
        missing = {
            "person": _fact_row(scope_type="person", subject_user_id="1001"),
            "group": _fact_row(
                scope_type="group",
                subject_user_id=None,
                group_id="2001",
                memory_key="g-missing",
            ),
            "person_group": _fact_row(
                scope_type="person_group",
                subject_user_id="1001",
                group_id="2001",
                memory_key="pg-missing",
            ),
        }
        legal = {
            "person": _fact_row(
                scope_type="person",
                subject_user_id="1001",
                memory_key="p-ok",
                canonical_subject_person_id=person_id,
            ),
            "group": _fact_row(
                scope_type="group",
                subject_user_id=None,
                group_id="2001",
                memory_key="g-ok",
                canonical_subject_space_id=space_id,
            ),
            "person_group": _fact_row(
                scope_type="person_group",
                subject_user_id="1001",
                group_id="2001",
                memory_key="pg-ok",
                canonical_subject_person_id=person_id,
                canonical_subject_space_id=space_id,
            ),
            "self": _fact_row(
                scope_type="self",
                subject_user_id=None,
                visibility_type="global",
                kind="preference",
                memory_key="self-ok",
            ),
            "self_private": _fact_row(
                scope_type="self",
                subject_user_id=None,
                visibility_type="private",
                visibility_user_id="1001",
                kind="preference",
                memory_key="self-priv",
                canonical_visibility_person_id=person_id,
            ),
            "self_group": _fact_row(
                scope_type="self",
                subject_user_id=None,
                visibility_type="group",
                visibility_group_id="2001",
                kind="preference",
                memory_key="self-grp",
                canonical_visibility_space_id=space_id,
            ),
        }
        mixed = {
            "person_with_space": _fact_row(
                scope_type="person",
                subject_user_id="1001",
                memory_key="p-mix",
                canonical_subject_person_id=person_id,
                canonical_subject_space_id=space_id,
            ),
            "group_with_person": _fact_row(
                scope_type="group",
                subject_user_id=None,
                group_id="2001",
                memory_key="g-mix",
                canonical_subject_person_id=person_id,
                canonical_subject_space_id=space_id,
            ),
            "self_global_with_visibility": _fact_row(
                scope_type="self",
                subject_user_id=None,
                visibility_type="global",
                kind="preference",
                memory_key="self-mix",
                canonical_visibility_person_id=person_id,
            ),
            "self_private_with_subject": _fact_row(
                scope_type="self",
                subject_user_id=None,
                visibility_type="private",
                visibility_user_id="1001",
                kind="preference",
                memory_key="self-priv-mix",
                canonical_subject_person_id=person_id,
                canonical_visibility_person_id=person_id,
            ),
        }
        for row in (*missing.values(), *legal.values()):
            session.add(row)
        await session.flush()
        await session.execute(
            text("DROP TRIGGER IF EXISTS trg_memory_facts_ownership_shadow_insert")
        )
        for row in mixed.values():
            session.add(row)
        await session.flush()
        for row in missing.values():
            session.add(
                MemoryEvidenceModel(
                    fact_id=row.id,
                    event_id=event.id,
                    source_speaker_user_id="1001",
                    relation="self_statement",
                    confidence=1.0,
                    authority="self_report",
                    excerpt="evidence",
                    created_at=_NOW,
                )
            )
        missing_ids = {name: row.id for name, row in missing.items()}
        legal_ids = {name: row.id for name, row in legal.items()}
        mixed_ids = {name: row.id for name, row in mixed.items()}
    async with database.sessions() as session:
        for name, fact_id in missing_ids.items():
            assert await refuse_legacy_live_fact(session, fact_id) is True, name
        for name, fact_id in legal_ids.items():
            assert await refuse_legacy_live_fact(session, fact_id) is False, name
        for name, fact_id in mixed_ids.items():
            assert await refuse_legacy_live_fact(session, fact_id) is True, name


@pytest.mark.asyncio
async def test_v2_health_snapshot_counts_only_xor_owner_states(
    database: Database,
) -> None:
    harness, bot = await _wire_v2(database)
    sender = IngressSender(bot)
    assert (
        await harness.processor.handle(
            _inbound(message_id="health-live", text="健康活路径"), sender
        )
    ).handled
    repo = SelfReflectionRepository(database)
    await repo.scan_new_events()
    async with database.sessions() as session, session.begin():
        session.add(
            MemorySelfReflectionStateModel(
                conversation_key_hash="c" * 64,
                bot_user_id="8000",
                canonical_person_id=None,
                canonical_space_id=None,
                scope_type="private",
                private_peer_user_id="1999",
                last_event_id=0,
                latest_event_id=1,
                pending_events=4,
                pending_characters=8,
                pending_since=_NOW,
                has_yuki_reply=True,
                has_tool_result=False,
                high_value_signal=False,
                updated_at=_NOW,
            )
        )
    pending, _calls, _status, _completed = await repo.health_snapshot(local_date="2026-08-24")
    async with database.sessions() as session:
        xor_pending = int(
            await session.scalar(
                select(MemorySelfReflectionStateModel.id).where(
                    MemorySelfReflectionStateModel.pending_events > 0,
                    MemorySelfReflectionStateModel.canonical_person_id.is_not(None),
                    MemorySelfReflectionStateModel.canonical_space_id.is_(None),
                )
            )
            or 0
        )
        all_pending = list(
            await session.scalars(
                select(MemorySelfReflectionStateModel).where(
                    MemorySelfReflectionStateModel.pending_events > 0
                )
            )
        )
    assert len(all_pending) >= 2
    assert pending == 1
    assert xor_pending


@pytest.mark.asyncio
async def test_v1_health_snapshot_still_counts_legacy_states(database: Database) -> None:
    repo = SelfReflectionRepository(database)
    async with database.sessions() as session, session.begin():
        session.add(
            MemorySelfReflectionStateModel(
                conversation_key_hash="d" * 64,
                bot_user_id="8000",
                canonical_person_id=None,
                canonical_space_id=None,
                scope_type="private",
                private_peer_user_id="1001",
                last_event_id=0,
                latest_event_id=1,
                pending_events=2,
                pending_characters=4,
                pending_since=_NOW,
                has_yuki_reply=True,
                has_tool_result=False,
                high_value_signal=False,
                updated_at=_NOW,
            )
        )
        session.add(
            MemorySelfReflectionStateModel(
                conversation_key_hash="e" * 64,
                bot_user_id="8000",
                canonical_person_id=None,
                canonical_space_id=None,
                scope_type="group",
                group_id="2001",
                last_event_id=0,
                latest_event_id=2,
                pending_events=3,
                pending_characters=6,
                pending_since=_NOW,
                has_yuki_reply=True,
                has_tool_result=False,
                high_value_signal=False,
                updated_at=_NOW,
            )
        )
    pending, _calls, _status, _completed = await repo.health_snapshot(local_date="2026-08-24")
    assert pending == 2
