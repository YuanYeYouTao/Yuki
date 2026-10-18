"""Original effects survive source lifecycle changes without granting new sends."""

import io
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import select, update
from tests.integration.test_native_social_delivery import native_env as native_env
from tests.integration.test_native_social_delivery import sends
from tests.support.automation_unified_delivery_helpers import setup_run
from tests.support.social_identity_cases import social_env

from yuki.automation.models import RunStatus
from yuki.client_api.identity import ClientIdentityService
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.hydrate import require_primary_alias_for_conversation
from yuki.domain.conversations import ConversationScope
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.domain.tool_actor import ToolActor
from yuki.gateway.providers.telegram import TelegramAPIError
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel, PresenceModel
from yuki.persistence.models import ChatEventModel
from yuki.persistence.people_repository import PeopleRepository
from yuki.persistence.repositories import EventLedgerRepository
from yuki.plugin_host.db_models import PluginMediaArtifactModel
from yuki.plugin_host.facades import HostPluginContext, PluginFacadeServices, PluginInvocation
from yuki.plugin_host.media_artifacts import PluginMediaArtifactStore
from yuki.plugin_host.repository import PluginInstallationRepository
from yuki.runtime.origin import TurnOrigin
from yuki.social.db_models import SocialOperationModel
from yuki.social.models import OperationStatus, SocialError
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.permissions import PluginPermission


async def invocation(
    database, plugin, event, *, platform, space_id=None, gateway=None, runtime_config=None
):
    async with database.sessions() as reader:
        primary_alias = await require_primary_alias_for_conversation(
            reader, event.canonical_conversation_id
        )
    actor = ToolActor(
        user_id=event.sender_user_id,
        bot_user_id=event.bot_user_id,
        group_id=event.group_id,
        origin=TurnOrigin.USER_MESSAGE,
        instruction="explicit effect",
        event_id=event.id,
        person_id=event.author_person_id,
        conversation_id=event.canonical_conversation_id,
        presence_id=event.ingress_presence_id,
        platform=platform,
        space_id=space_id,
    )
    return PluginInvocation(
        plugin_id=plugin,
        origin=actor.origin,
        actor_user_id=actor.user_id,
        actor_person_id=actor.person_id,
        actor=actor,
        bot_user_id=actor.bot_user_id,
        conversation_id=actor.conversation_id,
        presence_id=actor.presence_id,
        person_id=actor.person_id if event.group_id is None else None,
        space_id=space_id,
        source_event_id=event.id,
        inbound=InboundMessage(
            message_id=event.platform_message_id,
            event_type="message",
            scope_type=event.scope_type,
            sender=SenderIdentity(event.sender_user_id),
            text=event.content,
            bot_user_id=event.bot_user_id,
            group_id=event.group_id,
            source_event_id=event.id,
            legacy_conversation_key=primary_alias,
            person_id=event.author_person_id,
            conversation_id=event.canonical_conversation_id,
            presence_id=event.ingress_presence_id,
            space_id=space_id,
            platform=platform,
        ),
        delivery_identity="original-callback",
        gateway=gateway,
        runtime_config=runtime_config,
    )


