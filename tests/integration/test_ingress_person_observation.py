"""Local event metadata shares the original receipt and SQLite transaction."""

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import event, select
from tests.support.memory_identity import transfer_account
from tests.unit.test_canonical_ingress import _Bot, _message, _stack
from tests.unit.test_memory_writer_boundaries import other_writer_and_read_only

from yuki.conversation.canonical_db_models import CanonicalEventReceiptModel
from yuki.domain.messages import SenderIdentity
from yuki.identity import canonical_uow
from yuki.identity.canonical_repository import (
    ensure_person,
    ensure_presence,
    find_identity_binding,
    set_identity_failpoint,
)
from yuki.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    SpaceBindingModel,
)
from yuki.identity.errors import CanonicalIdentityError
from yuki.identity.person_identity import merge_persons
from yuki.persistence.models import ChatEventModel, MembershipModel, PersonAliasModel


@pytest.fixture
async def ingress(database):
    registry, resolver, uow = await _stack(database)
    bot = _Bot("8000")
    async with database.sessions() as session:
        presence = await ensure_presence(session, "8000")
    registry.connect(bot, provider_id="snowluma")
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    return bot, resolver, uow


async def admitted(ingress, message_id, *, user="1001", name="Event nickname", card="Event card"):
    bot, resolver, _ = ingress
    message = replace(
        _message(message_id=message_id, user_id=user, group_id="2001"),
        sender=SenderIdentity(
            user, nickname=name, nickname_known=True, group_card=card, group_card_known=True
        ),
        group_name="Event group title",
    )
    result = await resolver.pre_admit(bot, message)
    assert not result.dropped
    return result


async def observation(database, person_id, space_id):
    async with database.sessions() as session:
        person = await session.get(CanonicalPersonModel, person_id)
        accounts = (
            await session.execute(
                select(
                    IdentityBindingModel.display_name,
                    IdentityBindingModel.last_seen_at,
                    IdentityBindingModel.revision,
                ).where(IdentityBindingModel.person_id == person_id)
            )
        ).all()
        membership = await session.get(MembershipModel, (person_id, space_id))
        aliases = (
            await session.execute(
                select(
                    PersonAliasModel.canonical_space_id,
                    PersonAliasModel.alias,
                    PersonAliasModel.first_seen_at,
                    PersonAliasModel.last_seen_at,
                ).where(PersonAliasModel.canonical_person_id == person_id)
            )
        ).all()
        space = await session.get(CanonicalSpaceModel, space_id)
        group = await session.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.space_id == space_id)
        )
        return {
            "person": (person.display_name, person.revision),
            "accounts": accounts,
            "membership": (membership.group_card, membership.first_seen_at, membership.last_seen_at)
            if membership
            else None,
            "aliases": aliases,
            "space": (space.name, space.revision),
            "group": (group.display_name, group.last_seen_at, group.revision),
        }


async def test_first_message_fills_empty_person_name_without_following_platform_renames(
    database, ingress
):
    async with database.immediate_session() as session:
        person_id = await ensure_person(session, "61073")
    first = await admitted(ingress, "first-profile", user="61073", name="First name")
    _, _, uow = ingress
    original = await uow.append_inbound(first.message, first)
    second = await admitted(ingress, "renamed-profile", user="61073", name="Renamed account")
    await uow.append_inbound(second.message, second)
    state = await observation(database, person_id, first.space_id)
    assert state["person"][0] == "First name"
    assert state["accounts"][0][0] == "Renamed account"
    assert {row[1] for row in state["aliases"]} == {"First name", "Renamed account", "Event card"}
    assert state["membership"][0] == "Event card"
    assert state["space"][0] == state["group"][0] == "Event group title"
    async with database.sessions() as session:
        row = await session.get(ChatEventModel, original.event.id)
        assert row.author_person_id == person_id
        assert row.sender_nickname == "First name"
        assert row.sender_group_card == "Event card"


