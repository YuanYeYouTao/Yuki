"""A third opaque-ID transport exercises the shared admission and effect pipeline."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.support.service_dependencies import social_service

from yuki.domain.conversations import ScopeType
from yuki.domain.messages import InboundMessage, MessageMention, SenderIdentity
from yuki.gateway.provider import GatewayConnectionProfile, GatewayProviderCatalog
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.gateway.social import TransportReceipt, split_transport_message
from yuki.identity.canonical_repository import (
    create_person_binding,
    create_presence,
    create_space_binding,
)
from yuki.identity.canonical_uow import CanonicalIngressUnitOfWork
from yuki.identity.db_models import AccountModel, CanonicalPersonModel
from yuki.identity.ingress import CanonicalIngressResolver
from yuki.identity.routing import PresenceRouter
from yuki.persistence.models import ChatEventModel
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from yuki.social.service import SocialContext


class Operations:
    def __init__(self):
        self.sent, self.probes = [], []
        self.plans = 0

    def native_mention(self, text, offset):
        return None

    def plan_send(self, message):
        self.plans += 1
        return split_transport_message(message, 16)

    async def is_member(self, handle, space, account):
        self.probes.append(account)
        return True

    async def send(self, handle, target, message):
        self.sent.append((target, message))
        return TransportReceipt(f"opaque-receipt:{len(self.sent)}")

    async def recall(self, handle, target, reference):
        raise AssertionError("not exercised")


class Provider:
    platform, provider_id = "scopedchat", "scopedchat"

    def __init__(self, operations):
        self.social = operations

    def project_inbound(self, message, yuki_ids):
        return message

    def describe_connection(self, handle):
        return GatewayConnectionProfile(
            self.provider_id,
            self.platform,
            handle.self_id,
            frozenset({"send_group", "send_private", "group_member_probe"}),
        )

    def media(self, handle):
        return None

    def activity(self, handle, target):
        return None


@pytest.mark.asyncio
async def test_third_platform_admission_names_exact_accounts_layout_and_ack(database):
    now = datetime.now(UTC)
    native_sender, native_guest = "tenant-A/user:alice", "tenant-A/user:guest"
    handle = SimpleNamespace(self_id="tenant-A/bot:yuki")
    operations = Operations()
    registry = GatewayConnectionRegistry(providers=GatewayProviderCatalog((Provider(operations),)))
    router = PresenceRouter(database, registry)
    async with database.immediate_session() as session:
        sender = await create_person_binding(
            session,
            external_account_id=native_sender,
            display_name="Ice",
            now=now,
            platform="scopedchat",
        )
        presence = await create_presence(
            session, external_account_id=handle.self_id, now=now, platform="scopedchat"
        )
        await create_space_binding(
            session,
            external_space_id="tenant-A/room:main",
            name="Room",
            enabled=True,
            autonomous_enabled=True,
            require_mention=False,
            now=now,
            platform="scopedchat",
        )
    registry.connect(handle, provider_id="scopedchat", presence_id=presence.id)
    resolver = CanonicalIngressResolver(database, registry, router)
    uow = CanonicalIngressUnitOfWork(database, router)
    raw = InboundMessage(
        "source:1",
        "message:scopedchat",
        ScopeType.GROUP,
        SenderIdentity(native_sender, nickname="Ice", nickname_known=True),
        "前😀@Guest 后文",
        platform="scopedchat",
        bot_user_id=handle.self_id,
        group_id="tenant-A/room:main",
        occurred_at=now,
        mentions=(MessageMention("member", 0, 2, 8, native_guest, "Guest"),),
        segments=(
            {
                "type": "native_mention",
                "data": {"target_user_id": native_guest, "display_name": "Guest"},
            },
        ),
    )
    admitted = await resolver.pre_admit(handle, raw)
    assert not admitted.dropped
    appended = await uow.append_inbound(admitted.message, admitted)
    assert appended.created and appended.event.author_person_id == sender.person_id
    snapshot = next(item["data"] for item in appended.event.segments if item["type"] == "mention")
    async with database.read_session() as session:
        guest = await session.scalar(
            select(AccountModel).where(
                AccountModel.platform == "scopedchat",
                AccountModel.external_account_id == native_guest,
            )
        )
        assert snapshot["account_id"] == guest.id and snapshot["person_id"] is None
        assert await session.get(CanonicalPersonModel, guest.id) is None
    social = social_service(database, router, ScopedEventLedgerUnitOfWork(database))
    context = SocialContext(
        "third-turn",
        "natural",
        appended.event.canonical_conversation_id,
        space_id=admitted.space_id,
        inbound=admitted.message,
        trigger_event_id=appended.event.id,
    )
    natural = await social.execute("send", {"text": "前😀@Guest，@Ice看看！后文"}, context)
    assert natural["status"] == "succeeded"
    original_native_targets = [
        item.external_id for _, part in operations.sent for item in part.mentions
    ]
    assert original_native_targets == [native_guest, native_sender]
    assert all(
        part.text[item.text_start : item.text_end] in {"@Guest", "@Ice"}
        for _, part in operations.sent
        for item in part.mentions
    )
    before, plans = len(operations.sent), operations.plans
    assert (
        await social.execute("send", {"text": "前😀@Guest，@Ice看看！后文"}, context)
        == natural
    )
    assert len(operations.sent) == before and operations.plans == plans
    exact = await social.execute(
        "send", {"text": f"@{{account:{guest.id}}}"}, replace(context, call_id="exact")
    )
    assert exact["status"] == "succeeded"
    assert operations.sent[-1][1].mentions[0].account_id == guest.id
    assert operations.sent[-1][1].mentions[0].assignment_id is None
    assert set(operations.probes) <= {handle.self_id}
    async with database.read_session() as session:
        events = list(
            await session.scalars(
                select(ChatEventModel).where(ChatEventModel.direction == "outbound")
            )
        )
    assert len(events) == len(operations.sent)
    assert all(
        event.author_presence_id == presence.id
        and event.canonical_conversation_id == context.conversation_id
        for event in events
    )
