"""A physical ACK and its duplicate retain the original sending stage."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select, update
from tests.integration.test_person_execution_receipts import invocation
from tests.support.social_identity_cases import social_env

from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalEventReceiptModel,
)
from yuki.domain.conversations import ConversationScope
from yuki.gateway.social import TransportMessage, transport_payload
from yuki.identity.canonical_repository import (
    find_identity_binding,
    find_space_binding,
    replace_assignment,
)
from yuki.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    PresenceModel,
    SpaceBindingModel,
)
from yuki.identity.errors import CanonicalIdentityError
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.persistence.models import ChatEventModel
from yuki.plugin_host.facades import HostPluginContext, PluginFacadeServices
from yuki.plugin_host.media_artifacts import PluginMediaArtifactStore
from yuki.plugin_host.notification_delivery import (
    NotificationDeliveryReceipt,
    PluginNotificationOutboxWorker,
)
from yuki.plugin_host.notification_repository import OutboxRecord, PluginNotificationRepository
from yuki.services.effect_gate import ConversationEffectGate
from yuki.social.db_models import SocialOperationModel
from yuki_plugin_sdk.permissions import PluginPermission


async def _replace_sender(database, env, owner):
    async with database.immediate_session() as writer:
        old = await writer.get(PresenceModel, env.presence)
        account = old.account
        await replace_assignment(writer, account, old.id, None, now=datetime.now(UTC))
        if owner != "ended":
            await replace_assignment(
                writer,
                account,
                None,
                "yuki" if owner == "yuki" else await writer.get(CanonicalPersonModel, env.person),
                now=datetime.now(UTC),
            )


async def _repeat_original(database, env, sent):
    async with database.read_session() as reader:
        row = await reader.get(ChatEventModel, sent["event_id"])
        original = dict(
            (
                await reader.execute(
                    select(ChatEventModel.__table__).where(ChatEventModel.id == row.id)
                )
            )
            .mappings()
            .one()
        )
        receipt = await reader.scalar(
            select(CanonicalEventReceiptModel).where(
                CanonicalEventReceiptModel.canonical_event_id == row.canonical_event_id
            )
        )
        receipt_id = receipt.id
    writes = []

    def observe(_connection, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", observe)
    try:
        repeated = await env.service.writer.append(
            scope=ConversationScope.group(
                row.bot_user_id,
                row.group_id,
                platform="qq",
                presence_id=env.presence,
                conversation_id=row.canonical_conversation_id,
            ),
            platform_message_id=row.platform_message_id,
            sender_user_id=row.sender_user_id,
            direction="outbound",
            content=row.content,
            segments=tuple(json.loads(row.segments_json)),
            origin=row.origin,
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", observe)
    assert repeated.event.id == row.id and not repeated.created
    assert not writes
    async with database.read_session() as reader:
        assert (
            dict(
                (
                    await reader.execute(
                        select(ChatEventModel.__table__).where(ChatEventModel.id == row.id)
                    )
                )
                .mappings()
                .one()
            )
            == original
        )
        assert (
            await reader.scalar(
                select(CanonicalEventReceiptModel.id).where(
                    CanonicalEventReceiptModel.canonical_event_id == row.canonical_event_id
                )
            )
            == receipt_id
        )


@pytest.mark.parametrize("owner", ["ended", "person", "yuki"])
async def test_inflight_confirmed_send_retains_original_presence(
    database, tmp_path, monkeypatch, owner
):
    env = await social_env(database, tmp_path)
    original_call = env.bot.call_api

    async def original_ack(action, **params):
        if action == "send_group_msg":
            await _replace_sender(database, env, owner)
        return await original_call(action, **params)

    monkeypatch.setattr(env.bot, "call_api", original_ack)
    sent = await env.service.execute("send", {"text": "原发送确认"}, env.context)
    assert sent["status"] == "succeeded"
    async with database.read_session() as reader:
        row = await reader.get(ChatEventModel, sent["event_id"])
        assert row.author_kind == "yuki"
        assert row.author_presence_id == row.ingress_presence_id == env.presence
        assert row.author_person_id is None and row.author_account_id is None
    await _repeat_original(database, env, sent)
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1


@pytest.mark.parametrize("owner", ["ended", "person", "yuki"])
async def test_existing_receipt_keeps_its_original_buggy_author_bytes(database, tmp_path, owner):
    env = await social_env(database, tmp_path)
    sent = await env.service.execute("send", {"text": "已入账原文"}, env.context)
    async with database.immediate_session() as writer:
        # An already accepted fact from the retired writer, before this fix.
        await writer.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == sent["event_id"])
            .values(author_kind="external_bot", author_presence_id=None)
        )
    await _replace_sender(database, env, owner)
    await _repeat_original(database, env, sent)
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1


async def test_onebot_private_ack_keeps_the_binding_prepared_before_transfer(database, tmp_path):
    env = await social_env(database, tmp_path)
    async with database.immediate_session() as writer:
        (await writer.get(CanonicalPersonModel, env.person)).is_superuser = True
        old = await find_identity_binding(writer, "10002")
        old_binding_id, old_person_id = old.id, old.person_id
        other = await find_identity_binding(writer, "1003")
        new_person_id = other.person_id
        source = await writer.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "inbound")
        )
    calls = []

    class Gateway:
        async def call_api(self, action, params):
            calls.append((action, params))
            async with database.immediate_session() as writer:
                old = await find_identity_binding(writer, "10002")
                await replace_assignment(
                    writer,
                    old.account,
                    old.id,
                    await writer.get(CanonicalPersonModel, new_person_id),
                    now=datetime.now(UTC),
                )
            return {"message_id": "original-native-private-ack"}

    host = HostPluginContext(
        plugin_id="test.prepared-onebot-entry",
        approved_permissions=(PluginPermission.ONEBOT_SEND,),
        services=PluginFacadeServices(ledger=EventLedgerRepository(database), social=env.service),
    )
    original = await invocation(
        database,
        host.plugin_id,
        source,
        platform="qq",
        space_id=env.space,
        gateway=Gateway(),
    )
    try:
        with host.bind(original):
            sent = await host.onebot.send_private("10002", "原账号换人前已发出")
        assert sent.ok and len(calls) == 1
        async with database.read_session() as reader:
            receipt = await reader.get(SocialOperationModel, sent.data["operation_id"])
            message = await reader.get(ChatEventModel, receipt.event_id)
            conversation = await reader.get(
                CanonicalConversationModel, message.canonical_conversation_id
            )
            assert conversation.identity_binding_id == old_binding_id
            assert conversation.person_id == old_person_id != new_person_id
            assert json.loads(receipt.prepared_json)["binding_id"] == old_binding_id
            assert message.author_presence_id == message.ingress_presence_id == env.presence
        with host.bind(original):
            duplicate = await host.onebot.send_private("10002", "原账号换人前已发出")
        assert duplicate.data["operation_id"] == sent.data["operation_id"]
        assert len(calls) == 1
    finally:
        await host.close_host_resources()


@pytest.mark.parametrize("paused", ["space", "binding"])
async def test_onebot_cross_group_ack_keeps_prepared_entry_when_paused(database, tmp_path, paused):
    env = await social_env(database, tmp_path)
    async with database.immediate_session() as writer:
        (await writer.get(CanonicalPersonModel, env.person)).is_superuser = True
        entry = await find_space_binding(writer, "2002")
        binding_id, space_id = entry.id, entry.space_id
        source = await writer.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "inbound")
        )
    calls = []

    class Gateway:
        async def call_api(self, action, params):
            calls.append((action, params))
            async with database.immediate_session() as writer:
                if paused == "space":
                    (await writer.get(CanonicalSpaceModel, space_id)).enabled = False
                else:
                    (await writer.get(SpaceBindingModel, binding_id)).status = "disabled"
            return {"message_id": "original-native-group-ack"}

    host = HostPluginContext(
        plugin_id="test.prepared-onebot-group",
        approved_permissions=(PluginPermission.ONEBOT_SEND,),
        services=PluginFacadeServices(ledger=EventLedgerRepository(database), social=env.service),
    )
    original = await invocation(
        database,
        host.plugin_id,
        source,
        platform="qq",
        space_id=env.space,
        gateway=Gateway(),
    )
    try:
        with host.bind(original):
            sent = await host.onebot.send_group("2002", "群暂停前已发出的原消息")
        assert sent.ok and len(calls) == 1
        async with database.read_session() as reader:
            receipt = await reader.get(SocialOperationModel, sent.data["operation_id"])
            message = await reader.get(ChatEventModel, receipt.event_id)
            assert message is not None
            conversation = await reader.get(
                CanonicalConversationModel, message.canonical_conversation_id
            )
            assert conversation.space_id == space_id and conversation.space_binding_id == binding_id
            assert json.loads(receipt.prepared_json)["binding_id"] == binding_id
            assert message.author_presence_id == message.ingress_presence_id == env.presence
        with host.bind(original):
            duplicate = await host.onebot.send_group("2002", "群暂停前已发出的原消息")
        assert duplicate.data["operation_id"] == sent.data["operation_id"]
        assert len(calls) == 1
    finally:
        await host.close_host_resources()


async def test_notification_record_reuses_existing_author_fact_without_rechecking_it(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    scope = ConversationScope.group(
        env.bot.self_id,
        "20001",
        presence_id=env.presence,
        conversation_id=env.context.conversation_id,
    )
    external = await env.service.writer.append_external(
        scope=scope,
        platform_message_id="original-external",
        source_plugin_id="test.confirmed-notification",
        external_source="test",
        external_event_key="original-key",
        external_event_type="updated",
        external_payload={},
        external_target_id="20001",
        content="original event",
        occurred_at=datetime.now(UTC),
    )
    item = OutboxRecord(
        id=1,
        notification_id="original-notification",
        source_event_id=external.event.id,
        plugin_id="test.confirmed-notification",
        target_type="group",
        target_id="20001",
        bot_user_id=env.bot.self_id,
        part_type="text",
        text="原通知正文",
        media_handle_id=None,
        attempts=1,
        canonical_target_person_id=None,
        canonical_target_space_id=env.space,
        canonical_presence_id=env.presence,
        canonical_conversation_id=env.context.conversation_id,
        source_person_id=env.person,
        prepared={"message": transport_payload(TransportMessage("原通知正文"))},
    )
    receipt = NotificationDeliveryReceipt(
        message_id="original-notification-ack",
        sender_account_id=env.bot.self_id,
        external_target_id="20001",
        route_kind="conversation",
        presence_id=env.presence,
        conversation_id=env.context.conversation_id,
    )
    transport = AsyncMock()
    worker = PluginNotificationOutboxWorker(
        repository=PluginNotificationRepository(database),
        artifacts=PluginMediaArtifactStore(database, tmp_path / "plugin-media"),
        ledger=EventLedgerRepository(database),
        transport=transport,
        effect_gate=ConversationEffectGate(),
    )
    from yuki.plugin_host.db_models import PluginInstallationModel, PluginNotificationOutboxModel
    from yuki_plugin_sdk.api import PLUGIN_API_VERSION

    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        writer.add(
            PluginInstallationModel(
                plugin_id=item.plugin_id,
                name="notification fixture",
                version="1.0.0",
                plugin_api=PLUGIN_API_VERSION,
                yuki_requires="*",
                manifest_hash="a" * 64,
                entrypoint="fixture:Plugin",
                status="running",
                enabled=True,
                discovered_at=now,
                updated_at=now,
            )
        )
        await writer.flush()
        writer.add(
            PluginNotificationOutboxModel(
                id=item.id,
                notification_id=item.notification_id,
                part_key="text",
                source_event_id=item.source_event_id,
                plugin_id=item.plugin_id,
                target_type=item.target_type,
                target_id=item.target_id,
                bot_user_id=item.bot_user_id,
                part_type=item.part_type,
                text=item.text,
                status="processing",
                attempts=item.attempts,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
                canonical_target_space_id=item.canonical_target_space_id,
                canonical_presence_id=item.canonical_presence_id,
                canonical_conversation_id=item.canonical_conversation_id,
                source_person_id=item.source_person_id,
                prepared_json=json.dumps(item.prepared),
            )
        )
    await worker._record_outbound(item, receipt)
    async with database.immediate_session() as writer:
        original = await writer.scalar(
            select(ChatEventModel).where(ChatEventModel.platform_message_id == receipt.message_id)
        )
        original.author_kind, original.author_presence_id = "external_bot", None
        event_id = original.id
    await _replace_sender(database, env, "yuki")
    async with database.read_session() as reader:
        before = dict(
            (
                await reader.execute(
                    select(ChatEventModel.__table__).where(ChatEventModel.id == event_id)
                )
            )
            .mappings()
            .one()
        )
    await worker._record_outbound(item, receipt)
    async with database.read_session() as reader:
        assert (
            dict(
                (
                    await reader.execute(
                        select(ChatEventModel.__table__).where(ChatEventModel.id == event_id)
                    )
                )
                .mappings()
                .one()
            )
            == before
        )
    assert not transport.mock_calls


async def test_original_outbound_receipt_still_rejects_a_different_explicit_timestamp(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    sent = await env.service.execute("send", {"text": "原时间事实"}, env.context)
    async with database.read_session() as reader:
        original = await reader.get(ChatEventModel, sent["event_id"])
    with pytest.raises(CanonicalIdentityError) as conflict:
        await env.service.writer.append(
            scope=ConversationScope.group(
                original.bot_user_id,
                original.group_id,
                presence_id=env.presence,
                conversation_id=original.canonical_conversation_id,
            ),
            platform_message_id=original.platform_message_id,
            sender_user_id=original.sender_user_id,
            direction="outbound",
            content=original.content,
            segments=tuple(json.loads(original.segments_json)),
            occurred_at=original.occurred_at + timedelta(seconds=1),
            origin=original.origin,
        )
    assert conflict.value.category == "receipt_conflict"
