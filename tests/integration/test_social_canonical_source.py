"""Canonical source facts survive account changes and remain live until dispatch."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from tests.support.social_identity_cases import social_env

from qq_ai_bot.capabilities.invocation import ToolInvocationContext, current_invocation
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.identity.canonical_repository import ensure_person
from qq_ai_bot.identity.db_models import IdentityBindingModel
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.social.agent_adapter import invoke_social
from qq_ai_bot.social.models import SocialError


async def test_ordinary_inbound_reply_reaches_social_send_and_persists_original_receipt(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    async with database.sessions() as session:
        original = await session.scalar(select(ChatEventModel))
    current = await env.service.writer.append(
        scope=ConversationScope.group("80001", "20001"),
        platform_message_id="ordinary-reply",
        sender_user_id="10001",
        direction="inbound",
        content="read the quoted message",
        reply_to_event_id=original.id,
        reply_to_message_id=original.platform_message_id,
    )
    inbound = InboundMessage(
        "ordinary-reply",
        "message",
        ScopeType.GROUP,
        SenderIdentity("10001"),
        "read the quoted message",
        bot_user_id="80001",
        group_id="20001",
        source_event_id=current.event.id,
        reply_to_event_id=original.id,
        person_id=env.person,
        conversation_id=env.context.conversation_id,
        presence_id=env.presence,
        space_id=env.space,
    )
    runtime = ToolRuntime(inbound=inbound, visible_event_ids=frozenset({original.id}))
    arguments = {"text": "reply delivered", "reply_to_event_id": original.id}
    token = current_invocation.set(ToolInvocationContext(runtime, call_id="ordinary-reply-send"))
    try:
        result = await invoke_social(env.service, "send_message", arguments, runtime)
    finally:
        current_invocation.reset(token)
    assert result["status"] == "succeeded"
    sends = [params for action, params in env.bot.calls if action == "send_group_msg"]
    assert len(sends) == 1
    assert sends[0]["message"][0] == {"type": "reply", "data": {"id": original.platform_message_id}}
    receipt = await env.service.receipts.find(
        f"{env.context.conversation_id}:event:{current.event.id}", "ordinary-reply-send"
    )
    assert receipt is not None and receipt.operation_id == result["operation_id"]
    assert receipt.status == "succeeded" and receipt.event_id == result["event_id"]
    async with database.sessions() as session:
        sent = await session.get(ChatEventModel, receipt.event_id)
    assert sent.direction == "outbound" and sent.content == "reply delivered"
    assert sent.canonical_conversation_id == current.event.canonical_conversation_id
    assert sent.reply_to_event_id == original.id


async def test_current_reply_rechecks_original_source_before_claim(database, tmp_path, monkeypatch):
    env = await social_env(database, tmp_path)
    async with database.sessions() as session:
        event = await session.scalar(select(ChatEventModel))
    context = replace(env.context, trigger_event_id=event.id, presence_id=env.presence)
    send_route = env.service.send_route

    async def delete_after_route(*args, **kwargs):
        route = await send_route(*args, **kwargs)
        if await env.service.receipts.find(context.turn_id, context.call_id) is not None:
            async with database.immediate_session() as session:
                original = await session.get(ChatEventModel, event.id)
                await session.delete(original)
        return route

    monkeypatch.setattr(env.service, "send_route", delete_after_route)
    env.bot.calls.clear()
    with pytest.raises(SocialError, match="invalid_reply_context"):
        await env.service.execute("send_message", {"text": "obsolete source"}, context)
    assert not any(action.startswith("send_") for action, _ in env.bot.calls)
    receipt = await env.service.receipts.find(context.turn_id, context.call_id)
    assert receipt.status == "prepared"


async def test_social_refs_use_original_person_after_external_account_transfer(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    async with database.sessions() as session:
        event = await session.scalar(select(ChatEventModel))
    inbound = InboundMessage(
        "inbound",
        "message",
        ScopeType.GROUP,
        SenderIdentity("10001"),
        "hello",
        bot_user_id="80001",
        group_id="20001",
        source_event_id=event.id,
        person_id=env.person,
        conversation_id=env.context.conversation_id,
        presence_id=env.presence,
        space_id=env.space,
    )
    actor = ToolActor.from_inbound(inbound)
    async with database.immediate_session() as session:
        different = await ensure_person(session, "10002")
        binding = await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.platform == "qq",
                IdentityBindingModel.external_account_id == "10001",
                IdentityBindingModel.status == "active",
            )
        )
        binding.person_id = different
    execute = AsyncMock(return_value={"source": "canonical_ledger"})
    monkeypatch.setattr(env.service, "execute", execute)
    runtime = SimpleNamespace(
        actor_context=actor,
        require_actor=lambda: actor,
        inbound=inbound,
        origin=TurnOrigin.USER_MESSAGE,
        tools_closed=False,
        effective_conversation_id=env.context.conversation_id,
        effective_trigger_event_id=event.id,
        space_id=env.space,
    )
    token = current_invocation.set(ToolInvocationContext(runtime, call_id="original"))
    try:
        await invoke_social(env.service, "find_contacts", {"kind": "person"}, runtime)
    finally:
        current_invocation.reset(token)
    actual = execute.await_args.args[2]
    assert actual.person_refs["current_speaker"] == env.person != different
    assert actual.presence_id == env.presence
