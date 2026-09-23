"""Confirmed receipt content and actor-free background media delivery."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import delete, select
from tests.support.social_identity_cases import social_env

from qq_ai_bot.admin.models import ReplyRuntimeConfig
from qq_ai_bot.capabilities.invocation import ToolInvocationContext, current_invocation
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.emoji.delivery import EmojiDeliveryService
from qq_ai_bot.emoji.models import EmojiSelectionResult
from qq_ai_bot.identity.canonical_repository import ensure_presence, ensure_space
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.social.agent_adapter import invoke_social
from qq_ai_bot.social.db_models import SocialOperationModel
from qq_ai_bot.social.models import SocialError
from qq_ai_bot.speech.delivery import VoiceDeliveryService


@pytest.mark.asyncio
async def test_strict_social_receipt_is_uncertain_without_resend_and_upload_stays_independent(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    gateway = AsyncMock()
    monkeypatch.setattr(env.service, "_call", gateway)
    invalid = (
        None,
        {},
        {"message_id": ""},
        {"message_id": "  "},
        {"message_id": False},
        {"message_id": True},
        {"message_id": {}},
        {"message_id": []},
    )
    for index, response in enumerate(invalid):
        gateway.return_value = response
        context = replace(env.context, call_id=f"invalid-{index}")
        result = await env.service.execute("send_message", {"text": "不确定"}, context)
        assert result["status"] == "uncertain"
        assert "delivered_text" not in result
        count = gateway.await_count
        assert await env.service.execute("send_message", {"text": "不确定"}, context) == result
        assert gateway.await_count == count
    async with database.sessions() as session:
        assert not list(
            await session.scalars(
                select(ChatEventModel).where(ChatEventModel.direction == "outbound")
            )
        )
    notify = Mock(side_effect=RuntimeError("offline wakeup hook failed"))
    monkeypatch.setattr(env.service.writer, "notify_committed", notify)
    for index, response in enumerate(({"message_id": 123}, {"message_id": "456"}, {"id": "789"})):
        gateway.return_value = response
        context = replace(env.context, call_id=f"valid-{index}")
        result = await env.service.execute(
            "send_message",
            {"text": "#62052>真实正文"},
            context,
        )
        assert result["status"] == "succeeded" and result["delivered_text"] == "真实正文"
        assert result["delivered_text_available"] is True
        async with database.sessions() as session:
            stored = await session.get(SocialOperationModel, result["operation_id"])
            assert stored.event_id == result["event_id"]
            event = await session.get(ChatEventModel, stored.event_id)
            assert event.content == "真实正文"
        count = gateway.await_count
        assert (
            await env.service.execute("send_message", {"text": "#62052>真实正文"}, context)
            == result
        )
        assert gateway.await_count == count
    artifact = env.store.write("receipt.txt", b"test")
    gateway.return_value = {}
    uploaded = await env.service.execute(
        "send_message",
        {"artifact_id": artifact["artifact_id"], "attachment_kind": "file"},
        replace(env.context, call_id="upload-no-message-id"),
    )
    assert uploaded["status"] == "succeeded"
    assert uploaded["platform_reference"] is None and uploaded["delivered_text"] == ""
    assert notify.call_count == 4


@pytest.mark.asyncio
async def test_receipt_content_projection_uses_internal_event_and_checks_ownership(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    args = {"text": "#62052>真实正文"}
    receipt = await env.service.execute("send_message", args, env.context)
    async with database.sessions() as session:
        actual = await session.get(ChatEventModel, receipt["event_id"])
        timestamp = actual.occurred_at
    async with database.sessions.begin() as session:
        await ensure_presence(session, "80002")
        await ensure_space(session, "20002")
    unrelated_ids = []
    for scope in (
        ConversationScope.group("80002", "20001"),
        ConversationScope.group("80001", "20002"),
    ):
        unrelated = await env.service.writer.append(
            scope=scope,
            platform_message_id=f"unrelated-{scope.bot_user_id}-{scope.group_id}",
            sender_user_id=scope.bot_user_id,
            direction="outbound",
            sender_is_bot=True,
            content="其他目标或账号",
            origin="social_tool",
            occurred_at=timestamp,
            segments=({"type": "text", "data": {"text": "其他目标或账号"}},),
        )
        # New writes already reject conflicting receipt identities. Simulate
        # pre-existing imported/corrupt ledger data to exercise read isolation.
        async with database.sessions.begin() as session:
            other = await session.get(ChatEventModel, unrelated.event.id)
            other.platform_message_id = receipt["platform_reference"]
        unrelated_ids.append(unrelated.event.id)
    assert await env.service.execute("send_message", args, env.context) == receipt
    async with database.sessions.begin() as session:
        actual = await session.get(ChatEventModel, receipt["event_id"])
        actual.platform_message_id = "transport-metadata-changed"
        actual.occurred_at = datetime(2020, 1, 1, tzinfo=UTC)
    # Neither transport reference nor timestamps are used to locate the event.
    assert await env.service.execute("send_message", args, env.context) == receipt
    for event_id in unrelated_ids:
        async with database.sessions.begin() as session:
            stored = await session.get(SocialOperationModel, receipt["operation_id"])
            stored.event_id = event_id
        projected = await env.service.execute("send_message", args, env.context)
        assert projected["status"] == "succeeded" and projected["delivered_text_available"] is False
        assert "delivered_text" not in projected
    # Missing or incorrectly bound historical data is not reconstructed from raw args.
    async with database.sessions.begin() as session:
        stored = await session.get(SocialOperationModel, receipt["operation_id"])
        stored.event_id = receipt["event_id"]
        actual = await session.get(ChatEventModel, receipt["event_id"])
        actual.origin = "unrelated_source"
    projected = await env.service.execute("send_message", args, env.context)
    assert projected["status"] == "succeeded" and "delivered_text" not in projected
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1


@pytest.mark.asyncio
async def test_historical_or_deleted_event_reference_never_reconstructs_content_or_resends(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    args = {"text": "#62052>真实正文"}
    receipt = await env.service.execute("send_message", args, env.context)
    event_id = receipt["event_id"]
    async with database.sessions.begin() as session:
        stored = await session.get(SocialOperationModel, receipt["operation_id"])
        stored.event_id = None
    # The matching historical event still exists, but must never be reverse-looked up.
    old = await env.service.execute("send_message", args, env.context)
    assert old["status"] == "succeeded" and old["event_id"] is None
    assert old["delivered_text_available"] is False and "delivered_text" not in old
    async with database.sessions.begin() as session:
        stored = await session.get(SocialOperationModel, receipt["operation_id"])
        stored.event_id = event_id
    async with database.sessions.begin() as session:
        await session.execute(delete(ChatEventModel).where(ChatEventModel.id == event_id))
    deleted = await env.service.execute("send_message", args, env.context)
    assert deleted == old
    async with database.sessions() as session:
        stored = await session.get(SocialOperationModel, receipt["operation_id"])
        assert stored.status == "succeeded" and stored.event_id is None
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1


@pytest.mark.asyncio
async def test_plugin_background_media_uses_only_real_target_without_actor(database, tmp_path):
    env = await social_env(database, tmp_path)
    assert await env.router.cas_takeover_person(env.person) in {"taken", "unchanged"}
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"offline-audio")
    speech = SimpleNamespace(
        synthesize=AsyncMock(
            return_value=SimpleNamespace(
                generation_id=7,
                profile_id="default",
                reference_key="ref",
                target_language="zh",
                duration_milliseconds=500,
            )
        ),
        audio_path=lambda _: audio,
        mark_sent=AsyncMock(),
    )
    env.service.speech_delivery = VoiceDeliveryService(speech)
    selector = SimpleNamespace(
        select=AsyncMock(return_value=EmojiSelectionResult(emoji_id="emoji"))
    )
    repository = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                id="emoji",
                relative_path="emoji.png",
                description="笑脸",
                mime_type="image/png",
                animated=False,
            )
        )
    )
    env.service.emoji_delivery = EmojiDeliveryService(
        selector=selector,
        repository=repository,
        storage=SimpleNamespace(read=lambda _: b"offline-image"),
    )
    snapshot = SimpleNamespace(
        speech=SimpleNamespace(
            enabled=True,
            agent_delivery_enabled=True,
            private_enabled=True,
            group_enabled=True,
            default_profile="default",
            split_sentence=True,
        ),
        emoji=SimpleNamespace(enabled=True),
        vision=SimpleNamespace(),
    )
    for scope, target_kind in (
        (ConversationScope.group("80001", "20001"), "group"),
        (ConversationScope.private("80001", "10001"), "private"),
    ):
        event = await env.service.writer.append_external(
            scope=scope,
            platform_message_id=f"plugin-{target_kind}",
            source_plugin_id="test",
            external_source="test",
            external_event_key=f"media-{target_kind}",
            external_event_type="notice",
            external_payload={},
            external_target_id=scope.group_id or scope.private_peer_user_id,
            content="notice",
            occurred_at=datetime.now(UTC),
        )
        async with database.sessions() as session:
            row = await session.get(ChatEventModel, event.event.id)
            conversation_id = row.canonical_conversation_id
        runtime = SimpleNamespace(
            origin=TurnOrigin.PLUGIN_BACKGROUND,
            effective_trigger_event_id=event.event.id,
            effective_conversation_id=conversation_id,
            inbound=None,
            read_only=False,
            tools_closed=False,
            space_id=env.space if scope.group_id else None,
            person_id=None if scope.group_id else env.person,
            runtime_config=snapshot,
            conversation_key=scope.key,
        )
        for media in ("voice", "emoji"):
            args = {
                "text": "#62052>Yuki已收到",
                media: (
                    {"request_basis": "agent_initiated"} if media == "voice" else {"goal": "开心"}
                ),
            }
            token = current_invocation.set(
                ToolInvocationContext(runtime, call_id=f"{target_kind}-{media}")
            )
            try:
                result = await invoke_social(env.service, "send_message", args, runtime)
                assert result["status"] == "succeeded"
                assert result["delivered_text"] == (
                    "ゆき已收到" if media == "voice" else "Yuki已收到"
                )
                assert env.bot.calls[-1][0] == f"send_{target_kind}_msg"
                count = len(env.bot.calls)
                assert await invoke_social(env.service, "send_message", args, runtime) == result
                assert len(env.bot.calls) == count
                with pytest.raises(SocialError, match="permission_denied"):
                    await invoke_social(
                        env.service,
                        "send_message",
                        {
                            **args,
                            "target": {
                                "kind": "person",
                                "target_id": env.person,
                            },
                        },
                        runtime,
                    )
            finally:
                current_invocation.reset(token)
        request = selector.select.await_args.args[0]
        assert request.group_id == scope.group_id
        assert request.private_peer_user_id == scope.private_peer_user_id
        speech_request = speech.synthesize.await_args.args[0]
        assert speech_request.canonical_conversation_id == conversation_id
        assert speech_request.conversation_key == scope.key


@pytest.mark.asyncio
async def test_work_social_delivery_passes_seventeenth_message_and_split_without_replay(
    database, tmp_path, monkeypatch
):
    from qq_ai_bot.runtime.work_activation import current_work_control
    from qq_ai_bot.runtime.work_control import WorkControl
    from qq_ai_bot.runtime.work_repository import WorkRepository
    from qq_ai_bot.services.message_splitter import OutboundMessageSplitter

    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)
    source = {"origin": "user_message", "actor_user_id": "10001"}
    control = WorkControl(repo, lease, "social-test", source, AsyncMock())
    control.current = await repo.accept(lease, source_key="social-test", source=source, goal="send")
    token = current_work_control.set(control)
    try:
        for index in range(15):
            receipt = await env.service.execute(
                "send_message",
                {"text": f"step-{index}"},
                replace(env.context, call_id=f"step-{index}"),
            )
            assert receipt["status"] == "succeeded"
        context = replace(
            env.context,
            call_id="split",
            runtime_snapshot=SimpleNamespace(reply=ReplyRuntimeConfig(0, 0, 1800, 10)),
        )
        result = await env.service.execute("send_message", {"text": "第十六条\n第十七条"}, context)
        assert result["status"] == "succeeded" and result["sent_messages"] == 2
        assert control.current["sent_messages"] == 17
        assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 17
        assert (
            await env.service.execute("send_message", {"text": "第十六条\n第十七条"}, context)
            == result
        )
        assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 17
    finally:
        current_work_control.reset(token)
        await repo.release(lease)

    source_event = await env.service.writer.append(
        scope=ConversationScope.group("80001", "20001"),
        platform_message_id="123",
        sender_user_id="10001",
        direction="inbound",
        content="quote source",
    )
    monkeypatch.setattr(
        OutboundMessageSplitter, "render", lambda *_args, **_kwargs: ("first", "second")
    )
    quoted_context = replace(
        env.context,
        call_id="quoted-sequence",
        visible_event_ids=frozenset({source_event.event.id}),
        runtime_snapshot=SimpleNamespace(
            reply=SimpleNamespace(delay_min_seconds=0, delay_max_seconds=0)
        ),
    )
    quoted = await env.service.execute(
        "send_message",
        {"text": "first second", "reply_to_event_id": source_event.event.id},
        quoted_context,
    )
    assert quoted["status"] == "succeeded"
    async with database.sessions() as session:
        sent = list(
            await session.scalars(
                select(ChatEventModel)
                .where(ChatEventModel.id > source_event.event.id)
                .order_by(ChatEventModel.id)
            )
        )
    assert [row.reply_to_event_id for row in sent] == [source_event.event.id, None]
    assert sent[0].reply_to_message_id == source_event.event.platform_message_id
    assert sent[1].reply_to_message_id is None
