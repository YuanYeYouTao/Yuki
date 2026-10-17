"""Personal code possession proves identity; device tokens never grant operator rights."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from yuki.client_api.db_models import ClientDeviceModel, ClientLinkCodeModel
from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.hydrate import ensure_canonical_conversation
from yuki.domain.conversations import ConversationScope
from yuki.identity.canonical_repository import find_identity_binding, find_presence
from yuki.identity.db_models import IdentityBindingModel
from yuki.identity.person_identity import current_person_id
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


async def code_for(database, account="1001"):
    service = ClientIdentityService(database)
    async with database.sessions() as session:
        binding = await find_identity_binding(session, account)
    issued = await service.issue_for_operator(binding.person_id, str(uuid4()), str(uuid4()))
    return service, binding.person_id, issued


async def test_pair_is_one_time_two_devices_share_entrance_and_revocation_is_independent(database):
    service, person, issued = await code_for(database)
    first = await service.pair(issued["code"], "computer")
    with pytest.raises(ClientError, match="unauthenticated"):
        await service.pair(issued["code"], "reused")
    a = await service.authenticate(first["token"])
    next_code = await service.issue_for_device(a, str(uuid4()))
    second = await service.pair(next_code["code"], "phone")
    b = await service.authenticate(second["token"])
    assert a.person_id == b.person_id == person
    assert a.conversation_id == b.conversation_id and a.binding_id == b.binding_id
    assert a.device_id != b.device_id and first["token"] != second["token"]
    me = await service.me(b)
    assert len(me["devices"]) == 2
    await service.revoke(b, a.device_id, str(uuid4()))
    with pytest.raises(ClientError, match="unauthenticated"):
        await service.authenticate(first["token"])
    assert (await service.authenticate(second["token"])).person_id == person
    async with database.sessions() as session:
        contents = " ".join(
            str(row)
            for row in await session.execute(
                text(
                    "SELECT effective_state_json FROM control_command_receipts UNION ALL "
                    "SELECT after_json FROM admin_operation_events"
                )
            )
        )
        assert all(
            secret not in contents
            for secret in (issued["code"], next_code["code"], first["token"], second["token"])
        )
        assert (await session.get(ClientDeviceModel, a.device_id)).token_hash != first["token"]


async def test_expired_codes_and_other_person_devices_do_not_disclose_identity(database):
    service, _, issued = await code_for(database)
    async with database.immediate_session() as session:
        row = await session.scalar(select(ClientLinkCodeModel))
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    for raw in (issued["code"], "unknown-code"):
        with pytest.raises(ClientError, match="unauthenticated"):
            await service.pair(raw, "invalid")
    _, _, other = await code_for(database, "1002")
    paired = await service.pair(other["code"], "other person")
    principal = await service.authenticate(paired["token"])
    with pytest.raises(ClientError, match="not_found"):
        await service.revoke(principal, str(uuid4()), str(uuid4()))


async def test_account_claim_merges_person_and_preserves_original_binding_and_history(database):
    service, target, issued = await code_for(database, "1002")
    writer = ScopedEventLedgerUnitOfWork(database)
    old_event = (
        await writer.append(
            scope=ConversationScope.private("8000", "1001"),
            platform_message_id="old-owner",
            sender_user_id="1001",
            direction="inbound",
            content="old person's private history",
        )
    ).event
    async with database.sessions() as session:
        original = await find_identity_binding(session, "1001")
        old_binding, old_person = original.id, original.person_id
    linked = await service.claim_account(
        issued["code"],
        platform="qq",
        account="1001",
        display_name="same name",
        message_id="link-request",
    )
    assert linked["merged"] and not linked["transferred"] and linked["person_id"] == target
    async with database.sessions() as session:
        old = await session.get(IdentityBindingModel, old_binding)
        current = await find_identity_binding(session, "1001")
        assert old.person_id == old_person and old.status == "active"
        assert current.id == old.id and current.person_id == old_person
        assert await current_person_id(session, old_person) == target
        assert (
            await session.get(CanonicalConversationModel, old_event.canonical_conversation_id)
        ).person_id == old_person
        assert (
            len(
                list(
                    await session.scalars(
                        select(CanonicalConversationModel).where(
                            CanonicalConversationModel.identity_binding_id == current.id
                        )
                    )
                )
            )
            == 1
        )
    new_event = (
        await writer.append(
            scope=ConversationScope.private("8000", "1001"),
            platform_message_id="new-owner",
            sender_user_id="1001",
            direction="inbound",
            content="new person's private history",
        )
    ).event
    async with database.sessions() as session:
        entry = await session.get(CanonicalConversationModel, new_event.canonical_conversation_id)
        assert entry.person_id == old_person and entry.identity_binding_id == current.id
        assert entry.id == old_event.canonical_conversation_id
        assert new_event.author_person_id == old_event.author_person_id == old_person
        assert await current_person_id(session, new_event.author_person_id) == target
    with pytest.raises(ClientError, match="unauthenticated"):
        await service.claim_account(
            issued["code"],
            platform="qq",
            account="1001",
            display_name="",
            message_id="second-exchange",
        )


async def test_same_owner_transfer_does_not_choose_one_of_multiple_presence_entries(database):
    service, person_id, _issued = await code_for(database)
    async with database.immediate_session() as session:
        binding = await find_identity_binding(session, "1001")
        originals = []
        for account in ("8000", "8001"):
            presence = await find_presence(session, account)
            entry = await ensure_canonical_conversation(
                session,
                kind="private",
                primary_scope_key=f"bot:{account}:private:1001",
                identity_binding_id=binding.id,
                person_id=person_id,
                presence_id=presence.id,
            )
            originals.append(entry.conversation_id)
    issuer, request = str(uuid4()), str(uuid4())
    changed = await service.transfer_for_operator(binding.id, person_id, issuer, request)
    assert changed["binding_id"] == changed["old_binding_id"] == binding.id
    assert "conversation_id" not in changed
    assert (await service.transfer_for_operator(binding.id, person_id, issuer, request))["replay"]
    async with database.sessions() as session:
        rows = list(
            await session.scalars(
                select(CanonicalConversationModel).where(
                    CanonicalConversationModel.identity_binding_id == binding.id
                )
            )
        )
        assert sorted(row.id for row in rows) == sorted(originals)
