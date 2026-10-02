import json

import pytest


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
@pytest.mark.asyncio
async def test_rollup_update_keeps_main_chain_and_observes_new_input(
    database, tmp_path, monkeypatch, protocol
):
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
            ("get_my_capabilities", {"mode": "summary"}),
            ("get_my_capabilities", {"mode": "summary"}),
            ("send_message", {"text": "已经记录好了。"}),
            (None, None),
        ]
    )

    def respond(request):
        name, arguments = next(steps)
        if name is None:
            return ChatResponse("内部完成回执", 0)
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
    chat.runtime.runner.main_contract = MainAgentContract(chat, state)
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
            if calls == 3:
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
    assert not sender.messages

    assert len(provider.requests) == 4
    assert [
        json.dumps(body, ensure_ascii=False).count("等待期间的新补充") for body in captured
    ] == [0, 1, 1, 1]
    assert len({request.request_chain_id for request in provider.requests}) == 1
    assert all(request.tools == provider.requests[0].tools for request in provider.requests[1:])
    assert all(
        request.native_tools == provider.requests[1].native_tools
        for request in provider.requests[2:]
    )
    assert not chat.rollup_wakeups.states
    assert len({json.dumps(item["tools"], sort_keys=True) for item in captured}) == 1
    assert '"name":"send_message"' in json.dumps(captured, separators=(",", ":"))
    history_key = "input" if protocol == "responses" else "messages"
    assert captured[3][history_key][: len(captured[1][history_key])] == captured[1][history_key]
    # No rollup interruption/wakeup: the original loop observes the event at its
    # next paired boundary, so there is no separate wakeup consumer.
    assert consumed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["source_only", "mixed_failure", "owned_work"])
async def test_parallel_source_change_preserves_retry_owner_and_other_failures(
    database, tmp_path, monkeypatch, case
):
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tests.unit.test_work_reporting_runner import case as runner_case

    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.persistence.event_repository import ConversationReadVersion
    from qq_ai_bot.runtime.activation_outcome import ActivationOutcome, ExitReason
    from qq_ai_bot.services.agent_runner import AgentRunner
    from qq_ai_bot.services.turn_coordinator import HistorySourceChangedError

    source = HistorySourceChangedError(
        ConversationReadVersion(
            ConversationScope.group("9999", "2001"),
            "original-conversation",
            3,
            17,
            rollup_stamp=(8, 0),
        )
    )
    errors = [ExceptionGroup("parallel read", [source])]
    if case == "mixed_failure":
        errors.append(ValueError("a separate tool failed"))
    group = ExceptionGroup("tool batch", errors)
    runner = object.__new__(AgentRunner)

    async def fail(*_args):
        raise group

    monkeypatch.setattr(runner, "_run", fail)
    control = (
        SimpleNamespace(
            current={"id": "original-work"},
            requests_started=2,
            tools_started=1,
            ending="queued",
            recover_failure=AsyncMock(return_value=ActivationOutcome(ExitReason.RETRY)),
        )
        if case == "owned_work"
        else None
    )
    fixture = await runner_case(database, tmp_path, [], reporting="quiet")
    runtime = replace(fixture.runtime, work_control=control, max_model_requests=4)
    if control is not None:
        result = await runner._run_with_receipts((), runtime, None)
        assert result.suppress_delivery
        assert result.model_requests == 2
        control.recover_failure.assert_awaited_once_with(group)
    elif case == "mixed_failure":
        with pytest.raises(ExceptionGroup) as caught:
            await runner._run_with_receipts((), runtime, None)
        assert caught.value is group
    else:
        with pytest.raises(HistorySourceChangedError) as caught:
            await runner._run_with_receipts((), runtime, None)
        assert caught.value is source
        assert caught.value.version.rollup_stamp == (8, 0)
