"""Native time and stable receiving Account preserve the first original fact."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from tests.support.canonical_identity import ensure_person, ensure_presence
from tests.unit.test_canonical_ingress import _Bot, _message, _stack

from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalEventReceiptModel,
)
from yuki.identity.canonical_repository import (
    ensure_account,
    find_identity_binding,
    replace_assignment,
)
from yuki.identity.db_models import AccountAssignmentModel, CanonicalPersonModel, PresenceModel
from yuki.identity.errors import CanonicalIdentityError
from yuki.identity.event_author import project_event_authors
from yuki.identity.ownership_interval import historical_person_ids
from yuki.persistence.models import ChatEventModel

T0 = datetime(2026, 10, 18, 10, tzinfo=UTC)


async def _case(database):
    registry, resolver, uow = await _stack(database)
    bot = _Bot("8000")
    async with database.immediate_session() as writer:
        presence = await ensure_presence(writer, "8000")
        source, target = await ensure_person(writer, "1001"), await ensure_person(writer, "1002")
        account = await ensure_account(writer, "92001", platform="qq", now=T0)
        old = await replace_assignment(
            writer, account, None, await writer.get(CanonicalPersonModel, source), now=T0
        )
        new = await replace_assignment(
            writer,
            account,
            old.id,
            await writer.get(CanonicalPersonModel, target),
            now=T0 + timedelta(minutes=10),
        )
        account_id, old_id, new_id = account.id, old.id, new.id
    registry.connect(bot, provider_id="snowluma")
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    return registry, resolver, uow, bot, presence, source, target, account_id, old_id, new_id


async def test_first_late_event_uses_original_person_and_stage_without_new_owner_entry(database):
    _, resolver, uow, bot, _, source, target, _, old_id, new_id = await _case(database)
    native = replace(
        _message(message_id="first-late", user_id="92001"), occurred_at=T0 + timedelta(minutes=5)
    )
    admitted = await resolver.pre_admit(bot, native)
    assert not admitted.dropped and admitted.author_person_id == source
    assert admitted.author_assignment_id == old_id
    result = await uow.append_inbound(admitted.message, admitted)
    assert result.event.author_person_id == source
    async with database.read_session() as reader:
        conversation = await reader.get(
            CanonicalConversationModel, result.event.canonical_conversation_id
        )
        assert conversation.person_id == source and conversation.identity_binding_id == old_id
        assert (
            await reader.scalar(
                select(CanonicalConversationModel.id).where(
                    CanonicalConversationModel.identity_binding_id == new_id
                )
            )
            is None
        )
        assert (await reader.get(AccountAssignmentModel, new_id)).person_id == target
        assert await historical_person_ids(reader, "qq", ("92001",), native.occurred_at) == {
            "92001": source
        }
    repeated = await resolver.pre_admit(bot, native)
    assert repeated.reason == "receipt_replay" and repeated.author_person_id == source
    assert not (await uow.append_inbound(repeated.message, repeated)).created


@pytest.mark.parametrize("unknown", ["before_first_seen", "unknown_end", "unknown_current_start"])
async def test_unproved_private_author_is_one_account_fact_and_payload_conflict_stays_conflict(
    database, unknown
):
    _, resolver, uow, bot, _, _, target, account_id, old_id, new_id = await _case(database)
    native_time = T0 - timedelta(minutes=1)
    async with database.immediate_session() as writer:
        if unknown == "unknown_end":
            (await writer.get(AccountAssignmentModel, old_id)).ended_at = None
            native_time = T0 + timedelta(minutes=5)
        elif unknown == "unknown_current_start":
            (await writer.get(AccountAssignmentModel, new_id)).started_at = None
            (await writer.get(AccountAssignmentModel, old_id)).ended_at = None
            native_time = T0 + timedelta(minutes=5)
    native = replace(
        _message(message_id="unknown-original", user_id="92001", text="original account fact"),
        occurred_at=native_time,
    )
    admitted = await resolver.pre_admit(bot, native)
    assert not admitted.dropped and admitted.author_kind == "account"
    assert admitted.author_account_id == account_id and admitted.conversation_id is None
    fact = await uow.record_account_fact(admitted.message, admitted)
    assert fact.author_kind == "account" and fact.author_person_id is None
    repeated = await resolver.pre_admit(
        bot, replace(native, received_at=native.received_at + timedelta(hours=1))
    )
    assert repeated.reason == "receipt_replay"
    assert (await uow.record_account_fact(repeated.message, repeated)).id == fact.id
    forged = replace(repeated.message, text="different payload under the old native ID")
    with pytest.raises(CanonicalIdentityError) as conflict:
        await uow.record_account_fact(forged, repeated)
    assert conflict.value.category == "receipt_conflict"
    async with database.read_session() as reader:
        assert (
            await reader.scalar(select(func.count()).select_from(CanonicalEventReceiptModel)) == 1
        )
        assert await reader.scalar(select(func.count()).select_from(ChatEventModel)) == 1
        assert (
            await reader.scalar(
                select(CanonicalConversationModel.id).where(
                    CanonicalConversationModel.identity_binding_id.in_((old_id, new_id))
                )
            )
            is None
        )
        assert (await reader.get(AccountAssignmentModel, new_id)).person_id == target


async def test_known_ended_stage_does_not_permanently_block_a_new_normal_person(database):
    _, resolver, uow, bot, _, source, target, _, _, new_id = await _case(database)
    async with database.immediate_session() as writer:
        current = await writer.get(AccountAssignmentModel, new_id)
        await replace_assignment(
            writer, current.account, current.id, None, now=T0 + timedelta(minutes=20)
        )
    native = replace(
        _message(message_id="after-all-known-ends", user_id="92001"),
        occurred_at=T0 + timedelta(minutes=30),
    )
    admitted = await resolver.pre_admit(bot, native)
    assert not admitted.dropped and admitted.author_kind == "person"
    assert admitted.author_person_id not in {source, target, None}
    assert admitted.author_assignment_id not in {new_id, None}
    event = await uow.append_inbound(admitted.message, admitted)
    assert event.event.author_person_id == admitted.author_person_id
    async with database.read_session() as reader:
        current = await find_identity_binding(reader, "92001")
        assert current.id == admitted.author_assignment_id and current.status == "active"


async def test_yuki_role_replacement_deduplicates_by_stable_receiver_account(database):
    registry, resolver, uow, bot, old_presence, _, _, _, _, _ = await _case(database)
    native = _message(message_id="stable-receiver", user_id="1001")
    admitted = await resolver.pre_admit(bot, native)
    original = await uow.append_inbound(admitted.message, admitted)
    registry.disconnect(bot)
    async with database.immediate_session() as writer:
        old = await writer.get(PresenceModel, old_presence)
        person = await writer.get(CanonicalPersonModel, await ensure_person(writer, "1003"))
        intermediate = await replace_assignment(writer, old.account, old.id, person, now=T0)
        current = await replace_assignment(
            writer, old.account, intermediate.id, "yuki", now=T0 + timedelta(minutes=1)
        )
        current_id = current.id
    registry.connect(bot, provider_id="snowluma")
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=current_id)
    replay = await resolver.pre_admit(bot, native)
    assert replay.reason == "receipt_replay" and replay.presence_id == old_presence
    reused = await uow.append_inbound(replay.message, replay)
    assert not reused.created and reused.event.id == original.event.id
    async with database.read_session() as reader:
        assert (
            await reader.scalar(select(func.count()).select_from(CanonicalEventReceiptModel)) == 1
        )
        assert await reader.scalar(select(func.count()).select_from(ChatEventModel)) == 1
        receipt = await reader.scalar(select(CanonicalEventReceiptModel))
        assert receipt.ingress_presence_id == old_presence
        assert (
            receipt.ingress_account_id == (await reader.get(PresenceModel, current_id)).account_id
        )


async def test_current_disabled_stage_does_not_create_another_owner(database):
    _, resolver, _, bot, _, _, target, _, _, new_id = await _case(database)
    async with database.immediate_session() as writer:
        (await writer.get(AccountAssignmentModel, new_id)).status = "disabled"
    native = replace(
        _message(message_id="disabled-owner", user_id="92001"),
        occurred_at=T0 + timedelta(minutes=30),
    )
    admitted = await resolver.pre_admit(bot, native)
    assert admitted.dropped and admitted.reason == "private_not_allowed"
    async with database.read_session() as reader:
        original = await find_identity_binding(reader, "92001")
        assert original.id == new_id and original.person_id == target
        with pytest.raises(CanonicalIdentityError):
            await project_event_authors(
                reader, sender_user_ids=("92001",), occurred_at=native.occurred_at
            )


async def test_account_original_reply_cannot_become_yuki_from_a_native_hint(database):
    _, resolver, uow, bot, _, _, _, account_id, _, _ = await _case(database)
    original = replace(
        _message(message_id="group-account-fact", user_id="92001", group_id="2001"),
        occurred_at=T0 - timedelta(minutes=1),
    )
    admission = await resolver.pre_admit(bot, original)
    assert admission.author_kind == "account" and admission.conversation_id is not None
    event = await uow.append_inbound(admission.message, admission)
    assert event.event.author_account_id == account_id
    reply = await resolver.pre_admit(
        bot,
        _message(
            message_id="reply-to-account",
            group_id="2001",
            reply_to_message_id=original.native_message_id,
            reply_sender_user_id="8000",
        ),
    )
    assert reply.message.reply_to_event_id == event.event.id
    assert reply.message.canonical_reply_to_yuki is False
    assert reply.message.canonical_reply_author_kind == "account"