@pytest.mark.parametrize("cleanup", [False, True])
@pytest.mark.parametrize("lost_ack", [False, True])
async def test_sdk_image_replay_does_not_read_expired_original_bytes(
    native_env, tmp_path, monkeypatch, cleanup, lost_ack
):
    env = native_env
    plugin = "test.original-image"
    await PluginInstallationRepository(env.db).upsert_discovered(
        plugin_id=plugin,
        name=plugin,
        version="1",
        plugin_api="5.0",
        yuki_requires="*",
        manifest_hash="original-media-manifest",
        entrypoint="plugin:register",
        requested_permissions=(PluginPermission.MESSAGE_MEDIA_SEND.value,),
    )
    media = PluginMediaArtifactStore(env.db, tmp_path / "plugin-media")
    stream = io.BytesIO()
    Image.new("RGB", (4, 4), (120, 80, 40)).save(stream, format="PNG")
    handle = await media.create(
        plugin_id=plugin,
        data=stream.getvalue(),
        content_type="image/png",
        filename="original.png",
        ttl_seconds=60,
        storage_mb=1,
    )
    host = HostPluginContext(
        plugin_id=plugin,
        approved_permissions=(PluginPermission.MESSAGE_MEDIA_SEND,),
        services=PluginFacadeServices(social=env.service, media_artifacts=media),
    )
    source = await invocation(
        env.db, plugin, env.event, platform="telegram", space_id=env.context.space_id
    )
    if lost_ack:
        env.failure.method, env.failure.timeout = "sendPhoto", True
    arguments = {
        "target_type": "group",
        "target_id": env.context.space_id,
        "media_reference": handle.handle_id,
    }
    try:
        with host.bind(source):
            first = await host.messages.send_image(**arguments)
        async with env.db.immediate_session() as session:
            await session.execute(
                update(PluginMediaArtifactModel)
                .where(PluginMediaArtifactModel.handle_id == handle.handle_id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        if cleanup:
            assert await media.cleanup() == 1
        count = len(sends(env))
        original_read = media.read_owned
        reads = []

        async def read(**kwargs):
            reads.append(kwargs)
            return await original_read(**kwargs)

        monkeypatch.setattr(media, "read_owned", read)
        with host.bind(source):
            replay = await host.messages.send_image(**arguments)
        assert replay.model_dump() == first.model_dump()
        assert not reads and len(sends(env)) == count
        with host.bind(replace(source, delivery_identity="new-callback")):
            with pytest.raises(PluginPermissionError, match="missing, expired, or foreign"):
                await host.messages.send_image(**arguments)
        assert len(reads) == 1 and len(sends(env)) == count
        with host.bind(source):
            with pytest.raises(SocialError, match="idempotency_conflict"):
                await host.messages.send_image(**{**arguments, "media_reference": uuid4().hex})
        assert len(sends(env)) == count
    finally:
        await host.close_host_resources()


@pytest.mark.parametrize("failure", [None, "first_failed", "first_unknown", "second_failed"])
async def test_sdk_sequence_uses_actual_aggregate_on_first_call_and_replay(
    native_env, monkeypatch, failure
):
    env = native_env
    original_call = env.client.call
    count = 0

    async def call(method, params, files=None):
        nonlocal count
        if method == "sendMessage":
            count += 1
            if (failure in {"first_failed", "first_unknown"} and count == 1) or (
                failure == "second_failed" and count == 2
            ):
                raise TelegramAPIError(
                    "synthetic_native_failure", uncertain=failure == "first_unknown"
                )
        return await original_call(method, params, files)

    monkeypatch.setattr(env.client, "call", call)
    plugin = "test.original-sequence"
    host = HostPluginContext(
        plugin_id=plugin,
        approved_permissions=(PluginPermission.MESSAGE_GROUP_SEND,),
        services=PluginFacadeServices(social=env.service),
    )
    source = await invocation(
        env.db,
        plugin,
        env.event,
        platform="telegram",
        space_id=env.context.space_id,
        runtime_config=env.context.runtime_snapshot,
    )
    try:
        with host.bind(source):
            first = await host.messages.send_text("a" * 5000)
        sent = count
        with host.bind(source):
            replay = await host.messages.send_text("a" * 5000)
        assert first.model_dump() == replay.model_dump() and count == sent
        aggregate = first.data["result"]
        assert first.ok is (failure is None)
        assert first.data["status"] == (
            "succeeded"
            if failure is None
            else "failed"
            if failure == "second_failed"
            else "unknown"
        )
        assert aggregate["parts"][0]["status"] == (
            "failed"
            if failure == "first_failed"
            else "uncertain"
            if failure == "first_unknown"
            else "succeeded"
        )
        if failure == "second_failed":
            assert aggregate["parts"][1]["status"] == "failed"
    finally:
        await host.close_host_resources()


@pytest.mark.parametrize("api", ["text", "music", "custom", "raw"])
@pytest.mark.parametrize("change", ["reset", "binding", "presence"])
@pytest.mark.parametrize("lost_ack", [False, True])
async def test_onebot_original_receipt_precedes_all_upstream_lifecycle_gates(
    database, tmp_path, api, change, lost_ack
):
    env = await social_env(database, tmp_path)
    async with database.sessions() as reader:
        event = await reader.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "inbound")
        )

    class Gateway:
        async def call_api(self, action, params):
            result = await env.bot.call_api(action, **params)
            if lost_ack:
                raise TimeoutError("lost original acknowledgement")
            return result

    async with database.immediate_session() as session:
        (await session.get(CanonicalPersonModel, event.author_person_id)).is_superuser = True
    plugin = "test.original-onebot"
    host = HostPluginContext(
        plugin_id=plugin,
        approved_permissions=(PluginPermission.ONEBOT_SEND, PluginPermission.ONEBOT_MUTATE),
        services=PluginFacadeServices(
            ledger=EventLedgerRepository(database),
            social=env.service,
        ),
    )
    source = await invocation(
        database, plugin, event, platform="qq", space_id=env.space, gateway=Gateway()
    )

    async def send():
        if api == "text":
            return await host.onebot.send_group(event.group_id, "original content")
        if api == "music":
            return await host.onebot.send_music_card(provider="qq", resource_id="42")
        if api == "custom":
            return await host.onebot.send_custom_music_card(
                url="https://example.invalid/music",
                image="https://example.invalid/image",
                title="original card",
            )
        return await host.onebot.call_mutating_action(
            "send_group_msg", {"group_id": event.group_id, "message": "original content"}
        )

    try:
        with host.bind(source):
            first = await send()
        assert first.ok is not lost_ack
        async with database.immediate_session() as writer:
            if change == "reset":
                await writer.execute(
                    update(CanonicalConversationModel)
                    .where(CanonicalConversationModel.id == source.conversation_id)
                    .values(starts_after_event_id=event.id, generation=2)
                )
            elif change == "binding":
                await writer.execute(
                    update(IdentityBindingModel)
                    .where(IdentityBindingModel.person_id == event.author_person_id)
                    .values(status="disabled")
                )
            else:
                await writer.execute(
                    update(PresenceModel)
                    .where(PresenceModel.id == env.presence)
                    .values(enabled=False)
                )
        before = len(env.bot.calls)
        with host.bind(source):
            replay = await send()
        assert first.data["operation_id"] == replay.data["operation_id"]
        assert replay.ok is not lost_ack
        assert len(env.bot.calls) == before
        with host.bind(replace(source, delivery_identity="new-callback")):
            with pytest.raises((SocialError, PluginPermissionError)):
                await send()
        assert len(env.bot.calls) == before
    finally:
        await host.close_host_resources()


