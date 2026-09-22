"""Account-specific live history and delegated target boundaries."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from qq_ai_bot.conversation.canonical_db_models import PersonActiveRouteModel, SpaceActiveRouteModel
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.identity.canonical_repository import ensure_presence
from qq_ai_bot.identity.db_models import PresenceModel
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.social.automation import SocialAutomationAdapter
from qq_ai_bot.social.db_models import SocialOperationModel
from qq_ai_bot.social.models import SocialError
from tests.support.social_identity_cases import Bot, add_second_account


async def history_agent_loop(env):
    from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
    from qq_ai_bot.domain.messages import (
        ChatResponse,
        InboundMessage,
        SenderIdentity,
        ToolCall,
        ToolFunction,
    )
    from qq_ai_bot.llm.fake import FakeLLMProvider
    from qq_ai_bot.services.main_agent_contract import MainAgentContract
    from qq_ai_bot.workspace.short_state import ShortState
    from tests.conftest import MemorySender, build_harness, make_settings

    calls = 0
    arguments = json.dumps({"kind": "person", "target_id": env.person, "presence_id": env.presence})
    env.bot.history = {"messages": [{"message_id": 50, "message": "第一条"}]}

    def respond(request):
        nonlocal calls
        calls += 1
        returned = [
            json.loads(message.content) for message in request.messages if message.role == "tool"
        ]
        if calls == 2:
            assert returned[-1]["ok"], returned[-1]
            assert returned[-1]["data"]["count"] == 1
            env.bot.history["messages"].append({"message_id": 51, "message": "第二条"})
        if calls == 3:
            assert returned[-1]["data"]["count"] == 2
            return "读到两条消息"
        if calls == 4:
            assert any(
                message.role == "system" and "上一段最终正文没有发送给用户" in message.content
                for message in request.messages
            )
            return ChatResponse(content="", latency_seconds=0)
        return ChatResponse(
            content="",
            latency_seconds=0,
            tool_calls=(
                ToolCall(
                    id=f"history-{calls}",
                    function=ToolFunction(name="read_conversation_history", arguments=arguments),
                ),
            ),
        )

    provider = FakeLLMProvider(respond)
    harness = build_harness(env.db, make_settings(env.db.url, enabled_groups_csv="20001"), provider)
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    chat._agent_runner.main_contract = MainAgentContract(chat, ShortState(env.store))
    sender = MemorySender()
    result = await harness.processor.handle(
        InboundMessage(
            message_id="read-other-conversation",
            event_type="message:test",
            scope_type=ScopeType.GROUP,
            sender=SenderIdentity("10001"),
            text="看看私聊后续说了什么",
            bot_user_id="80001",
            group_id="20001",
            mentions_bot=True,
            conversation_id=env.context.conversation_id,
            legacy_conversation_key=ConversationScope.group("80001", "20001").key,
            person_id=env.person,
            space_id=env.space,
            presence_id=env.presence,
        ),
        sender,
    )
    assert result.reason == "chat" and not sender.messages
    assert calls == 4
    assert sum(action == "get_friend_msg_history" for action, _ in env.bot.calls) == 2
    async with env.db.sessions() as session:
        assert not await session.scalar(
            select(ChatEventModel.id).where(ChatEventModel.platform_message_id.in_(("50", "51")))
        )


async def history_receipt(env):
    assert await env.router.cas_takeover_person(env.person) == "taken"
    sent = await env.service.execute(
        "send_message",
        {"target": {"kind": "person", "target_id": env.person}, "text": "hi"},
        env.context,
    )
    assert sent["status"] == "succeeded"
    await add_second_account(env)
    other = Bot("80002")
    async with env.db.sessions() as session, session.begin():
        second = await ensure_presence(session, other.self_id)
        route = await session.get(PersonActiveRouteModel, env.person)
        route.presence_id = second
        route.paused = True
        before = await session.scalar(select(func.count(ChatEventModel.id)))
    env.registry.connect(other, provider_id="snowluma", presence_id=second)
    env.bot.history = {
        "messages": [{"message_id": 42, "sender": {"user_id": 10001}, "message": "收到"}]
    }
    args = {"operation_id": sent["operation_id"]}
    result = await env.service.execute("read_conversation_history", args, env.context)
    assert result["messages"][0]["text"] == "收到"
    assert result["presence_id"] == env.presence
    assert result["external_target_id"] == "10001"
    assert env.bot.calls[-1] == ("get_friend_msg_history", {"user_id": 10001, "count": 20})
    assert not other.calls
    env.bot.history["messages"].append({"message_id": 43, "message": "再说一句"})
    assert (await env.service.execute("read_conversation_history", args, env.context))["count"] == 2
    async with env.db.sessions() as session:
        assert await session.scalar(select(func.count(ChatEventModel.id))) == before
    for invalid in ({**args, "presence_id": second}, {**args, "target_id": env.person}):
        with pytest.raises(SocialError, match="target_selector_conflict"):
            await env.service.execute("read_conversation_history", invalid, env.context)
    with pytest.raises(SocialError, match="history_receipt_unavailable"):
        await env.service.execute(
            "read_conversation_history", args, replace(env.context, conversation_id=str(uuid4()))
        )

    # Colliding transport references on another target or Presence must not
    # change the persisted internal anchor or re-select an arbitrary event.
    unrelated_ids = []
    for index, scope in enumerate(
        (
            ConversationScope.group(env.bot.self_id, "20001"),
            ConversationScope.private(other.self_id, "10001"),
        )
    ):
        unrelated = await env.service.writer.append(
            scope=scope,
            platform_message_id=f"history-unrelated-{index}",
            sender_user_id=scope.bot_user_id,
            direction="outbound",
            sender_is_bot=True,
            content="unrelated",
            origin="social_tool",
        )
        async with env.db.sessions.begin() as session:
            row = await session.get(ChatEventModel, unrelated.event.id)
            row.platform_message_id = sent["platform_reference"]
        unrelated_ids.append(unrelated.event.id)
    assert (await env.service.execute("read_conversation_history", args, env.context))[
        "external_target_id"
    ] == "10001"
    async with env.db.sessions.begin() as session:
        event = await session.get(ChatEventModel, sent["event_id"])
        event.platform_message_id = "history-transport-metadata-changed"
        event.occurred_at = datetime(2020, 1, 1, tzinfo=UTC)
    assert (await env.service.execute("read_conversation_history", args, env.context))[
        "external_target_id"
    ] == "10001"
    calls_before_rejections = len(env.bot.calls)
    for event_id in (*unrelated_ids, None):
        async with env.db.sessions.begin() as session:
            receipt = await session.get(SocialOperationModel, sent["operation_id"])
            receipt.event_id = event_id
        with pytest.raises(SocialError, match="history_anchor_unavailable"):
            await env.service.execute("read_conversation_history", args, env.context)
    async with env.db.sessions.begin() as session:
        receipt = await session.get(SocialOperationModel, sent["operation_id"])
        receipt.event_id = sent["event_id"]
    assert len(env.bot.calls) == calls_before_rejections
    env.registry.disconnect(env.bot)
    with pytest.raises(SocialError, match="history_presence_unavailable"):
        await env.service.execute("read_conversation_history", args, env.context)
    async with env.db.sessions.begin() as session:
        await session.execute(delete(ChatEventModel).where(ChatEventModel.id == sent["event_id"]))
    with pytest.raises(SocialError, match="history_anchor_unavailable"):
        await env.service.execute("read_conversation_history", args, env.context)
    assert len(env.bot.calls) == calls_before_rejections
    assert not other.calls


async def history_targets_and_delegation(env):
    other = Bot("80002")
    async with env.db.sessions() as session, session.begin():
        second = await ensure_presence(session, other.self_id)
        group_route = await session.get(SpaceActiveRouteModel, env.space)
        group_route.paused = True
    env.registry.connect(other, provider_id="snowluma", presence_id=second)
    args = {"kind": "person", "target_id": env.person}
    result = await env.service.execute("read_conversation_history", args, env.context)
    assert result["error"] == "presence_ambiguous"
    assert {item["presence_id"] for item in result["presences"]} == {env.presence, second}
    assert not any("msg_history" in action for action, _ in (*env.bot.calls, *other.calls))
    other.history = {
        "data": {
            "messages": [
                {
                    "message_id": "1",
                    "raw_message": "你好[CQ:image,url=http://secret/file]",
                    "sender": {"nickname": "name", "user_id": 10001},
                }
            ]
        }
    }
    result = await env.service.execute(
        "read_conversation_history", {**args, "presence_id": second}, env.context
    )
    assert result["messages"][0]["text"] == "你好[image]"
    assert "http://secret" not in str(result)
    binding = await add_second_account(env)
    with pytest.raises(SocialError, match="binding_ambiguous"):
        await env.service.execute("read_conversation_history", args, env.context)
    result = await env.service.execute(
        "read_conversation_history",
        {**args, "binding_id": binding, "presence_id": second},
        env.context,
    )
    assert result["external_target_id"] == "10002"
    group = {"kind": "space", "target_id": env.space, "presence_id": env.presence, "limit": 1}
    env.bot.history = {"messages": []}
    result = await env.service.execute("read_conversation_history", group, env.context)
    assert result["count"] == 0 and result["messages"] == []
    assert env.bot.calls[-1] == ("get_group_msg_history", {"group_id": 20001, "count": 1})
    for invalid in (0, 51, True, "20"):
        with pytest.raises(SocialError, match="invalid_history_limit"):
            await env.service.execute(
                "read_conversation_history", {**group, "limit": invalid}, env.context
            )
    for malformed in (
        {"status": "failed", "retcode": 1},
        {"message_id": 99},
        {"messages": ["bad"]},
    ):
        env.bot.history = malformed
        with pytest.raises(SocialError, match=r"history_provider_failed|invalid_history_result"):
            await env.service.execute("read_conversation_history", group, env.context)
    env.bot.history = {"messages": []}
    with patch.object(env.service, "_call", AsyncMock(side_effect=TimeoutError("private URL"))):
        with pytest.raises(SocialError, match=r"^history_provider_failed$"):
            await env.service.execute("read_conversation_history", group, env.context)
    with patch.object(env.service, "_call", AsyncMock(side_effect=asyncio.CancelledError)):
        with pytest.raises(asyncio.CancelledError):
            await env.service.execute("read_conversation_history", group, env.context)
    adapter = SocialAutomationAdapter(env.service, None, None)
    invoke = adapter.mapping()["social.read_conversation_history"]
    context = SimpleNamespace(
        authority=SimpleNamespace(
            allowed_capabilities={"social.read_conversation_history"},
            delegated_authority=object(),
            actor_is_superuser=False,
        ),
        automation_run_id=1,
        step_id="read",
        canonical_conversation_id=env.context.conversation_id,
        canonical_target_space_id=env.space,
        canonical_target_person_id=None,
    )
    assert (await invoke(group, context)).data["count"] == 0
    with pytest.raises(SocialError, match="delegated_target_not_allowed"):
        await invoke({**args, "binding_id": binding, "presence_id": second}, context)
    context.authority.delegated_authority = None
    with pytest.raises(SocialError, match="capability_denied"):
        await invoke(group, context)
    async with env.db.sessions() as session, session.begin():
        presence = await session.get(PresenceModel, second)
        presence.enabled = False
    with pytest.raises(SocialError, match="history_presence_unavailable"):
        await env.service.execute(
            "read_conversation_history",
            {**args, "binding_id": binding, "presence_id": second},
            env.context,
        )
