"""Protocol credentials stay separate from ingress dedupe and internal ownership."""

from dataclasses import fields, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from tests.support.automation_unified_delivery_helpers import setup_run
from tests.support.canonical_identity import ensure_presence, ensure_space
from tests.unit.test_canonical_ingress import _Bot, _message, _stack

from yuki.adapters.onebot.social import OneBotSocialOperations
from yuki.automation.authority import AutomationScene, project_authority_snapshot
from yuki.conversation.canonical_db_models import (
    CanonicalEventReceiptModel,
    SpaceBindingIngestRouteModel,
)
from yuki.domain.messages import LocalDeliveryReceipt, OutboundMessage, OutboundSendReceipt
from yuki.domain.tool_actor import ToolActor
from yuki.gateway.providers.app.social import AppSocialOperations
from yuki.gateway.providers.feishu import FeishuClient
from yuki.gateway.providers.telegram import TelegramClient
from yuki.gateway.social import (
    TransportEffectError,
    TransportMedia,
    TransportMessage,
    TransportTarget,
)
from yuki.identity.canonical_repository import create_presence, create_space_binding
from yuki.identity.db_models import PresenceModel
from yuki.identity.ingress import resolve_canonical_reply
from yuki.persistence.models import AutomationModel, AutomationVersionModel
from yuki.services.processor import MessageProcessor
from yuki.social.models import SocialError
from yuki_plugin_sdk.api import PLUGIN_API_VERSION
from yuki_plugin_sdk.models import CurrentMessage


async def _switch_ingress(database, binding_id, presence_id):
    async with database.immediate_session() as session:
        route = await session.get(SpaceBindingIngestRouteModel, binding_id)
        route.ingest_presence_id = presence_id
        route.revision += 1
        route.route_generation += 1


async def test_qq_reply_does_not_cross_the_receiving_account_namespace(database):
    registry, resolver, writer = await _stack(database)
    first_bot, next_bot = _Bot("8000"), _Bot("8001")
    async with database.immediate_session() as session:
        first_presence = await ensure_presence(session, first_bot.self_id)
        next_presence = await ensure_presence(session, next_bot.self_id)
        await ensure_space(session, "2001")
    registry.connect(first_bot, provider_id="snowluma", presence_id=first_presence)
    first = await resolver.pre_admit(first_bot, _message(message_id="777", group_id="2001"))
    original = await writer.append_inbound(first.message, first)
    registry.connect(next_bot, provider_id="snowluma", presence_id=next_presence)
    await _switch_ingress(database, first.space_binding_id, next_presence)
    quoted = await resolver.pre_admit(
        next_bot,
        replace(
            _message(
                message_id="888",
                group_id="2001",
                bot_user_id=next_bot.self_id,
                reply_to_message_id="777",
            ),
            reply_text="the actual native quote from the new account",
        ),
    )
    assert not quoted.dropped and quoted.conversation_id == first.conversation_id
    assert quoted.message.reply_to_event_id is None
    assert quoted.message.reply_text == "the actual native quote from the new account"
    async with database.read_session() as session:
        _, _, internal = await resolve_canonical_reply(
            session,
            conversation_id=first.conversation_id,
            receiving_account_id=(await session.get(PresenceModel, next_presence)).account_id,
            operations=OneBotSocialOperations(),
            scope=quoted.message.scope(),
            reply_to_event_id=original.event.id,
            reply_to_message_id="an unrelated native envelope",
        )
    assert internal == original.event.id


async def test_native_reply_survives_a_new_assignment_of_the_same_receiving_account(database):
    registry, resolver, writer = await _stack(database)
    bot = _Bot("8000")
    async with database.immediate_session() as session:
        presence_id = await ensure_presence(session, bot.self_id)
    registry.connect(bot, provider_id="snowluma", presence_id=presence_id)
    admitted = await resolver.pre_admit(bot, _message(message_id="777"))
    original = await writer.append_inbound(admitted.message, admitted)
    async with database.immediate_session() as session:
        old = await session.get(PresenceModel, presence_id)
        old.status = "ended"
        await session.flush()
        reassigned = PresenceModel(id=str(uuid4()), account_id=old.account_id)
        session.add(reassigned)
        await session.flush()
        reassigned_id = reassigned.id
    async with database.read_session() as session:
        _, _, reply = await resolve_canonical_reply(
            session,
            conversation_id=admitted.conversation_id,
            receiving_account_id=(await session.get(PresenceModel, reassigned_id)).account_id,
            operations=OneBotSocialOperations(),
            scope=admitted.message.scope(),
            reply_to_message_id="777",
        )
    assert reply == original.event.id


