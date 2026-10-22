"""Original ingress receipts retain their author after current account ownership changes."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from sqlalchemy import select
from tests.support.canonical_identity import ensure_presence
from tests.support.client_identity import client_identity_service
from tests.support.user_profiles_helpers import observe_members
from tests.unit.test_canonical_ingress import _Bot, _stack
from tests.unit.test_normalizer import group_event, private_event

from yuki.adapters.onebot.normalizer import normalize_event
from yuki.conversation.canonical_db_models import CanonicalEventReceiptModel
from yuki.conversation.hydrate import ensure_canonical_conversation
from yuki.identity.canonical_repository import find_identity_binding
from yuki.identity.db_models import AccountSpaceProfileModel
from yuki.identity.errors import CanonicalIdentityError
from yuki.identity.receipt_compat import occurred_at_token
from yuki.persistence.models import ChatEventModel


@pytest.mark.parametrize("private", [False, True])
async def test_receipt_replay_after_person_transfer_keeps_original_author_conversation_and_names(
    database, private
):
    registry, resolver, uow = await _stack(database)
    await observe_members(database, ("1001", "1002", "1003"))
    async with database.immediate_session() as session:
        receiver = await ensure_presence(session, "9999")
        if private:
            source = await find_identity_binding(session, "1001")
            await ensure_canonical_conversation(
                session,
                kind="private",
                primary_scope_key="bot:9999:private:1001",
                person_id=source.person_id,
                identity_binding_id=source.id,
            )
    bot = _Bot("9999")
    registry.connect(bot, provider_id="snowluma")
    registry.bind_presence(platform="qq", external_account_id="9999", presence_id=receiver)
    factory = private_event if private else group_event
    native = normalize_event(
        factory(Message([MessageSegment.at(1003), MessageSegment.text(" original question")]))
    )
    first_admission = await resolver.pre_admit(bot, native)
    first = await uow.append_inbound(first_admission.message, first_admission)
    if private:
        assert first.scope.runtime_scope_key.startswith("binding:")
    async with database.immediate_session() as session:
        binding = await find_identity_binding(session, "1001")
        target = await find_identity_binding(session, "1002")
        mentioned = await find_identity_binding(session, "1003")
        target_person = target.person_id
        await client_identity_service(database).transfer_binding(
            session, binding, target_person, now=datetime.now(UTC)
        )
        mentioned.display_name = "renamed target"
        if first_admission.space_id:
            profile = await session.get(
                AccountSpaceProfileModel,
                (mentioned.account_id, first_admission.space_binding_id),
            )
            assert profile is not None
            profile.display_name = ""
        receipt = await session.scalar(
            select(CanonicalEventReceiptModel).where(
                CanonicalEventReceiptModel.canonical_event_id == first.event.canonical_event_id
            )
        )
        original_receipt_id = receipt.id
    current = await resolver.pre_admit(bot, native)
    assert current.author_person_id == first.event.author_person_id != target_person
    assert current.reason == "receipt_replay"
    if private:
        assert current.conversation_id == first.event.canonical_conversation_id
    recovered = await uow.append_inbound(current.message, current)
    assert not recovered.created
    assert occurred_at_token(recovered.event.occurred_at) == occurred_at_token(
        first.event.occurred_at
    )
    assert replace(recovered.event, occurred_at=first.event.occurred_at) == first.event
    assert recovered.scope.id == first.scope.id
    assert recovered.scope.scope.conversation_id == first.scope.id
    assert recovered.scope.runtime_scope_key == first.scope.runtime_scope_key

    # The same transport key cannot conceal changed text, targets, sender, time,
    # reply relation or thread, even though today's binding has changed.
    changed_native = normalize_event(
        factory(Message([MessageSegment.at(1002), MessageSegment.text(" original question")]))
    )
    changed_admission = await resolver.pre_admit(
        bot, replace(changed_native, occurred_at=native.occurred_at)
    )
    attempts = (
        (changed_admission.message, changed_admission),
        (replace(current.message, text=current.message.text + " changed"), current),
        (replace(current.message, sender=replace(current.message.sender, user_id="1002")), current),
        (replace(current.message, occurred_at=native.occurred_at + timedelta(seconds=1)), current),
        (replace(current.message, reply_to_message_id="different-reply"), current),
        (replace(current.message, thread_id="different-thread"), current),
    )
    for message, admitted in attempts:
        with pytest.raises(CanonicalIdentityError) as error:
            await uow.append_inbound(message, admitted)
        assert error.value.category == "receipt_conflict"
    async with database.read_session() as session:
        events = list(await session.scalars(select(ChatEventModel)))
        receipts = list(await session.scalars(select(CanonicalEventReceiptModel)))
        assert len(events) == len(receipts) == 1
        assert events[0].author_person_id == first.event.author_person_id
        assert events[0].canonical_conversation_id == first.scope.id
        assert receipts[0].id == original_receipt_id
        assert receipts[0].canonical_event_id == first.event.canonical_event_id