@pytest.mark.parametrize("accounts", ["multiple", "none"])
async def test_accepted_group_automation_runs_without_a_representative_account(
    database, tmp_path, accounts
):
    case = await setup_run(database, tmp_path, mode="silent")
    identity = ClientIdentityService(database)
    if accounts == "multiple":
        code = await identity.issue_for_operator(case.env.person, str(uuid4()), str(uuid4()))
        await identity.claim_account(
            code["code"],
            platform="qq",
            account="10088",
            display_name="second account",
            message_id="second-link",
        )
    else:
        async with database.immediate_session() as writer:
            await writer.execute(
                update(IdentityBindingModel)
                .where(IdentityBindingModel.person_id == case.env.person)
                .values(status="disabled")
            )
    case.clock.advance(seconds=2)
    result = await case.dispatch()
    assert result is not None and result.status is RunStatus.SUCCEEDED
    original = await case.repository.get(case.row.id)
    assert original.canonical_creator_person_id == case.env.person


@pytest.mark.parametrize("sdk", [False, True])
@pytest.mark.parametrize("confirmed", [False, True])
async def test_forget_during_dispatch_keeps_only_the_original_transport_fact(
    database, tmp_path, monkeypatch, sdk, confirmed
):
    env = await social_env(database, tmp_path)
    appended = await env.service.writer.append(
        scope=ConversationScope.private(env.bot.self_id, "10001"),
        platform_message_id="private-original",
        sender_user_id="10001",
        direction="inbound",
        content="original private source",
    )
    async with database.sessions() as reader:
        event = await reader.get(ChatEventModel, appended.event.id)
    plugin = "test.forget-confirmed"
    physical = []

    async def send_then_forget(action, params):
        physical.append((action, params))
        assert await PeopleRepository(database).delete_person_id(env.person)
        if not confirmed:
            raise TimeoutError("unconfirmed response")
        return {"message_id": "real-confirmation"}

    class Gateway:
        async def call_api(self, action, params):
            return await send_then_forget(action, params)

    if sdk:
        host = HostPluginContext(
            plugin_id=plugin,
            approved_permissions=(PluginPermission.ONEBOT_SEND,),
            services=PluginFacadeServices(
                ledger=EventLedgerRepository(database), social=env.service
            ),
        )
        source = await invocation(database, plugin, event, platform="qq", gateway=Gateway())
        try:
            with host.bind(source):
                result = await host.onebot.send_private("10001", "private content")
            operation_id = result.data["operation_id"]
        finally:
            await host.close_host_resources()
    else:
        original = env.bot.call_api

        async def call(action, **params):
            if action == "send_private_msg":
                return await send_then_forget(action, params)
            return await original(action, **params)

        monkeypatch.setattr(env.bot, "call_api", call)
        context = replace(
            env.context,
            conversation_id=event.canonical_conversation_id,
            space_id=None,
            trigger_event_id=event.id,
            person_refs={"current_speaker": env.person},
        )
        result = await env.service.execute("send_message", {"text": "private content"}, context)
        operation_id = result["operation_id"]
    receipt = await env.service.receipts.get(operation_id)
    assert receipt.status is (OperationStatus.SUCCEEDED if confirmed else OperationStatus.UNCERTAIN)
    assert receipt.event_id is None and len(physical) == 1
    async with database.sessions() as reader:
        assert await reader.get(CanonicalConversationModel, event.canonical_conversation_id) is None
        assert not list(
            await reader.scalars(
                select(ChatEventModel).where(ChatEventModel.content == "private content")
            )
        )
        stored = await reader.get(SocialOperationModel, operation_id)
        assert stored.source_conversation_id == event.canonical_conversation_id