async def test_telegram_chat_reference_survives_a_different_receiving_account(database):
    registry, resolver, writer = await _stack(database)

    def no_network(request):
        raise AssertionError("identity resolution must not make platform calls")

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_network)) as http:
        first_bot, next_bot = TelegramClient("a", client=http), TelegramClient("b", client=http)
        first_bot.self_id, next_bot.self_id = "9001", "9002"
        async with database.immediate_session() as session:
            first_presence = await create_presence(
                session, external_account_id="9001", platform="telegram", now=datetime.now(UTC)
            )
            next_presence = await create_presence(
                session, external_account_id="9002", platform="telegram", now=datetime.now(UTC)
            )
            await create_space_binding(
                session,
                external_space_id="-100777",
                platform="telegram",
                name="shared chat",
                enabled=True,
                autonomous_enabled=True,
                require_mention=False,
                now=datetime.now(UTC),
            )
        registry.connect(first_bot, provider_id="telegram", presence_id=first_presence.id)
        first_message = replace(
            _message(message_id="-100777:777", group_id="-100777", bot_user_id="9001"),
            platform="telegram",
            event_type="message:telegram",
        )
        first = await resolver.pre_admit(first_bot, first_message)
        original = await writer.append_inbound(first.message, first)
        registry.connect(next_bot, provider_id="telegram", presence_id=next_presence.id)
        await _switch_ingress(database, first.space_binding_id, next_presence.id)
        quoted = await resolver.pre_admit(
            next_bot,
            replace(
                _message(
                    message_id="-100777:888",
                    group_id="-100777",
                    bot_user_id="9002",
                    reply_to_message_id="-100777:777",
                ),
                platform="telegram",
                event_type="message:telegram",
            ),
        )
        assert not quoted.dropped
        assert quoted.message.reply_to_event_id == original.event.id
        assert quoted.message.reply_text == first_message.text


async def test_feishu_same_open_id_in_different_apps_does_not_share_native_references(database):
    registry, resolver, writer = await _stack(database)

    def no_network(request):
        raise AssertionError("identity resolution must not make platform calls")

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_network)) as http:
        first_bot = FeishuClient("cli_a", "secret", client=http)
        next_bot = FeishuClient("cli_b", "secret", client=http)
        first_bot.self_id, next_bot.self_id = "cli_a:ou_bot", "cli_b:ou_bot"
        async with database.immediate_session() as session:
            first_presence = await create_presence(
                session,
                external_account_id=first_bot.self_id,
                platform="feishu",
                now=datetime.now(UTC),
            )
            next_presence = await create_presence(
                session,
                external_account_id=next_bot.self_id,
                platform="feishu",
                now=datetime.now(UTC),
            )
            await create_space_binding(
                session,
                external_space_id="oc_shared",
                platform="feishu",
                name="one physical chat",
                enabled=True,
                autonomous_enabled=True,
                require_mention=False,
                now=datetime.now(UTC),
            )
        registry.connect(first_bot, provider_id="feishu", presence_id=first_presence.id)
        first = await resolver.pre_admit(
            first_bot,
            replace(
                _message(
                    message_id="om_collision",
                    group_id="oc_shared",
                    user_id="cli_a:ou_human",
                    bot_user_id=first_bot.self_id,
                ),
                platform="feishu",
                event_type="message:feishu",
            ),
        )
        await writer.append_inbound(first.message, first)
        registry.connect(next_bot, provider_id="feishu", presence_id=next_presence.id)
        await _switch_ingress(database, first.space_binding_id, next_presence.id)
        quoted = await resolver.pre_admit(
            next_bot,
            replace(
                _message(
                    message_id="om_reply",
                    group_id="oc_shared",
                    user_id="cli_b:ou_human",
                    bot_user_id=next_bot.self_id,
                    reply_to_message_id="om_collision",
                ),
                platform="feishu",
                event_type="message:feishu",
            ),
        )
        assert not quoted.dropped and quoted.conversation_id == first.conversation_id
        assert quoted.message.reply_to_event_id is None


async def test_complete_ingress_keys_do_not_collide_at_the_old_128_boundary(database):
    registry, resolver, writer = await _stack(database)
    bot = _Bot("8000")
    async with database.immediate_session() as session:
        presence = await ensure_presence(session, bot.self_id)
    registry.connect(bot, provider_id="snowluma", presence_id=presence)
    first_message = _message(message_id="x" * 128 + "A")
    first = await resolver.pre_admit(bot, first_message)
    original = await writer.append_inbound(first.message, first)
    replay = await resolver.pre_admit(bot, first_message)
    assert not (await writer.append_inbound(replay.message, replay)).created
    second = await resolver.pre_admit(
        bot,
        replace(first_message, ingress_key="x" * 128 + "B", native_message_id="native-B"),
    )
    another = await writer.append_inbound(second.message, second)
    assert another.created and another.event.id != original.event.id
    async with database.read_session() as session:
        keys = list(await session.scalars(select(CanonicalEventReceiptModel.ingress_key)))
    assert set(keys) == {"x" * 128 + "A", "x" * 128 + "B"}


