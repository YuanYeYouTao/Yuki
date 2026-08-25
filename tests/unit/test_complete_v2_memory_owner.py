"""complete-v2 Memory owner: jobs, facts, SELF, reflection, dream, MCP, cutover."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from tests.conftest import build_harness, make_settings
from tests.unit.test_complete_v2_processor_runtime import (
    IngressSender,
    _Bot,
    _carrier_counts,
    _flip_v2,
    _inbound,
    _person_id_for,
    _wire_ingress,
)
from tests.unit.test_identity_cutover import (
    _insert_memory_fact_person,
    _insert_people,
    _open,
    _seed_identity,
)
from tests.unit.test_memory_v2 import _append_event
from tests.unit.test_migration_0043 import _upgrade

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.identity.canonical_memory_schema import memory_fact_canonical_conflict_kind
from qq_ai_bot.identity.cutover_repository import IdentityCutoverRepository
from qq_ai_bot.identity.db_models import IdentityBindingModel, SpaceBindingModel
from qq_ai_bot.identity.dual_write import ensure_canonical_presence_preconfig as ensure_v2_presence
from qq_ai_bot.identity.errors import IdentityCutoverPreconditionError
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.identity.memory_guard import refuse_legacy_live_fact
from qq_ai_bot.mcp.repository import MCPRepository
from qq_ai_bot.memory.dream.repository import DreamCandidate
from qq_ai_bot.memory.enums import (
    MemoryKind,
    MemoryScopeType,
    MemorySourceType,
    MemoryStatus,
    SelfMemoryVisibility,
)
from qq_ai_bot.memory.models import MemoryFact, MemoryFactCreate, MemoryFactQuery
from qq_ai_bot.memory.partition import MemoryPartitionResolutionError
from qq_ai_bot.memory.repository import MemoryFactRepository, MemoryJobRepository
from qq_ai_bot.memory.runtime.partition_lookup import DatabaseMemoryPartitionLookup
from qq_ai_bot.memory.runtime.resolver import MemoryStructuredCommand
from qq_ai_bot.memory.runtime.turn_session import TurnMemorySession
from qq_ai_bot.memory.self_reflection.repository import SelfReflectionRepository
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    GroupModel,
    MembershipModel,
    MemoryFactModel,
    MemoryJobModel,
    MemorySelfReflectionRuntimeModel,
    MemorySelfReflectionStateModel,
    MemoryToolReceiptModel,
    PersonModel,
)
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.runtime.authority import TurnAuthority
from qq_ai_bot.runtime.origin import TurnOrigin

_NOW = datetime(2026, 8, 24, tzinfo=UTC)


def _person_fact(user_id: str, memory_key: str = "likes") -> MemoryFactCreate:
    return MemoryFactCreate(
        scope_type=MemoryScopeType.PERSON,
        subject_user_id=user_id,
        kind=MemoryKind.FACT,
        memory_key=memory_key,
        category="test",
        content="喜欢喝茶",
        importance=4,
        confidence=0.9,
        source_type=MemorySourceType.EXPLICIT,
    )


def _self_fact(
    *,
    visibility: SelfMemoryVisibility,
    visibility_user_id: str | None = None,
    visibility_group_id: str | None = None,
) -> MemoryFactCreate:
    return MemoryFactCreate(
        scope_type=MemoryScopeType.SELF,
        visibility_type=visibility,
        visibility_user_id=visibility_user_id,
        visibility_group_id=visibility_group_id,
        kind=MemoryKind.PREFERENCE,
        memory_key="self-tone",
        category="self",
        content="说话简短",
        importance=3,
        confidence=0.8,
        source_type=MemorySourceType.EXPLICIT,
    )


def _person_group_fact(user_id: str, group_id: str) -> MemoryFactCreate:
    return MemoryFactCreate(
        scope_type=MemoryScopeType.PERSON_GROUP,
        subject_user_id=user_id,
        group_id=group_id,
        kind=MemoryKind.FACT,
        memory_key="group-likes",
        category="test",
        content="在群里喝茶",
        importance=3,
        confidence=0.8,
        source_type=MemorySourceType.EXPLICIT,
    )


async def _second_binding(database: Database, external_id: str, person_id: str) -> None:
    async with database.sessions() as session, session.begin():
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=person_id,
                platform=IDENTITY_PLATFORM,
                external_account_id=external_id,
                display_name="",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )


async def _seed_reflection_runtime(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        session.add(
            MemorySelfReflectionRuntimeModel(
                id=1,
                last_scanned_event_id=0,
                updated_at=_NOW,
            )
        )


@pytest.mark.asyncio
async def test_v1_job_keys_and_claim_still_use_qq_partitions(database: Database) -> None:
    ledger = EventLedgerRepository(database)
    jobs = MemoryJobRepository(database)
    first = await _append_event(ledger, message_id="v1-job-1", group_id="2001")
    second = await _append_event(
        ledger,
        message_id="v1-job-2",
        user_id="1002",
        group_id="2001",
    )
    assert await jobs.enqueue(first.id, "group:2001")
    assert await jobs.enqueue(second.id, "group:2001")
    async with database.sessions() as session:
        rows = list(await session.scalars(select(MemoryJobModel).order_by(MemoryJobModel.id)))
        space_id = await session.scalar(
            select(SpaceBindingModel.space_id).where(SpaceBindingModel.external_space_id == "2001")
        )
    assert space_id is not None
    assert [row.conversation_key for row in rows] == ["group:2001", "group:2001"]
    assert {row.canonical_person_id for row in rows} == {None}
    assert {row.canonical_space_id for row in rows} == {space_id}
    claimed = await jobs.claim_ready_batch(
        limit=12,
        trigger_count=2,
        max_characters=8000,
        max_wait_seconds=300,
    )
    assert len(claimed) == 2
    assert {job.conversation_key for job in claimed} == {"group:2001"}


@pytest.mark.asyncio
async def test_complete_v2_two_bindings_share_job_fact_self_cursor_and_receipt(
    database: Database,
) -> None:
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings)
    registry, _router = _wire_ingress(harness, database)
    await _flip_v2(database)
    await _seed_reflection_runtime(database)
    bot_a = _Bot("8000")
    bot_b = _Bot("8001")
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_v2_presence(session, "8000")
        presence_b = await ensure_v2_presence(session, "8001")
    registry.connect(bot_a)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    first = await harness.processor.handle(
        _inbound(message_id="mem-owner-1", text="第一句"),
        IngressSender(bot_a),
    )
    assert first.handled is True
    person_id = await _person_id_for(database, "1001")
    await _second_binding(database, "1002", person_id)
    registry.connect(bot_b)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    second = await harness.processor.handle(
        _inbound(message_id="mem-owner-2", user_id="1002", bot_user_id="8001", text="第二句"),
        IngressSender(bot_b),
    )
    assert second.handled is True
    assert await _carrier_counts(database) == (0, 0, 0)
    async with database.sessions() as session:
        jobs = list(await session.scalars(select(MemoryJobModel)))
        people = int(await session.scalar(select(func.count()).select_from(PersonModel)) or 0)
        groups = int(await session.scalar(select(func.count()).select_from(GroupModel)) or 0)
        memberships = int(
            await session.scalar(select(func.count()).select_from(MembershipModel)) or 0
        )
        events = list(
            await session.scalars(
                select(ChatEventModel).where(ChatEventModel.direction == "inbound")
            )
        )
    assert people == 0
    assert groups == 0
    assert memberships == 0
    live_jobs = [row for row in jobs if row.canonical_person_id]
    assert live_jobs
    assert {row.canonical_person_id for row in live_jobs} == {person_id}
    assert {row.canonical_space_id for row in live_jobs} == {None}
    assert {row.conversation_key for row in live_jobs} == {f"person:{person_id}"}
    claimed = await MemoryJobRepository(database).claim_ready_batch(
        limit=12,
        trigger_count=2,
        max_characters=8000,
        max_wait_seconds=300,
    )
    assert {job.conversation_key for job in claimed} == {f"person:{person_id}"}
    assert len(claimed) >= 2

    facts = MemoryFactRepository(database)
    async with database.sessions() as session, session.begin():
        created = await facts.create_fact(
            _person_fact("1001"),
            normalized_content="喜欢喝茶",
            supersedes_id=None,
            session=session,
        )
        found = await facts.find_active(_person_fact("1002"), session=session)
        listed = await facts.list_facts(
            MemoryFactQuery(scope_type=MemoryScopeType.PERSON, subject_user_id="1002"),
            session=session,
        )
        self_global = await facts.create_fact(
            _self_fact(visibility=SelfMemoryVisibility.GLOBAL),
            normalized_content="说话简短",
            supersedes_id=None,
            session=session,
        )
        self_private_a = await facts.create_fact(
            _self_fact(visibility=SelfMemoryVisibility.PRIVATE, visibility_user_id="1001"),
            normalized_content="说话简短",
            supersedes_id=None,
            session=session,
        )
        self_private_b = await facts.find_active(
            _self_fact(visibility=SelfMemoryVisibility.PRIVATE, visibility_user_id="1002"),
            session=session,
        )
    assert found is not None and found.id == created.id
    assert [item.id for item in listed] == [created.id]
    assert created.canonical_subject_person_id == person_id
    assert self_global.canonical_visibility_person_id is None
    assert self_global.canonical_subject_person_id is None
    assert self_private_b is not None and self_private_b.id == self_private_a.id
    assert self_private_a.canonical_visibility_person_id == person_id
    async with database.sessions() as session:
        assert await refuse_legacy_live_fact(session, self_global.id) is False
        assert await refuse_legacy_live_fact(session, self_private_a.id) is False

    mcp = MCPRepository(database)
    inbound = {row.platform_message_id: row for row in events}
    await mcp.record_invocation(
        conversation_key="private:1001",
        provider_id="test",
        tool_name="web_search",
        success=True,
        latency_seconds=0.01,
        result_size=8,
        artifact_created=False,
        error_category=None,
        trigger_message_id="mem-owner-1",
        bot_user_id="8000",
        result_excerpt="ok",
    )
    await mcp.record_invocation(
        conversation_key="private:1002",
        provider_id="test",
        tool_name="web_search",
        success=True,
        latency_seconds=0.01,
        result_size=8,
        artifact_created=False,
        error_category=None,
        trigger_message_id="mem-owner-2",
        bot_user_id="8001",
        result_excerpt="ok",
    )
    async with database.sessions() as session:
        receipts = list(await session.scalars(select(MemoryToolReceiptModel)))
    assert inbound["mem-owner-1"].bot_user_id != inbound["mem-owner-2"].bot_user_id
    assert {row.canonical_person_id for row in receipts} == {person_id}
    assert {row.conversation_key_hash for row in receipts} == {receipts[0].conversation_key_hash}

    scanned = await SelfReflectionRepository(database).scan_new_events(limit=5000)
    assert scanned >= 1
    async with database.sessions() as session:
        states = list(await session.scalars(select(MemorySelfReflectionStateModel)))
    person_states = [row for row in states if row.canonical_person_id == person_id]
    assert len(person_states) == 1


@pytest.mark.asyncio
async def test_complete_v2_self_without_owner_is_refused(database: Database) -> None:
    await _flip_v2(database)
    facts = MemoryFactRepository(database)
    async with database.sessions() as session, session.begin():
        with pytest.raises(MemoryPartitionResolutionError):
            await facts.create_fact(
                _self_fact(visibility=SelfMemoryVisibility.PRIVATE, visibility_user_id="1999"),
                normalized_content="x",
                supersedes_id=None,
                session=session,
            )
        row = MemoryFactModel(
            scope_type="self",
            visibility_type="private",
            visibility_user_id="1999",
            kind="preference",
            memory_key="orphan-self",
            category="self",
            content="x",
            normalized_content="x",
            importance=3,
            confidence=1.0,
            source_type="explicit",
            authority="self_report",
            status="active",
            conflict_state="clear",
            created_at=_NOW,
            updated_at=_NOW,
            last_confirmed_at=_NOW,
            review_state="verified",
        )
        session.add(row)
        await session.flush()
        assert await refuse_legacy_live_fact(session, row.id) is True


@pytest.mark.asyncio
async def test_complete_v2_person_group_does_not_insert_membership(database: Database) -> None:
    from qq_ai_bot.identity.db_models import CanonicalSpaceModel, SpaceBindingModel

    await _flip_v2(database)
    person_id = str(uuid4())
    space_id = str(uuid4())
    async with database.sessions() as session, session.begin():
        from qq_ai_bot.identity.db_models import CanonicalPersonModel

        session.add(
            CanonicalPersonModel(
                id=person_id,
                enabled=True,
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=person_id,
                platform=IDENTITY_PLATFORM,
                external_account_id="1001",
                display_name="",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
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
        session.add(
            SpaceBindingModel(
                id=str(uuid4()),
                space_id=space_id,
                platform=IDENTITY_PLATFORM,
                external_space_id="2001",
                display_name="",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    facts = MemoryFactRepository(database)
    async with database.sessions() as session, session.begin():
        created = await facts.create_fact(
            _person_group_fact("1001", "2001"),
            normalized_content="在群里喝茶",
            supersedes_id=None,
            session=session,
        )
    assert created.canonical_subject_person_id == person_id
    assert created.canonical_subject_space_id == space_id
    async with database.sessions() as session:
        people = int(await session.scalar(select(func.count()).select_from(PersonModel)) or 0)
        groups = int(await session.scalar(select(func.count()).select_from(GroupModel)) or 0)
        memberships = int(
            await session.scalar(select(func.count()).select_from(MembershipModel)) or 0
        )
    assert people == 0
    assert groups == 0
    assert memberships == 0


@pytest.mark.asyncio
async def test_complete_v2_starts_after_event_does_not_enqueue(database: Database) -> None:
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings)
    registry, _router = _wire_ingress(harness, database)
    await _flip_v2(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    handled = await harness.processor.handle(
        _inbound(message_id="mem-after-1", text="活路径"),
        IngressSender(bot),
    )
    assert handled.handled is True
    jobs = MemoryJobRepository(database)
    async with database.sessions() as session, session.begin():
        event = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.platform_message_id == "mem-after-1")
        )
        assert event is not None
        conversation = await session.get(
            CanonicalConversationModel, event.canonical_conversation_id
        )
        assert conversation is not None
        conversation.starts_after_event_id = int(event.id)
        if conversation.covered_through_event_id < int(event.id):
            conversation.covered_through_event_id = int(event.id)
        if conversation.last_event_id < int(event.id):
            conversation.last_event_id = int(event.id)
        await session.execute(
            text("DELETE FROM memory_jobs WHERE event_id = :id"), {"id": event.id}
        )
        event_id = int(event.id)
    assert await jobs.enqueue(event_id, "private:1001") is False


@pytest.mark.asyncio
async def test_complete_v2_missing_owner_raises_on_enqueue_and_scan(
    database: Database,
) -> None:
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings)
    registry, _router = _wire_ingress(harness, database)
    await _flip_v2(database)
    await _seed_reflection_runtime(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    handled = await harness.processor.handle(
        _inbound(message_id="mem-ok-1", text="有主人"),
        IngressSender(bot),
    )
    assert handled.handled is True
    async with database.sessions() as session, session.begin():
        seed = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.platform_message_id == "mem-ok-1")
        )
        assert seed is not None
        session.add(
            ChatEventModel(
                bot_user_id="8000",
                platform_message_id="mem-missing",
                scope_type="private",
                private_peer_user_id="1999",
                sender_user_id="1999",
                direction="inbound",
                event_kind="message",
                content="无主人",
                visual_summary="",
                segments_json="[]",
                origin="user_message",
                occurred_at=_NOW,
                observed_at=_NOW,
                canonical_event_id=str(uuid4()),
                canonical_conversation_id=seed.canonical_conversation_id,
                author_kind=None,
                suppression_status="keeper",
            )
        )
        await session.flush()
        missing_id = int(
            await session.scalar(
                select(ChatEventModel.id).where(ChatEventModel.platform_message_id == "mem-missing")
            )
        )
    jobs = MemoryJobRepository(database)
    with pytest.raises(MemoryPartitionResolutionError) as exc:
        await jobs.enqueue(missing_id, "private:1999")
    assert exc.value.reason == "missing_owner"
    scanned = await SelfReflectionRepository(database).scan_new_events(limit=5000)
    assert scanned >= 1
    person_id = await _person_id_for(database, "1001")
    async with database.sessions() as session:
        runtime = await session.get(MemorySelfReflectionRuntimeModel, 1)
        assert runtime is not None
        assert runtime.last_scanned_event_id >= missing_id
        states = list(await session.scalars(select(MemorySelfReflectionStateModel)))
    assert {row.canonical_person_id for row in states} == {person_id}
    mcp = MCPRepository(database)
    with pytest.raises(MemoryPartitionResolutionError):
        await mcp.record_invocation(
            conversation_key="private:1999",
            provider_id="test",
            tool_name="web_search",
            success=True,
            latency_seconds=0.01,
            result_size=1,
            artifact_created=False,
            error_category=None,
            trigger_message_id="mem-missing",
            bot_user_id="8000",
            result_excerpt="x",
        )


@pytest.mark.asyncio
async def test_complete_v2_legacy_event_does_not_write_receipt(database: Database) -> None:
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        session.add(
            ChatEventModel(
                bot_user_id="8000",
                platform_message_id="mem-legacy",
                scope_type="private",
                private_peer_user_id="1001",
                sender_user_id="1001",
                direction="inbound",
                event_kind="message",
                content="旧事件",
                visual_summary="",
                segments_json="[]",
                origin="user_message",
                occurred_at=_NOW,
                observed_at=_NOW,
            )
        )
    await MCPRepository(database).record_invocation(
        conversation_key="private:1001",
        provider_id="test",
        tool_name="web_search",
        success=True,
        latency_seconds=0.01,
        result_size=1,
        artifact_created=False,
        error_category=None,
        trigger_message_id="mem-legacy",
        bot_user_id="8000",
        result_excerpt="x",
    )
    async with database.sessions() as session:
        receipts = list(await session.scalars(select(MemoryToolReceiptModel)))
    assert receipts == []


@pytest.mark.asyncio
async def test_complete_v2_turn_session_uses_canonical_partition(database: Database) -> None:
    from tests.unit.test_memory_partition import _seed_binding

    person_id = str(uuid4())
    await _seed_binding(database, person_id=person_id, external_id="1001")
    await _flip_v2(database)
    inbound = _inbound(message_id="mem-turn-1", text="回忆")
    context = SimpleNamespace()
    lookup = DatabaseMemoryPartitionLookup(database)
    session = TurnMemorySession.open(
        inbound=inbound,
        identity=ConversationScope.private("8000", "1001"),
        runtime=SimpleNamespace(memory=SimpleNamespace(retrieval_enabled=True)),
        memory_context=context,  # type: ignore[arg-type]
        partition_lookup=lookup,
        origin=TurnOrigin.USER_MESSAGE,
        user_question=inbound.text,
        authority=TurnAuthority(
            actor_user_id="1001",
            bot_user_id="8000",
            origin=TurnOrigin.USER_MESSAGE,
            permission_ceiling=frozenset(),
            delegated_authority=None,
            authority_revision=1,
        ),
        structured_command=MemoryStructuredCommand.NONE,
    )
    assert session._identity.key.startswith("bot:")
    assert await session._memory_partition_key() == f"person:{person_id}"
    v1 = TurnMemorySession.open(
        inbound=inbound,
        identity=ConversationScope.private("8000", "1001"),
        runtime=SimpleNamespace(memory=SimpleNamespace(retrieval_enabled=True)),
        memory_context=context,  # type: ignore[arg-type]
        partition_lookup=lookup,
        origin=TurnOrigin.USER_MESSAGE,
        user_question=inbound.text,
        authority=TurnAuthority(
            actor_user_id="1001",
            bot_user_id="8000",
            origin=TurnOrigin.USER_MESSAGE,
            permission_ceiling=frozenset(),
            delegated_authority=None,
            authority_revision=1,
        ),
    )
    async with database.sessions() as db, db.begin():
        from qq_ai_bot.identity.db_models import IdentityRuntimeStateModel

        row = await db.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v1"
        row.cutover_id = None
        row.source_fingerprint = None
        row.completed_at = None
    assert await v1._memory_partition_key() == "private:1001"


def test_dream_partition_ignores_presence_and_qq() -> None:
    person_id = str(uuid4())
    left = MemoryFact(
        id=1,
        scope_type=MemoryScopeType.PERSON,
        subject_user_id="1001",
        kind=MemoryKind.FACT,
        memory_key="k",
        category="test",
        content="a",
        normalized_content="a",
        importance=3,
        confidence=1.0,
        source_type=MemorySourceType.EXPLICIT,
        status=MemoryStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
        canonical_subject_person_id=person_id,
    )
    right = MemoryFact(
        id=2,
        scope_type=MemoryScopeType.PERSON,
        subject_user_id="1002",
        kind=MemoryKind.FACT,
        memory_key="k",
        category="test",
        content="b",
        normalized_content="b",
        importance=3,
        confidence=1.0,
        source_type=MemorySourceType.EXPLICIT,
        status=MemoryStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
        canonical_subject_person_id=person_id,
    )
    first = DreamCandidate(
        fact=left,
        bot_user_id="8000",
        vector=object(),  # type: ignore[arg-type]
        signature="a",
        complete_v2=True,
    )
    second = DreamCandidate(
        fact=right,
        bot_user_id="8001",
        vector=object(),  # type: ignore[arg-type]
        signature="b",
        complete_v2=True,
    )
    assert first.partition_identity == second.partition_identity
    assert first.partition_identity == (person_id, None, None, None, MemoryKind.FACT.value)
    incomplete = MemoryFact(
        id=3,
        scope_type=MemoryScopeType.PERSON,
        subject_user_id="1001",
        kind=MemoryKind.FACT,
        memory_key="k",
        category="test",
        content="c",
        normalized_content="c",
        importance=3,
        confidence=1.0,
        source_type=MemorySourceType.EXPLICIT,
        status=MemoryStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
    )
    with pytest.raises(ValueError, match="incomplete_dream_owner"):
        _ = DreamCandidate(
            fact=incomplete,
            bot_user_id="8000",
            vector=object(),  # type: ignore[arg-type]
            signature="c",
            complete_v2=True,
        ).partition_identity


def test_cutover_and_rollup_never_insert_memory_jobs() -> None:
    cutover = Path("src/qq_ai_bot/identity/cutover_repository.py").read_text(encoding="utf-8")
    rollup = Path("src/qq_ai_bot/conversation/canonical_rollup.py").read_text(encoding="utf-8")
    migrations = Path("migrations/versions")
    assert "INSERT INTO memory_jobs" not in cutover
    assert "insert(MemoryJobModel)" not in cutover
    assert "MemoryJobRepository" not in cutover
    assert "from qq_ai_bot.memory.worker" not in cutover
    assert "from qq_ai_bot.memory.repository import" not in cutover
    assert "INSERT INTO memory_jobs" not in rollup
    assert "insert(MemoryJobModel)" not in rollup
    assert "DELETE FROM memory_jobs" in cutover
    for path in migrations.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "INSERT INTO memory_jobs" not in text
        assert "MemoryJobRepository" not in text
        assert "from qq_ai_bot.memory.worker" not in text


def test_cutover_preflight_detects_canonical_fact_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "fact-dup.db"
    _upgrade(path, monkeypatch, "0046")
    with _open(path) as connection:
        ids = _seed_identity(connection)
        _insert_people(connection, "1001", canonical_person_id=ids["person"])
        _insert_people(connection, "1002", canonical_person_id=ids["person"])
        _insert_memory_fact_person(
            connection,
            subject_user_id="1001",
            canonical_subject_person_id=ids["person"],
        )
        _insert_memory_fact_person(
            connection,
            subject_user_id="1002",
            canonical_subject_person_id=ids["person"],
        )
        connection.commit()
        assert memory_fact_canonical_conflict_kind(connection) == "canonical_person_fact"
        repo = IdentityCutoverRepository(path)
        with pytest.raises(IdentityCutoverPreconditionError) as exc:
            repo.require_no_canonical_memory_fact_conflicts(connection)
        assert exc.value.category == "canonical_memory_fact_conflict"
