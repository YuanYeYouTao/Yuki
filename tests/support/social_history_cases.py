"""Common history requests reach real canonical reads in each actual invocation."""

import json

from sqlalchemy import select

from yuki.persistence.models import ChatEventModel


async def history_agent_loop(env, *, same_batch=False):
    from tests.conftest import MemorySender, build_harness, make_settings

    from yuki.domain.conversations import ScopeType
    from yuki.domain.messages import (
        ChatResponse,
        InboundMessage,
        SenderIdentity,
        ToolCall,
        ToolFunction,
    )
    from yuki.llm.fake import FakeLLMProvider
    from yuki.services.main_agent_contract import MainAgentContract
    from yuki.workspace.short_state import ShortState

    calls = 0
    arguments = '{"view": "recent", "event_kind": "all"}'
    env.bot.history = {"messages": [{"message_id": 50, "message": "第一条"}]}

    def respond(request):
        nonlocal calls
        calls += 1
        returned = [
            json.loads(message.content) for message in request.messages if message.role == "tool"
        ]
        if calls == 2:
            assert returned[-1]["ok"], returned[-1]
            assert returned[-1]["data"]["source"] == "canonical_ledger"
            assert returned[-1]["data"]["returned_count"] >= 1
            if same_batch:
                assert len(returned) == 2 and all(
                    r["data"]["source"] == "canonical_ledger" for r in returned
                )
                return "读到两份原结果"
            env.bot.history["messages"].append({"message_id": 51, "message": "第二条"})
        if calls == 3:
            assert returned[-1]["data"]["source"] == "canonical_ledger"
            return "读到两条消息"
        return ChatResponse(
            content="",
            latency_seconds=0,
            tool_calls=tuple(
                ToolCall(
                    id=f"history-{calls}-{index}",
                    function=ToolFunction(name="query_chat_history", arguments=arguments),
                )
                for index in range(2 if same_batch else 1)
            ),
        )

    provider = FakeLLMProvider(respond)
    harness = build_harness(env.db, make_settings(env.db.url, enabled_groups_csv="20001"), provider)
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    chat.runtime.runner.main_contract = MainAgentContract(chat, ShortState(env.store))
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
            person_id=env.person,
            space_id=env.space,
            presence_id=env.presence,
        ),
        sender,
    )
    assert result.reason == "chat" and not sender.messages
    assert calls == (2 if same_batch else 3)
    assert not any(
        action in {"get_friend_msg_history", "get_group_msg_history", "get_forward_msg"}
        for action, _ in env.bot.calls
    )
    async with env.db.sessions() as session:
        assert not await session.scalar(
            select(ChatEventModel.id).where(ChatEventModel.platform_message_id.in_(("50", "51")))
        )
