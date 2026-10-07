"""Retired voice inputs never become text sends or erase dispatched facts."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from tests.support.social_identity_cases import social_env

from qq_ai_bot.automation.registry import AutomationCapabilityRegistry
from qq_ai_bot.social.automation import register_social_automation
from qq_ai_bot.social.db_models import SocialOperationModel
from qq_ai_bot.social.models import OperationStatus, SocialError, SocialTarget


@pytest.mark.parametrize("voice", [None, {}, False, "wrong", {"request_basis": "user_requested"}])
@pytest.mark.parametrize("entry", ["canonical", "sequence", "single"])
async def test_retired_key_presence_rejects_before_route_or_intent(
    database, tmp_path, voice, entry
):
    env = await social_env(database, tmp_path)
    env.bot.calls.clear()
    env.service.target = AsyncMock(side_effect=AssertionError("must not resolve target"))
    args = {"text": "hello", "voice": voice}
    with pytest.raises(SocialError, match=r"^invalid_message_arguments$"):
        if entry == "canonical":
            env.service._canonical_send_arguments(args)
        elif entry == "sequence":
            await env.service._send_message_sequence(args, env.context)
        else:
            await env.service.execute(
                "send_message", args, replace(env.context, sequence_part_index=0)
            )
    assert not env.bot.calls
    env.service.target.assert_not_awaited()
    async with database.sessions() as session:
        assert not (await session.scalars(select(SocialOperationModel))).all()


@pytest.mark.parametrize(
    "status", [OperationStatus.SUCCEEDED, OperationStatus.UNCERTAIN, OperationStatus.EXECUTING]
)
async def test_original_dispatched_voice_receipt_is_bound_without_normalization(
    database, tmp_path, status
):
    env = await social_env(database, tmp_path)
    env.bot.calls.clear()
    args = {"text": "#62052>原声音", "voice": {"request_basis": "user_requested"}}
    receipt = await env.service.receipts.prepare(
        source_turn_id=env.context.turn_id,
        tool_call_id=env.context.call_id,
        source_conversation_id=env.context.conversation_id,
        action="send_message",
        target=SocialTarget(kind="space", id=env.space),
        payload=args,
    )
    assert await env.service.receipts.claim(receipt.operation_id, presence_id=env.presence)
    if status is not OperationStatus.EXECUTING:
        async with database.sessions.begin() as session:
            await env.service.receipts.finish(receipt.operation_id, status=status, session=session)
    async with database.sessions() as session:
        original = await session.get(SocialOperationModel, receipt.operation_id)
        original_hash = original.payload_hash
    env.service._canonical_send_arguments = lambda _: pytest.fail(
        "must not renormalize old payload"
    )
    env.service.target = AsyncMock(side_effect=AssertionError("must not resolve target"))
    for _ in range(2):
        result = await env.service.execute("send_message", args, env.context)
        assert result["status"] == status.value
    with pytest.raises(SocialError, match="idempotency_conflict"):
        await env.service.execute("send_message", {**args, "voice": None}, env.context)
    assert not env.bot.calls
    async with database.sessions() as session:
        unchanged = await session.get(SocialOperationModel, receipt.operation_id)
        assert unchanged.payload_hash == original_hash and unchanged.status == status.value


def test_automation_uses_new_schema_and_rejects_retired_fields():
    registry = AutomationCapabilityRegistry()
    register_social_automation(registry, {})
    capability = registry.get("social.send_message")
    assert capability.schema_version == 2
    for voice in (None, {}, False, "wrong"):
        with pytest.raises(ValueError, match="invalid_arguments"):
            capability.validate_arguments({"text": "hello", "voice": voice})


def test_outer_fields_survive_message_validation():
    from qq_ai_bot.social.service import SocialService

    args = {
        "text": "hello",
        "target": {"kind": "space", "target_id": "id"},
        "reply_to_event_id": 1,
        "work_report": {"kind": "final"},
    }
    assert SocialService._canonical_send_arguments(args) == args
    with pytest.raises(SocialError, match="invalid_message_arguments"):
        SocialService._canonical_send_arguments({"text": "hello", "obsolete": False})


async def test_ingress_sender_rejects_outbound_audio_before_dispatch():
    from qq_ai_bot.adapters.onebot.sender import OneBotSender, OneBotSendError
    from qq_ai_bot.domain.messages import AttachmentKind, OutboundMedia, OutboundMessage
    from qq_ai_bot.services.chat import ChatService

    bot = SimpleNamespace(send=AsyncMock())
    sender = OneBotSender(bot, object())
    media = OutboundMedia(kind=AttachmentKind.AUDIO, content=b"old audio")
    with pytest.raises(OneBotSendError) as error:
        await sender._deliver(OutboundMessage(text="must not send", media=(media,)))
    assert error.value.dispatched is False
    bot.send.assert_not_awaited()
    with pytest.raises(ValueError, match="unsupported outbound media kind"):
        ChatService._ledger_media_segment(media)


def test_voice_command_is_no_longer_a_supported_command():
    from qq_ai_bot.services.policies import CommandName, parse_command

    assert "voice" not in {command.value for command in CommandName}
    assert parse_command("/ai voice status", "") == (None, "voice status", True)
