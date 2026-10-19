"""Canonical source facts survive account changes and remain live until dispatch."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select
from tests.support.canonical_identity import ensure_person
from tests.support.event_seed import seed_scoped_event
from tests.support.memory_identity import transfer_account
from tests.support.social_identity_cases import social_env

from yuki.capabilities.invocation import ToolInvocationContext, current_invocation
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.domain.tool_actor import ToolActor
from yuki.persistence.models import ChatEventModel
from yuki.runtime.origin import TurnOrigin
from yuki.services.agent_tools import ToolRuntime
from yuki.social.agent_adapter import invoke_social
from yuki.social.models import SocialError


async def test_send_context_does_not_query_a_reply_author_for_retired_subject_shortcuts(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    inbound = InboundMessage(
        "reply-source",
        "message",
        ScopeType.GROUP,
        SenderIdentity("10001"),
        "ordinary reply",
        bot_user_id="80001",
        group_id="20001",
        source_event_id=1,
        reply_to_event_id=1,
        person_id=env.person,
        conversation_id=env.context.conversation_id,
        presence_id=env.presence,
        space_id=env.space,
    )
    runtime = ToolRuntime(inbound=inbound)
    captured = AsyncMock(return_value={"status": "prepared-by-original-backend"})
    monkeypatch.setattr(env.service, "execute", captured)
    statements = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", observe)
    token = current_invocation.set(ToolInvocationContext(runtime, call_id="no-subject-lookup"))
    try:
        await invoke_social(env.service, "send_message", {"text": "reply text"}, runtime)
    finally:
        current_invocation.reset(token)
        event.remove(database.engine.sync_engine, "before_cursor_execute", observe)
    assert statements == []
    context = captured.await_args.args[2]
    assert context.person_refs == {"current_speaker": env.person}
    assert context.actor.person_id == env.person and context.trigger_event_id == 1


async def test_ordinary_inbound_reply_reaches_social_send_and_persists_original_receipt(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    async with database.sessions() as session:
        original = await session.scalar(select(ChatEventModel))
    current = await seed_scoped_event(
        env.service.writer,
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
    prepare = env.service.receipts.prepare

    async def delete_after_route(*args, **kwargs):
        receipt = await prepare(*args, **kwargs)
        async with database.immediate_session() as session:
            original = await session.get(ChatEventModel, event.id)
            await session.delete(original)
        return receipt

    monkeypatch.setattr(env.service.receipts, "prepare", delete_after_route)
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
    await transfer_account(database, "10001", "10002")
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
