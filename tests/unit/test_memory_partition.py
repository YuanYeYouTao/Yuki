"""Canonical, transport-neutral Memory partition behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from tests.conftest import make_settings
from tests.support.user_profiles_helpers import inbound, observe_members

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
)
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.identity.canonical_repository import IDENTITY_PLATFORM, ensure_person, ensure_space
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    SpaceBindingModel,
)
from qq_ai_bot.memory.enums import MemoryScopeType, MemoryTargetRole
from qq_ai_bot.memory.extraction import MemoryClaim
from qq_ai_bot.memory.partition import (
    MemoryPartitionResolutionError,
    format_canonical_memory_partition,
    parse_canonical_memory_partition,
    resolve_memory_partition_for_event,
    resolve_memory_partition_from_scope,
)
from qq_ai_bot.memory.read_scope import MemoryReadScopeResolver
from qq_ai_bot.memory.runtime.partition_lookup import DatabaseMemoryPartitionLookup
from qq_ai_bot.memory.subjects import SubjectContextBuilder, SubjectResolver
from qq_ai_bot.memory.targets import MemoryTargetResolver
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel, MembershipModel
from qq_ai_bot.persistence.repository_records import EventRecord

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
@pytest.mark.parametrize("disabled", ["binding", "owner"])
async def test_space_partition_requires_enabled_active_binding(
    database: Database, disabled
) -> None:
    async with database.sessions() as session, session.begin():
        space_id = await ensure_space(session, "2001", now=_NOW)
        binding = await session.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.space_id == space_id)
        )
        assert binding is not None
        space = await session.get(CanonicalSpaceModel, space_id)
        assert space is not None
        if disabled == "binding":
            binding.status = "disabled"
        else:
            space.enabled = False
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
        space = await session.get(CanonicalSpaceModel, space_id)
        assert space is not None
        space.enabled = True
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


@pytest.mark.asyncio
async def test_targets_keep_sixth_mention_and_reply_after_five_people(database: Database) -> None:
    from dataclasses import replace

    user_ids = tuple(str(62001 + index) for index in range(7))
    people = await observe_members(database, ("1001", *user_ids))
    message = replace(
        inbound(
            "references", message_id="references", group_id="2001", mentioned_user_ids=user_ids[:6]
        ),
        reply_sender_user_id=user_ids[-1],
    )
    targets = await MemoryTargetResolver(people).resolve(message, include_self=True)
    assert targets[0].role is MemoryTargetRole.CURRENT_SELF
    person_targets = tuple(
        target for target in targets if target.scope_type is MemoryScopeType.PERSON
    )
    local_targets = tuple(
        target for target in targets if target.scope_type is MemoryScopeType.PERSON_GROUP
    )
    assert tuple(target.subject_user_id for target in person_targets) == ("1001", *user_ids)
    assert tuple(target.subject_user_id for target in local_targets) == ("1001", *user_ids)
    assert all(target.group_id == "2001" for target in local_targets)
    assert sum(target.scope_type is MemoryScopeType.GROUP for target in targets) == 1


@pytest.mark.asyncio
async def test_targets_share_one_canonical_person_across_mention_and_reply_binding(
    database: Database,
) -> None:
    from dataclasses import replace

    user_ids = tuple(str(62021 + index) for index in range(7))
    people = await observe_members(database, ("1001", *user_ids[:6]))
    async with database.immediate_session() as session:
        await ensure_person(session, user_ids[-1], now=_NOW)
        binding = await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.external_account_id == user_ids[0]
            )
        )
        assert binding is not None
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=binding.person_id,
                platform="qq",
                external_account_id="62999",
                display_name="reply account",
                first_seen_at=_NOW,
                last_seen_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    message = replace(
        inbound("refs", message_id="bound-refs", group_id="2001", mentioned_user_ids=user_ids),
        reply_sender_user_id="62999",
    )
    targets = await MemoryTargetResolver(people).resolve(message)
    for scope in (MemoryScopeType.PERSON, MemoryScopeType.PERSON_GROUP):
        assert tuple(
            target.subject_user_id for target in targets if target.scope_type is scope
        ) == ("1001", *user_ids[:6])
    assert not any(target.subject_user_id in {"62999", user_ids[-1]} for target in targets)


@pytest.mark.asyncio
async def test_batch_person_scopes_use_direct_membership_and_forgetting_takes_effect(
    database: Database,
) -> None:
    people = await observe_members(database, ("1001", "62041"), group_id="2001")
    await observe_members(database, ("62041", "62042"), group_id="2002")
    await observe_members(database, ("1001",), group_id="2999")
    resolver = MemoryReadScopeResolver(database)
    batch = await resolver.people("1001", ("1001", "62041", "62042", "404"))
    assert set(batch) == {"1001", "62041"}
    assert {target.group_id for target in batch["62041"].targets if target.group_id} == {"2001"}
    assert (await resolver.person("1001", "62041", group_id="2999")).targets == ()
    message = inbound(
        "other group",
        message_id="other-group",
        group_id="2999",
        mentioned_user_ids=("62041", "62042"),
    )
    targets = await MemoryTargetResolver(people).resolve(message)
    assert any(
        target.subject_user_id == "62041" and target.scope_type is MemoryScopeType.PERSON
        for target in targets
    )
    assert not any(
        target.subject_user_id in {"62041", "62042"}
        and target.scope_type is MemoryScopeType.PERSON_GROUP
        for target in targets
    )
    assert not any(target.subject_user_id == "62042" for target in targets)
    async with database.immediate_session() as session:
        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.external_account_id == "62041")
        )
        group = await session.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.external_space_id == "2001")
        )
        assert binding is not None and group is not None
        membership = await session.get(MembershipModel, (binding.person_id, group.space_id))
        assert membership is not None
        await session.delete(membership)
    assert (await resolver.person("1001", "62041")).targets == ()


@pytest.mark.asyncio
async def test_normalized_claim_names_resolve_complete_exact_candidates_without_fuzzy_selection(
    database: Database,
) -> None:
    repeated = tuple(str(62061 + index) for index in range(7))
    people = await observe_members(database, ("1001", "62060", *repeated))
    await people.observe(user_id="62060", nickname="Ａ-lpHa", group_id="2001", group_card="")
    for user_id in repeated:
        await people.observe(user_id=user_id, nickname="同名", group_id="2001", group_card="")
    assert await people.find_group_members_by_exact_name("alpha", "2001") == ()
    assert await people.find_group_members_by_exact_name("Ａ-lpHa", "2001") == ("62060",)
    exact = await people.find_group_members_by_normalized_names(
        ("alpha", "A lp-ha", "同名"), "2001"
    )
    assert tuple(item.user_id for item in exact["alpha"]) == ("62060",)
    assert exact["alpha"] == exact["A lp-ha"]
    assert len(exact["同名"]) == len(repeated)
    fuzzy = await people.search_group_member_names("同名", "2001", limit=len(repeated))
    assert tuple(item.user_id for item in fuzzy) == repeated
    assert all(item.exact for item in fuzzy)
    event = EventRecord(
        id=1,
        bot_user_id="9999",
        platform_message_id="claims",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content="declarations",
        visual_summary="",
        segments=(),
        occurred_at=_NOW,
        group_id="2001",
        sender_nickname="event nickname",
    )
    claims = tuple(
        MemoryClaim(
            subject_ref="named_member",
            subject_name=name,
            scope_type=MemoryScopeType.PERSON_GROUP,
            memory_key=f"claim-{index}",
            category="declared",
            content="reported fact",
            evidence_quote="declarations",
        )
        for index, name in enumerate(("alpha", "A lp-ha", "同名", "alph"))
    )
    builder = SubjectContextBuilder(people)
    resolved, context = await builder.resolve_claim_names(
        event, claims, SubjectResolver.context(event)
    )
    assert resolved[0].subject_ref == resolved[1].subject_ref != "named_member"
    assert resolved[2:] == claims[2:]
    subject = context.resolve(f"{resolved[0].subject_ref}:person_group")
    assert subject is not None and subject.subject_user_id == "62060"
    displayed = await builder.build(event)
    assert displayed.available_subjects[0].display_label == "event nickname（ID 1001）"


@pytest.mark.asyncio
async def test_exact_tool_ambiguity_reports_all_authorized_people_and_groups(
    database: Database,
) -> None:
    from qq_ai_bot.memory.repository import MemoryFactRepository
    from qq_ai_bot.memory.service import MemoryFactService
    from qq_ai_bot.persistence.repositories import AgentActionRepository, EventLedgerRepository
    from qq_ai_bot.services.agent_tools import AgentToolService, ToolRuntime, _ToolFailure

    user_ids = tuple(str(62081 + index) for index in range(8))
    people = await observe_members(database, ("1001", *user_ids))
    for user_id in user_ids:
        await people.observe(user_id=user_id, nickname="共同名字", group_id="2001", group_card="")
    groups = tuple(str(62101 + index) for index in range(8))
    for group_id in groups:
        await observe_members(database, ("1001",), group_id=group_id)
    async with database.immediate_session() as session:
        for group_id in groups:
            binding = await session.scalar(
                select(SpaceBindingModel).where(SpaceBindingModel.external_space_id == group_id)
            )
            assert binding is not None
            space = await session.get(CanonicalSpaceModel, binding.space_id)
            assert space is not None
            space.name = "共同群名"
    tools = AgentToolService(
        settings=make_settings(database.url),
        ledger=EventLedgerRepository(database),
        memories=MemoryFactService(MemoryFactRepository(database)),
        actions=AgentActionRepository(database),
    )
    runtime = ToolRuntime(
        inbound=inbound("choose target", message_id="ambiguity", group_id="2001"),
        gateway=None,
        allow_generic_onebot=False,
    )
    people_failure = await tools._resolve_person_memory_selection(
        {"display_name": "共同名字"}, runtime
    )
    group_failure = await tools._read_group_selector(
        {"group_name": "共同群名"}, runtime, default_current=False
    )
    assert isinstance(people_failure, _ToolFailure) and people_failure.code == "ambiguous_person"
    assert {item["user_id"] for item in people_failure.data["candidates"]} == set(user_ids)
    assert isinstance(group_failure, _ToolFailure) and group_failure.code == "ambiguous_group"
    assert {item["group_id"] for item in group_failure.data["candidates"]} == set(groups)
