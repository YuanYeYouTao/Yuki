"""Canonical, transport-neutral Memory partition behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
)
from qq_ai_bot.identity.canonical_repository import IDENTITY_PLATFORM, ensure_person, ensure_space
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    IdentityBindingModel,
    SpaceBindingModel,
)
from qq_ai_bot.memory.partition import (
    MemoryPartitionResolutionError,
    format_canonical_memory_partition,
    parse_canonical_memory_partition,
    resolve_memory_partition_for_event,
    resolve_memory_partition_from_scope,
)
from qq_ai_bot.memory.runtime.partition_lookup import DatabaseMemoryPartitionLookup
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel

_NOW = datetime(2026, 8, 24, tzinfo=UTC)


def _event(*, message_id: str, peer: str | None = None, group: str | None = None) -> ChatEventModel:
    return ChatEventModel(
        bot_user_id="8000",
        platform_message_id=message_id,
        scope_type="group" if group else "private",
        group_id=group,
        private_peer_user_id=peer,
        sender_user_id=peer or "1001",
        direction="inbound",
        event_kind="message",
        content="hi",
        visual_summary="",
        segments_json="[]",
        origin="user_message",
        occurred_at=_NOW,
        observed_at=_NOW,
    )


def test_format_and_parse_only_accept_canonical_owner_ids() -> None:
    person_id = str(uuid4())
    space_id = str(uuid4())
    assert format_canonical_memory_partition(person_id=person_id) == f"person:{person_id}"
    assert format_canonical_memory_partition(space_id=space_id) == f"space:{space_id}"
    assert parse_canonical_memory_partition(f"person:{person_id}").person_id == person_id
    assert parse_canonical_memory_partition(f"space:{space_id}").space_id == space_id

    cases = (
        ("private:1001", "legacy_or_conversation_key"),
        ("group:2001", "legacy_or_conversation_key"),
        ("bot:8000:private:1001", "legacy_or_conversation_key"),
        (person_id, "conversation_uuid"),
        ("person:not-a-uuid", "malformed_uuid"),
        ("", "missing_owner"),
    )
    for raw, reason in cases:
        with pytest.raises(MemoryPartitionResolutionError) as exc:
            parse_canonical_memory_partition(raw)
        assert exc.value.reason == reason
    for owners in ({}, {"person_id": person_id, "space_id": space_id}):
        with pytest.raises(MemoryPartitionResolutionError) as exc:
            format_canonical_memory_partition(**owners)
        assert exc.value.reason == "owner_shape"


@pytest.mark.asyncio
async def test_private_event_resolves_all_bindings_to_one_person(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        person_id = await ensure_person(session, "1101", now=_NOW)
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=person_id,
                platform=IDENTITY_PLATFORM,
                external_account_id="1102",
                display_name="",
                status="active",
                revision=1,
                first_seen_at=_NOW,
                last_seen_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    async with database.sessions() as session:
        first = await resolve_memory_partition_for_event(
            session, _event(message_id="p1", peer="1101")
        )
        second = await resolve_memory_partition_for_event(
            session, _event(message_id="p2", peer="1102")
        )
    assert first.value == second.value == f"person:{person_id}"
    assert first.person_id == second.person_id == person_id


@pytest.mark.asyncio
async def test_space_partition_requires_enabled_active_binding(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        space_id = await ensure_space(session, "2001", now=_NOW)
        binding = await session.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.space_id == space_id)
        )
        assert binding is not None
        binding.status = "disabled"
    event = _event(message_id="g1", group="2001")
    async with database.sessions() as session:
        with pytest.raises(MemoryPartitionResolutionError) as disabled:
            await resolve_memory_partition_for_event(session, event)
    assert disabled.value.reason == "missing_owner"

    async with database.sessions() as session, session.begin():
        binding = await session.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.space_id == space_id)
        )
        assert binding is not None
        binding.status = "active"
    async with database.sessions() as session:
        partition = await resolve_memory_partition_for_event(session, event)
    assert partition.value == f"space:{space_id}"
    assert partition.space_id == space_id


@pytest.mark.asyncio
async def test_scope_lookup_is_canonical_and_fails_closed(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        person_id = await ensure_person(session, "1001", now=_NOW)
        space_id = await ensure_space(session, "2001", now=_NOW)
    lookup = DatabaseMemoryPartitionLookup(database)
    assert (
        await lookup.resolve_from_scope(group_id=None, private_peer_user_id="1001")
        == f"person:{person_id}"
    )
    assert (
        await lookup.resolve_from_scope(group_id="2001", private_peer_user_id=None)
        == f"space:{space_id}"
    )

    async with database.sessions() as session:
        for group_id, peer, reason in (
            ("2001", "1001", "owner_shape"),
            (None, None, "owner_shape"),
            (None, "1999", "missing_owner"),
        ):
            with pytest.raises(MemoryPartitionResolutionError) as exc:
                await resolve_memory_partition_from_scope(
                    session,
                    group_id=group_id,
                    private_peer_user_id=peer,
                )
            assert exc.value.reason == reason
            assert "1001" not in str(exc.value)
            assert "2001" not in str(exc.value)


@pytest.mark.asyncio
async def test_conversation_uuid_cannot_be_used_as_memory_owner(database: Database) -> None:
    person_id = str(uuid4())
    binding_id = str(uuid4())
    alias_id = str(uuid4())
    async with database.sessions() as session, session.begin():
        await session.execute(text("PRAGMA defer_foreign_keys=ON"))
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
                id=binding_id,
                person_id=person_id,
                platform=IDENTITY_PLATFORM,
                external_account_id="1103",
                display_name="",
                status="active",
                revision=1,
                first_seen_at=_NOW,
                last_seen_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
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
                scope_key=f"bot:8000:private:1103:{alias_id}",
                is_primary=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    async with database.sessions() as session:
        with pytest.raises(MemoryPartitionResolutionError) as exc:
            await resolve_memory_partition_for_event(
                session, _event(message_id="conversation-owner", peer="1103")
            )
    assert exc.value.reason == "conversation_uuid"
