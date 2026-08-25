"""Transport-neutral Memory partition helper."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    IdentityRuntimeStateModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.memory.partition import (
    MemoryPartitionResolutionError,
    format_canonical_memory_partition,
    format_legacy_memory_partition,
    parse_canonical_memory_partition,
    resolve_canonical_memory_partition_for_event,
    resolve_memory_partition_for_event,
    resolve_memory_partition_from_scope,
)
from qq_ai_bot.memory.runtime.partition_lookup import DatabaseMemoryPartitionLookup
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel

_NOW = datetime(2026, 8, 24, tzinfo=UTC)


def test_legacy_and_canonical_format_rules() -> None:
    assert (
        format_legacy_memory_partition(group_id=None, private_peer_user_id="1001") == "private:1001"
    )
    assert (
        format_legacy_memory_partition(group_id="2001", private_peer_user_id=None) == "group:2001"
    )
    person_id = str(uuid4())
    space_id = str(uuid4())
    assert format_canonical_memory_partition(person_id=person_id) == f"person:{person_id}"
    assert format_canonical_memory_partition(space_id=space_id) == f"space:{space_id}"
    with pytest.raises(MemoryPartitionResolutionError) as exc:
        format_canonical_memory_partition(person_id=person_id, space_id=space_id)
    assert exc.value.reason == "owner_shape"
    with pytest.raises(MemoryPartitionResolutionError) as exc:
        format_canonical_memory_partition()
    assert exc.value.reason == "owner_shape"
    for kwargs in (
        {"group_id": "2001", "private_peer_user_id": "1001"},
        {"group_id": None, "private_peer_user_id": None},
    ):
        with pytest.raises(MemoryPartitionResolutionError) as scoped:
            format_legacy_memory_partition(**kwargs)
        assert scoped.value.reason == "owner_shape"
        assert "2001" not in str(scoped.value)
        assert "1001" not in str(scoped.value)


def test_parse_canonical_partition_fail_closed() -> None:
    person_id = str(uuid4())
    parsed = parse_canonical_memory_partition(f"person:{person_id}")
    assert parsed.person_id == person_id
    assert parsed.space_id is None
    for raw, reason in (
        ("private:1001", "legacy_or_conversation_key"),
        ("group:2001", "legacy_or_conversation_key"),
        ("bot:8000:private:1001", "legacy_or_conversation_key"),
        (person_id, "conversation_uuid"),
        ("person:not-a-uuid", "malformed_uuid"),
        ("space:", "malformed_uuid"),
        ("conversation:x", "malformed_partition"),
        ("", "missing_owner"),
    ):
        with pytest.raises(MemoryPartitionResolutionError) as exc:
            parse_canonical_memory_partition(raw)
        assert exc.value.reason == reason


async def _flip_v2(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v2"
        row.cutover_id = str(uuid4())
        row.source_fingerprint = "cutover-fingerprint"
        row.completed_at = _NOW


async def _seed_binding(
    database: Database,
    *,
    person_id: str,
    external_id: str,
    status: str = "active",
) -> None:
    async with database.sessions() as session, session.begin():
        if await session.get(CanonicalPersonModel, person_id) is None:
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
                external_account_id=external_id,
                display_name="",
                status=status,
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )


@pytest.mark.asyncio
async def test_v1_event_keeps_private_group_keys(database: Database) -> None:
    event = ChatEventModel(
        bot_user_id="8000",
        platform_message_id="p1",
        scope_type="private",
        private_peer_user_id="1001",
        sender_user_id="1001",
        direction="inbound",
        event_kind="message",
        content="hi",
        visual_summary="",
        segments_json="[]",
        origin="user_message",
        occurred_at=_NOW,
        observed_at=_NOW,
    )
    async with database.sessions() as session:
        partition = await resolve_memory_partition_for_event(session, event)
    assert partition.value == "private:1001"
    assert partition.person_id is None
    group_event = ChatEventModel(
        bot_user_id="8000",
        platform_message_id="g-v1",
        scope_type="group",
        group_id="2001",
        private_peer_user_id="1001",
        sender_user_id="1001",
        direction="inbound",
        event_kind="message",
        content="hi",
        visual_summary="",
        segments_json="[]",
        origin="user_message",
        occurred_at=_NOW,
        observed_at=_NOW,
    )
    async with database.sessions() as session:
        group = await resolve_memory_partition_for_event(session, group_event)
    assert group.value == "group:2001"
    assert group.person_id is None
    assert group.space_id is None


@pytest.mark.asyncio
async def test_v2_resolves_active_binding_and_fails_closed(database: Database) -> None:
    person_id = str(uuid4())
    await _seed_binding(database, person_id=person_id, external_id="1001")
    await _seed_binding(database, person_id=person_id, external_id="1002")
    await _flip_v2(database)
    event = ChatEventModel(
        bot_user_id="8000",
        platform_message_id="p2",
        scope_type="private",
        private_peer_user_id="1002",
        sender_user_id="1002",
        direction="inbound",
        event_kind="message",
        content="hi",
        visual_summary="",
        segments_json="[]",
        origin="user_message",
        occurred_at=_NOW,
        observed_at=_NOW,
    )
    async with database.sessions() as session:
        first = await resolve_canonical_memory_partition_for_event(session, event)
        event.private_peer_user_id = "1001"
        event.sender_user_id = "1001"
        second = await resolve_canonical_memory_partition_for_event(session, event)
    assert first.value == second.value == f"person:{person_id}"
    missing = ChatEventModel(
        bot_user_id="8000",
        platform_message_id="p3",
        scope_type="private",
        private_peer_user_id="1999",
        sender_user_id="1999",
        direction="inbound",
        event_kind="message",
        content="hi",
        visual_summary="",
        segments_json="[]",
        origin="user_message",
        occurred_at=_NOW,
        observed_at=_NOW,
    )
    async with database.sessions() as session:
        with pytest.raises(MemoryPartitionResolutionError) as exc:
            await resolve_canonical_memory_partition_for_event(session, missing)
    assert exc.value.reason == "missing_owner"


@pytest.mark.asyncio
async def test_v2_space_partition_and_disabled_binding(database: Database) -> None:
    space_id = str(uuid4())
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
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
                status="disabled",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    event = ChatEventModel(
        bot_user_id="8000",
        platform_message_id="g1",
        scope_type="group",
        group_id="2001",
        sender_user_id="1001",
        direction="inbound",
        event_kind="message",
        content="hi",
        visual_summary="",
        segments_json="[]",
        origin="user_message",
        occurred_at=_NOW,
        observed_at=_NOW,
    )
    async with database.sessions() as session:
        with pytest.raises(MemoryPartitionResolutionError) as exc:
            await resolve_canonical_memory_partition_for_event(session, event)
    assert exc.value.reason == "missing_owner"
    async with database.sessions() as session, session.begin():
        binding = await session.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.external_space_id == "2001")
        )
        assert binding is not None
        binding.status = "active"
    async with database.sessions() as session:
        partition = await resolve_canonical_memory_partition_for_event(session, event)
    assert partition.value == f"space:{space_id}"


@pytest.mark.asyncio
async def test_v2_rejects_conversation_uuid_as_owner(database: Database) -> None:
    from qq_ai_bot.conversation.canonical_db_models import (
        CanonicalConversationModel,
        ConversationLegacyAliasModel,
    )

    person_id = str(uuid4())
    await _seed_binding(database, person_id=person_id, external_id="1001")
    await _flip_v2(database)
    alias_id = str(uuid4())
    async with database.sessions() as session, session.begin():
        session.add(
            CanonicalConversationModel(
                id=person_id,
                kind="private",
                person_id=person_id,
                space_id=None,
                primary_alias_id=alias_id,
                primary_marker=1,
                generation=1,
                starts_after_event_id=0,
                last_event_id=0,
                last_generation_change_event_id=0,
                covered_through_event_id=0,
                uncovered_event_count=0,
                uncovered_character_count=0,
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.add(
            ConversationLegacyAliasModel(
                id=alias_id,
                conversation_id=person_id,
                scope_key=f"bot:8000:private:1001:{alias_id}",
                is_primary=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    event = ChatEventModel(
        bot_user_id="8000",
        platform_message_id="p-conv",
        scope_type="private",
        private_peer_user_id="1001",
        sender_user_id="1001",
        direction="inbound",
        event_kind="message",
        content="hi",
        visual_summary="",
        segments_json="[]",
        origin="user_message",
        occurred_at=_NOW,
        observed_at=_NOW,
    )
    async with database.sessions() as session:
        with pytest.raises(MemoryPartitionResolutionError) as exc:
            await resolve_canonical_memory_partition_for_event(session, event)
    assert exc.value.reason == "conversation_uuid"


@pytest.mark.asyncio
async def test_ambiguous_active_bindings_fail_closed() -> None:
    from qq_ai_bot.memory.partition import resolve_active_person_id

    class _Binding:
        def __init__(self, person_id: str) -> None:
            self.person_id = person_id

    class _Result:
        def __init__(self, rows: list[_Binding]) -> None:
            self._rows = rows

        def __iter__(self) -> object:
            return iter(self._rows)

    class _Session:
        async def scalars(self, _statement: object) -> _Result:
            return _Result([_Binding(str(uuid4())), _Binding(str(uuid4()))])

    with pytest.raises(MemoryPartitionResolutionError) as exc:
        await resolve_active_person_id(_Session(), "1001")  # type: ignore[arg-type]
    assert exc.value.reason == "ambiguous_owner"


@pytest.mark.asyncio
async def test_from_scope_and_lookup_v1_keys(database: Database) -> None:
    async with database.sessions() as session:
        private = await resolve_memory_partition_from_scope(
            session, group_id=None, private_peer_user_id="1001"
        )
        group = await resolve_memory_partition_from_scope(
            session, group_id="2001", private_peer_user_id=None
        )
    assert private.value == "private:1001"
    assert group.value == "group:2001"
    lookup = DatabaseMemoryPartitionLookup(database)
    assert await lookup.resolve_from_scope(group_id=None, private_peer_user_id="1001") == (
        "private:1001"
    )
    assert await lookup.resolve_from_scope(group_id="2001", private_peer_user_id=None) == (
        "group:2001"
    )


@pytest.mark.asyncio
async def test_from_scope_and_lookup_v2_binding_key(database: Database) -> None:
    person_id = str(uuid4())
    await _seed_binding(database, person_id=person_id, external_id="1001")
    await _flip_v2(database)
    expected = f"person:{person_id}"
    async with database.sessions() as session:
        partition = await resolve_memory_partition_from_scope(
            session, group_id=None, private_peer_user_id="1001"
        )
    assert partition.value == expected
    lookup = DatabaseMemoryPartitionLookup(database)
    assert await lookup.resolve_from_scope(group_id=None, private_peer_user_id="1001") == expected


@pytest.mark.asyncio
async def test_from_scope_and_lookup_v2_missing_owner_fails(database: Database) -> None:
    await _flip_v2(database)
    lookup = DatabaseMemoryPartitionLookup(database)
    async with database.sessions() as session:
        with pytest.raises(MemoryPartitionResolutionError) as scoped:
            await resolve_memory_partition_from_scope(
                session, group_id=None, private_peer_user_id="1999"
            )
    with pytest.raises(MemoryPartitionResolutionError) as adapted:
        await lookup.resolve_from_scope(group_id=None, private_peer_user_id="1999")
    assert scoped.value.reason == adapted.value.reason == "missing_owner"


def _assert_owner_shape(exc: pytest.ExceptionInfo[MemoryPartitionResolutionError]) -> None:
    assert exc.value.reason == "owner_shape"
    leaked = str(exc.value)
    assert "1001" not in leaked
    assert "2001" not in leaked


@pytest.mark.asyncio
async def test_from_scope_xor_rejects_both_and_neither_v1(database: Database) -> None:
    lookup = DatabaseMemoryPartitionLookup(database)
    async with database.sessions() as session:
        with pytest.raises(MemoryPartitionResolutionError) as both:
            await resolve_memory_partition_from_scope(
                session, group_id="2001", private_peer_user_id="1001"
            )
        with pytest.raises(MemoryPartitionResolutionError) as neither:
            await resolve_memory_partition_from_scope(
                session, group_id=None, private_peer_user_id=None
            )
    _assert_owner_shape(both)
    _assert_owner_shape(neither)
    with pytest.raises(MemoryPartitionResolutionError) as lookup_both:
        await lookup.resolve_from_scope(group_id="2001", private_peer_user_id="1001")
    with pytest.raises(MemoryPartitionResolutionError) as lookup_neither:
        await lookup.resolve_from_scope(group_id=None, private_peer_user_id=None)
    _assert_owner_shape(lookup_both)
    _assert_owner_shape(lookup_neither)


@pytest.mark.asyncio
async def test_from_scope_xor_rejects_both_and_neither_v2(database: Database) -> None:
    await _flip_v2(database)
    lookup = DatabaseMemoryPartitionLookup(database)
    async with database.sessions() as session:
        with pytest.raises(MemoryPartitionResolutionError) as both:
            await resolve_memory_partition_from_scope(
                session, group_id="2001", private_peer_user_id="1001"
            )
        with pytest.raises(MemoryPartitionResolutionError) as neither:
            await resolve_memory_partition_from_scope(
                session, group_id=None, private_peer_user_id=None
            )
    _assert_owner_shape(both)
    _assert_owner_shape(neither)
    with pytest.raises(MemoryPartitionResolutionError) as lookup_both:
        await lookup.resolve_from_scope(group_id="2001", private_peer_user_id="1001")
    _assert_owner_shape(lookup_both)


@pytest.mark.asyncio
async def test_from_scope_v2_space_and_disabled(database: Database) -> None:
    space_id = str(uuid4())
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
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
                status="disabled",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    lookup = DatabaseMemoryPartitionLookup(database)
    async with database.sessions() as session:
        with pytest.raises(MemoryPartitionResolutionError) as disabled:
            await resolve_memory_partition_from_scope(
                session, group_id="2001", private_peer_user_id=None
            )
    assert disabled.value.reason == "missing_owner"
    async with database.sessions() as session, session.begin():
        binding = await session.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.external_space_id == "2001")
        )
        assert binding is not None
        binding.status = "active"
    async with database.sessions() as session:
        partition = await resolve_memory_partition_from_scope(
            session, group_id="2001", private_peer_user_id=None
        )
    assert partition.value == f"space:{space_id}"
    assert await lookup.resolve_from_scope(group_id="2001", private_peer_user_id=None) == (
        f"space:{space_id}"
    )
