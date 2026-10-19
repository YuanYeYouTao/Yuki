"""Memory rebuild selects permanent identities, independent of native account numbers."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from tests.support.event_seed import seed_scoped_event

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.conversations import ConversationScope
from yuki.identity.canonical_repository import (
    create_person_binding,
    create_presence,
    create_space_binding,
)
from yuki.identity.db_models import CanonicalPersonModel
from yuki.identity.person_identity import merge_persons
from yuki.memory.rebuild.models import MemoryRebuildSelection
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


@pytest.mark.parametrize("field", ["presence_ids", "person_ids", "space_ids", "conversation_ids"])
def test_rebuild_identity_filters_accept_only_internal_ids(field):
    with pytest.raises(ValidationError):
        MemoryRebuildSelection(**{field: ["1001"]})
    identity = str(uuid4())
    selection = MemoryRebuildSelection(**{field: [identity.upper(), identity]})
    assert getattr(selection, field) == (identity,)


async def test_rebuild_internal_filters_keep_platforms_and_merged_person_facts(database):
    writer = ScopedEventLedgerUnitOfWork(database)
    events = []
    people = []
    now = datetime.now(UTC)
    for platform in ("qq", "telegram"):
        async with database.immediate_session() as session:
            presence = await create_presence(
                session, external_account_id="80001", now=now, platform=platform
            )
            binding = await create_person_binding(
                session,
                external_account_id="10001",
                display_name=platform,
                now=now,
                platform=platform,
            )
            await create_space_binding(
                session,
                external_space_id="20001",
                name=platform,
                enabled=True,
                autonomous_enabled=True,
                require_mention=False,
                now=now,
                platform=platform,
            )
            people.append(binding.person_id)
        appended = await seed_scoped_event(
            writer,
            scope=ConversationScope.group(
                "80001", "20001", platform=platform, presence_id=presence.id
            ),
            platform_message_id=f"{platform}-source",
            sender_user_id="10001",
            direction="inbound",
            content="我住杭州",
        )
        events.append(appended.event)
    ledger = EventLedgerRepository(database)

    async def matches(**selection):
        stats = await ledger.count_rebuild_candidates(
            MemoryRebuildSelection(**selection), snapshot_max_event_id=events[-1].id
        )
        return stats.matched_events

    assert await matches(person_ids=(people[0],)) == 1
    assert await matches(presence_ids=(events[0].ingress_presence_id,)) == 1
    assert await matches(conversation_ids=(events[0].canonical_conversation_id,)) == 1
    async with database.sessions() as session:
        space = await session.scalar(
            select(CanonicalConversationModel.space_id).where(
                CanonicalConversationModel.id == events[0].canonical_conversation_id
            )
        )
    assert await matches(space_ids=(space,)) == 1
    assert await matches(event_ids=()) == 0
    assert await matches(event_ids=(events[0].id,)) == 1
    async with database.immediate_session() as session:
        await merge_persons(session, people[0], people[1])
        (await session.get(CanonicalPersonModel, people[0])).enabled = False
    # Current personal material unifies the family, including a paused original source.
    assert await matches(person_ids=(people[1],)) == 2
    assert await matches(person_ids=(people[0],)) == 2
    assert await matches(event_ids=(events[0].id,)) == 1
