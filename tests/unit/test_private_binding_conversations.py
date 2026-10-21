"""Entrance histories resolve their binding even when legacy aliases collide."""

from datetime import UTC, datetime

from sqlalchemy import select
from tests.support.event_seed import seed_scoped_event

from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
)
from yuki.conversation.hydrate import bump_canonical_generation, ensure_legacy_alias
from yuki.conversation.scope import ConversationTurnSnapshot
from yuki.conversation.scope_repository import ConversationScopeRepository
from yuki.domain.conversations import ConversationScope
from yuki.identity.canonical_repository import (
    create_presence,
    ensure_account,
    find_identity_binding,
    find_presence,
    replace_assignment,
)
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.persistence.people_repository import PeopleRepository
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from yuki.runtime.work_repository import WorkRepository


async def entries(database):
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        primary = await find_identity_binding(session, "1001")
        primary_presence = await find_presence(session, "8000")
        bindings = [primary.id]
        scopes = [ConversationScope.private("8000", "1001", presence_id=primary_presence.id)]
        for platform, account, bot in (
            ("qq", "second-qq", "8000"),
            ("telegram", "1001", "telegram-bot"),
            ("app", "opaque-app-account", "app-bot"),
        ):
            if platform != "qq":
                presence = await create_presence(
                    session, external_account_id=bot, now=now, platform=platform
                )
            else:
                presence = primary_presence
            account_row = await ensure_account(
                session,
                account,
                platform=platform,
                display_name="same name",
                now=now,
            )
            binding = await replace_assignment(
                session,
                account_row,
                None,
                await session.get(CanonicalPersonModel, primary.person_id),
                now=now,
            )
            bindings.append(binding.id)
            scopes.append(
                ConversationScope.private(bot, account, platform=platform, presence_id=presence.id)
            )
        return primary.person_id, bindings, scopes


async def test_four_bindings_keep_identity_but_separate_prompts_and_generation(database):
    person, bindings, scopes = await entries(database)
    writer = ScopedEventLedgerUnitOfWork(database)
    repository = ConversationScopeRepository(database)
    events = []
    for index, scope in enumerate(scopes):
        event = await seed_scoped_event(
            writer,
            scope=scope,
            platform_message_id=f"in-{index}",
            sender_user_id=scope.private_peer_user_id,
            direction="inbound",
            content=f"private-{index}",
        )
        event = event.event
        events.append(event)
        assert event.author_person_id == person
        state = await repository.get(scope)
        assert state.runtime_scope_key == f"conversation:{state.id}"
        async with database.sessions() as reader:
            conversation = await reader.get(CanonicalConversationModel, state.id)
            assert conversation.identity_binding_id == bindings[index]
        snapshot = await repository.load_prompt_snapshot(scope)
        assert [row.content for row in snapshot.raw_events] == [f"private-{index}"]
    assert len({event.canonical_conversation_id for event in events}) == 4
    work = WorkRepository(database)
    lease = await work.acquire(events[1].canonical_conversation_id, 1)
    accepted = await work.accept(
        lease, source_key="original-second-binding", source={}, goal="keep"
    )
    await work.release(lease)
    original_work = await work.get(accepted["id"])
    async with database.immediate_session() as session:
        await bump_canonical_generation(
            session, events[0].canonical_conversation_id, event_id=events[0].id, force=True
        )
    assert (await repository.get(scopes[0])).generation == 2
    for scope in scopes[1:]:
        assert (await repository.get(scope)).generation == 1
    assert await work.get(accepted["id"]) == original_work


async def test_old_transport_alias_never_redirects_new_binding_reads_or_writes(database):
    _, _, scopes = await entries(database)
    first, second = scopes[:2]
    writer = ScopedEventLedgerUnitOfWork(database)
    original = await seed_scoped_event(
        writer,
        scope=first,
        platform_message_id="old",
        sender_user_id="1001",
        direction="inbound",
        content="old needle secret",
    )
    original = original.event
    async with database.immediate_session() as session:
        await ensure_legacy_alias(
            session,
            conversation_id=original.canonical_conversation_id,
            scope_key=second.key,
            primary=False,
        )
    ledger = EventLedgerRepository(database)
    repository = ConversationScopeRepository(database)
    expected = []
    for index in range(3):
        for direction in ("inbound", "outbound"):
            event = await seed_scoped_event(
                writer,
                scope=second,
                platform_message_id=f"new-{index}-{direction}",
                sender_user_id="second-qq" if direction == "inbound" else "8000",
                direction=direction,
                content=f"new needle {index} {direction}",
            )
            event = event.event
            expected.append(event.id)
        state = await repository.get(second)
        assert state.id != original.canonical_conversation_id
        version, rows = await ledger.read_scope_context(second, limit=20)
        assert [row.id for row in rows] == expected
        assert await ledger.read_version_matches(version)
        page = await ledger.query_history(
            conversation_id=state.id,
            starts_after_event_id=state.starts_after_event_id,
            keyword="needle",
            order="oldest",
        )
        assert [row.id for row in page.events] == expected
        turn = ConversationTurnSnapshot(
            state.id, state.runtime_scope_key, 1, event.id, 1, second.key
        )
        assert [
            row.id
            for row in (
                await repository.load_prompt_snapshot(second, expected_turn=turn)
            ).raw_events
        ] == expected
    async with database.sessions() as session:
        alias = await session.scalar(
            select(ConversationLegacyAliasModel).where(
                ConversationLegacyAliasModel.scope_key == second.key
            )
        )
        assert alias.conversation_id == original.canonical_conversation_id
    async with database.immediate_session() as session:
        await bump_canonical_generation(session, state.id, event_id=event.id, force=True)
    assert (await repository.get(first)).generation == 1
    assert (await repository.get(second)).generation == 2
    assert await ledger.list_scope_recent(second, limit=20) == ()
    assert [row.id for row in await ledger.list_scope_recent(first, limit=20)] == [original.id]


async def test_person_erasure_removes_all_private_binding_conversations(database):
    person, bindings, scopes = await entries(database)
    writer = ScopedEventLedgerUnitOfWork(database)
    ids = []
    for index, scope in enumerate(scopes):
        event = await seed_scoped_event(
            writer,
            scope=scope,
            platform_message_id=f"erase-{index}",
            direction="inbound",
            sender_user_id=scope.private_peer_user_id,
            content="private attribution",
        )
        event = event.event
        ids.append(event.canonical_conversation_id)
    work = WorkRepository(database)
    lease = await work.acquire(ids[1], 1)
    accepted = await work.accept(lease, source_key="disabled-entrance", source={}, goal="erase")
    await work.release(lease)
    async with database.immediate_session() as session:
        binding = await session.get(IdentityBindingModel, bindings[1])
        binding.status = "disabled"
    assert await PeopleRepository(database).delete_person_id(person)
    assert await work.get(accepted["id"]) is None
    async with database.sessions() as session:
        assert not list(
            await session.scalars(
                select(CanonicalConversationModel).where(CanonicalConversationModel.id.in_(ids))
            )
        )