@pytest.mark.parametrize(
    "ack, expected", [({}, None), ({"file_id": "a-file"}, None), ({"message_id": 12}, "12")]
)
async def test_qq_file_ack_never_fabricates_a_message_reference(tmp_path, ack, expected):
    bot = SimpleNamespace(call_api=AsyncMock(return_value=ack))
    operations = OneBotSocialOperations(tmp_path, "/gateway/files")
    receipt = await operations.send(
        bot,
        TransportTarget("space", "2001"),
        TransportMessage(media=TransportMedia("file", b"original", "original.txt", "text/plain")),
    )
    assert receipt.reference == expected
    assert bot.call_api.await_count == 1
    assert not list(tmp_path.iterdir())


async def test_qq_actual_handoff_write_failure_is_known_unsent(tmp_path, monkeypatch):
    import yuki.adapters.onebot.social as onebot_social

    def fail(path, content):
        raise OSError("synthetic full disk")

    monkeypatch.setattr(onebot_social, "_write_handoff", fail)
    bot = SimpleNamespace(call_api=AsyncMock())
    with pytest.raises(TransportEffectError) as failure:
        await OneBotSocialOperations(tmp_path, "/gateway/files").send(
            bot,
            TransportTarget("space", "2001"),
            TransportMessage(
                media=TransportMedia("file", b"original", "original.txt", "text/plain")
            ),
        )
    assert not failure.value.uncertain
    bot.call_api.assert_not_awaited()


async def test_app_adapter_never_reports_a_noop_as_a_successful_send():
    with pytest.raises(SocialError, match="capability_unavailable"):
        await AppSocialOperations().send(
            object(), TransportTarget("person", "one-person"), TransportMessage(text="hello")
        )


@pytest.mark.parametrize(
    "receipt, native",
    [(LocalDeliveryReceipt(19), None), (OutboundSendReceipt("native-19"), "native-19")],
)
async def test_reply_hook_preserves_internal_trigger_and_only_actual_native_fact(receipt, native):
    notifications = []
    processor = object.__new__(MessageProcessor)
    processor._event_publisher = SimpleNamespace(enqueue_notification=notifications.append)
    sender = SimpleNamespace(send=AsyncMock(return_value=receipt))
    await processor._send_text_effect(
        replace(_message(message_id="current"), source_event_id=733),
        sender,
        OutboundMessage(text="one actual effect"),
        record=False,
    )
    assert len(notifications) == 1
    assert notifications[0].payload["trigger_event_id"] == 733
    assert notifications[0].payload["native_message_id"] == native
    assert "platform_message_id" not in notifications[0].payload
    assert "trigger_message_id" not in notifications[0].payload


@pytest.mark.parametrize("native", [None, "x" * 129])
def test_sdk_current_native_reference_is_nullable_and_not_a_business_key(native):
    current = CurrentMessage(
        native_message_id=native,
        event_id=19,
        sender_user_id="1001",
        scope_type="private",
        received_at=datetime.now(UTC),
    )
    assert PLUGIN_API_VERSION == "9.0"
    assert current.native_message_id == native and current.event_id == 19
    assert "message_id" not in current.model_dump()
    assert "platform_message_id" not in {field.name for field in fields(ToolActor)}


def test_old_authority_projects_retired_field_without_mutating_the_frozen_source():
    source = {"creator_user_id": "1001", "bot_user_id": "8000", "created_from_message_id": "old"}
    authority = AutomationScene.model_validate(project_authority_snapshot(source))
    assert "created_from_message_id" not in authority.model_dump()
    assert source["created_from_message_id"] == "old"


async def test_automation_history_keeps_original_authority_and_cold_provenance(database, tmp_path):
    import json

    case = await setup_run(database, tmp_path)
    async with database.immediate_session() as session:
        row = await session.get(AutomationModel, case.row.id)
        version = await session.get(AutomationVersionModel, case.row.version_id)
        assert row.created_from_message_id == ""
        assert "created_from_message_id" not in json.loads(version.authority_snapshot_json)
        old = {
            **json.loads(version.authority_snapshot_json),
            "created_from_message_id": "old-native",
        }
        original_json = json.dumps(old, ensure_ascii=False, separators=(",", ":"))
        original_hash = version.script_hash
        version.authority_snapshot_json = original_json
        row.created_from_message_id = "cold-original-native"
    active = await case.repository.get(case.row.id)
    assert "created_from_message_id" not in active.model_dump()
    assert "created_from_message_id" not in active.authority_snapshot
    assert AutomationScene.model_validate(active.authority_snapshot)
    async with database.read_session() as session:
        row = await session.get(AutomationModel, case.row.id)
        version = await session.get(AutomationVersionModel, case.row.version_id)
        assert row.created_from_message_id == "cold-original-native"
        assert version.authority_snapshot_json == original_json
        assert version.script_hash == original_hash