async def test_duplicate_metadata_is_readonly_and_does_not_touch_observation(database, ingress):
    incoming = await admitted(ingress, "readonly-profile")
    _, _, uow = ingress
    original = await uow.append_inbound(incoming.message, incoming)
    before = await observation(database, incoming.person_id, incoming.space_id)
    changed = replace(
        incoming.message,
        sender=replace(incoming.message.sender, nickname="Late nickname", group_card=""),
        group_name="Late title",
    )
    async with other_writer_and_read_only(database):
        duplicate = await uow.append_inbound(changed, incoming)
    assert not duplicate.created and duplicate.event.id == original.event.id
    assert await observation(database, incoming.person_id, incoming.space_id) == before


async def test_external_bot_title_is_local_metadata_without_a_person_or_membership(
    database, ingress
):
    bot, resolver, uow = ingress
    message = replace(
        _message(message_id="external-title", user_id="7777", group_id="2001", is_bot=True),
        group_name="Observed title",
    )
    incoming = await resolver.pre_admit(bot, message)
    assert not incoming.dropped and incoming.person_id is None
    async with database.sessions() as session:
        persons = tuple(await session.scalars(select(CanonicalPersonModel.id)))
    await uow.append_inbound(incoming.message, incoming)
    following = await resolver.pre_admit(
        bot, replace(message, message_id="external-empty-title", group_name="")
    )
    await uow.append_inbound(following.message, following)
    async with database.sessions() as session:
        assert tuple(await session.scalars(select(CanonicalPersonModel.id))) == persons
        assert not list(await session.scalars(select(MembershipModel)))
        space = await session.get(CanonicalSpaceModel, incoming.space_id)
        binding = await session.get(SpaceBindingModel, incoming.space_binding_id)
        assert space.name == binding.display_name == "Observed title"
        assert space.enabled and space.require_mention


async def test_concurrent_original_receipt_only_applies_winners_names(
    database, ingress, monkeypatch
):
    incoming = await admitted(ingress, "competing-profile")
    _, _, uow = ingress
    original = canonical_uow._prepare_observation
    ready, release = asyncio.Event(), asyncio.Event()
    prepared = 0

    async def prepare(*args):
        nonlocal prepared
        result = await original(*args)
        prepared += 1
        if prepared <= 2:
            if prepared == 2:
                ready.set()
            await release.wait()
        return result

    monkeypatch.setattr(canonical_uow, "_prepare_observation", prepare)
    other = replace(
        incoming.message, sender=replace(incoming.message.sender, nickname="Other winner")
    )
    pending = [
        asyncio.create_task(uow.append_inbound(message, incoming))
        for message in (incoming.message, other)
    ]
    try:
        await asyncio.wait_for(ready.wait(), 10)
    finally:
        release.set()
    results = await asyncio.wait_for(asyncio.gather(*pending), 10)
    assert sum(result.created for result in results) == 1
    assert results[0].event.id == results[1].event.id
    winner = next(result.event for result in results if result.created)
    state = await observation(database, incoming.person_id, incoming.space_id)
    assert state["accounts"][0][0] == winner.sender_nickname
    assert {row[1] for row in state["aliases"]} == {winner.sender_nickname, "Event card"}


async def pause_observation(monkeypatch):
    original = canonical_uow._prepare_observation
    entered, release = asyncio.Event(), asyncio.Event()

    async def prepare(*args):
        result = await original(*args)
        if not entered.is_set():
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(canonical_uow, "_prepare_observation", prepare)
    return entered, release


async def test_merge_during_read_reprepares_current_name_but_keeps_original_actor(
    database, ingress, monkeypatch
):
    incoming = await admitted(ingress, "merge-profile")
    _, _, uow = ingress
    async with database.sessions() as session:
        target = (await find_identity_binding(session, "1002")).person_id
    entered, release = await pause_observation(monkeypatch)
    pending = asyncio.create_task(uow.append_inbound(incoming.message, incoming))
    try:
        await asyncio.wait_for(entered.wait(), 10)

        async def merge(session):
            return await merge_persons(session, incoming.person_id, target)

        await database.apply_snapshot_write(merge)
    finally:
        release.set()
    result = await asyncio.wait_for(pending, 10)
    async with database.sessions() as session:
        assert (await session.get(CanonicalPersonModel, target)).display_name == "test-1002"
        assert (await find_identity_binding(session, "1001")).person_id == incoming.person_id
        row = await session.get(ChatEventModel, result.event.id)
        assert row.author_person_id == incoming.person_id
        assert row.canonical_conversation_id == incoming.conversation_id
        assert row.ingress_presence_id == incoming.presence_id
    state = await observation(database, incoming.person_id, incoming.space_id)
    assert state["accounts"][0][0] == "Event nickname"
    assert state["membership"][0] == "Event card"


