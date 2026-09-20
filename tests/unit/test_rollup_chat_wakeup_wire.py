import json

import pytest


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
@pytest.mark.asyncio
async def test_rollup_interrupt_reenters_main_contract(database, tmp_path, monkeypatch, protocol):
    from dataclasses import replace

    from tests.conftest import MemorySender, build_harness, make_settings
    from tests.unit.test_commands_and_chat import inbound

    from qq_ai_bot.conversation.hydrate import ensure_canonical_conversation
    from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
    from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence, ensure_space
    from qq_ai_bot.llm.fake import FakeLLMProvider
    from qq_ai_bot.services.main_agent_contract import MainAgentContract
    from qq_ai_bot.workspace.short_state import ShortState
    from qq_ai_bot.workspace.store import WorkspaceStore

    steps = iter(
        [
            ("request_tools", {"query": "读取记录"}),
            ("request_tools", {"query": "读取记录"}),
            (None, None),
        ]
    )

    def respond(request):
        name, arguments = next(steps)
        if name is None:
            return ChatResponse("已经记录好了。", 0)
        return ChatResponse(
            "",
            0,
            tool_calls=(
                ToolCall(str(len(provider.requests)), ToolFunction(name, json.dumps(arguments))),
            ),
        )

    provider = FakeLLMProvider(respond)
    harness = build_harness(
        database, make_settings(database.url, runtime_work_enabled=True), provider
    )
    chat = harness.processor._chat
    consumed = []
    chat.rollup_wakeups.on_consumed = lambda cid, event_id: consumed.append((cid, event_id))
    state = ShortState(WorkspaceStore(tmp_path / "short-state"))
    chat._agent_runner.main_contract = MainAgentContract(chat, state)
    chat._tools.short_state = state
    async with database.sessions() as session, session.begin():
        person = await ensure_person(session, "1001")
        presence = await ensure_presence(session, "9999")
        space = await ensure_space(session, "2001")
        conversation = await ensure_canonical_conversation(
            session, kind="space", primary_scope_key="group:9999:2001", space_id=space
        )
    message = replace(
        inbound("为什么叫Yuki", message_id="real-work", group_id="2001", mentions_bot=True),
        conversation_id=conversation.conversation_id,
        legacy_conversation_key="group:9999:2001",
        space_id=space,
        person_id=person,
        presence_id=presence,
    )
    sender = MemorySender()
    from tests.support.runtime_wire import install_wire

    wire, captured = install_wire(chat, provider, protocol)
    from datetime import UTC, datetime

    from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationRollupModel

    validator = chat._context_validator
    calls = 0

    def instrumented(*args, **kwargs):
        validate = validator(*args, **kwargs)

        async def check():
            nonlocal calls
            calls += 1
            if calls == 2:
                async with database.sessions() as session, session.begin():
                    session.add(
                        CanonicalConversationRollupModel(
                            conversation_id=conversation.conversation_id,
                            generation=1,
                            covered_through_event_id=0,
                            summary_text="earlier discussion",
                            summary_kind="model",
                            source_fingerprint="0" * 64,
                            revision=1,
                            created_at=datetime.now(UTC),
                            updated_at=datetime.now(UTC),
                        )
                    )
                await harness.ledger.append_inbound(
                    replace(
                        message,
                        message_id="during-rollup",
                        text="等待期间的新补充",
                        source_event_id=None,
                    ),
                    bot_user_id="9999",
                )
            await validate()

        return check

    monkeypatch.setattr(chat, "_context_validator", instrumented)
    result = await harness.processor.handle(message, sender)
    await wire.aclose()
    assert result.reason == "chat", result
    assert sender.messages

    assert len(provider.requests) == 3
    assert any("等待期间的新补充" in (m.content or "") for m in provider.requests[1].messages)
    assert provider.requests[0].tools == provider.requests[1].tools == provider.requests[2].tools
    assert provider.requests[1].native_tools == provider.requests[2].native_tools
    assert not chat.rollup_wakeups.states
    assert captured[0]["tools"] == captured[1]["tools"] == captured[2]["tools"]
    history_key = "input" if protocol == "responses" else "messages"
    assert captured[2][history_key][: len(captured[1][history_key])] == captured[1][history_key]
    assert len(consumed) == 1 and consumed[0][1] > 0