async def test_transfer_during_read_cannot_write_late_observation_to_new_person(
    database, ingress, monkeypatch
):
    incoming = await admitted(ingress, "transferred-profile")
    _, _, uow = ingress
    entered, release = await pause_observation(monkeypatch)
    pending = asyncio.create_task(uow.append_inbound(incoming.message, incoming))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        transferred = await transfer_account(database, "1001")
    finally:
        release.set()
    with pytest.raises(CanonicalIdentityError) as failure:
        await asyncio.wait_for(pending, 10)
    assert failure.value.category == "canonical_owner_mismatch"
    async with database.sessions() as session:
        assert not list(await session.scalars(select(ChatEventModel)))
        assert not list(await session.scalars(select(CanonicalEventReceiptModel)))
        assert not list(
            await session.scalars(
                select(PersonAliasModel).where(
                    PersonAliasModel.alias.in_(("Event nickname", "Event card"))
                )
            )
        )
        assert await session.get(MembershipModel, (incoming.person_id, incoming.space_id)) is None
        assert (
            await session.get(MembershipModel, (transferred["person_id"], incoming.space_id))
            is None
        )


@pytest.mark.parametrize("failure", ["after_receipt_claim", "after_canonical_append"])
async def test_failure_rolls_back_original_receipt_event_and_observation(
    database, ingress, failure
):
    incoming = await admitted(ingress, "rollback-profile")
    _, _, uow = ingress
    before = await observation(database, incoming.person_id, incoming.space_id)

    def fail(stage):
        if stage == failure:
            raise RuntimeError("original transaction failed")

    set_identity_failpoint(fail)
    try:
        with pytest.raises(RuntimeError, match="original transaction failed"):
            await uow.append_inbound(incoming.message, incoming)
    finally:
        set_identity_failpoint(None)
    assert await observation(database, incoming.person_id, incoming.space_id) == before
    async with database.sessions() as session:
        assert not list(await session.scalars(select(ChatEventModel)))
        assert not list(await session.scalars(select(CanonicalEventReceiptModel)))


async def test_caller_session_owns_event_and_observation_commit(database, ingress):
    incoming = await admitted(ingress, "caller-profile")
    _, _, uow = ingress
    before = await observation(database, incoming.person_id, incoming.space_id)

    async def caller(session):
        await uow.append_inbound(incoming.message, incoming, session=session, origin="work_input")
        raise RuntimeError("caller rollback")

    with pytest.raises(RuntimeError, match="caller rollback"):
        await database.apply_snapshot_write(caller)
    assert await observation(database, incoming.person_id, incoming.space_id) == before
    assert (await uow.append_inbound(incoming.message, incoming)).created


async def test_person_observation_reads_precede_first_database_write(database, ingress):
    incoming = await admitted(ingress, "prepared-profile")
    _, _, uow = ingress
    writing = False
    late_reads = []

    def inspect(_connection, _cursor, statement, *_args):
        nonlocal writing
        sql = statement.strip().lower()
        if sql.startswith(("insert", "update", "delete", "begin immediate")):
            writing = True
        if (
            writing
            and sql.startswith("select")
            and any(
                f"from {table}" in sql
                for table in ("persons", "identity_bindings", "memberships", "person_aliases")
            )
        ):
            late_reads.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", inspect)
    try:
        assert (await uow.append_inbound(incoming.message, incoming)).created
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", inspect)
    assert writing and not late_reads
