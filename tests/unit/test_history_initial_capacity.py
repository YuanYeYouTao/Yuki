"""An ordinary chain rebases before replaying snapshots beyond its input budget."""

import json
import math
from copy import copy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from tests.conftest import MemorySender
from tests.support.runtime_wire import install_wire
from tests.unit.test_history_dispatch_ownership import _scene, _tool

from qq_ai_bot.conversation.frozen_fragments import FrozenFragments
from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.conversation.projections import ProjectionSnapshot
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatResponse
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.model_runtime.capacity import ModelCapacity, estimate_request_tokens
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.services import main_agent_turns


@pytest.fixture
async def capacity_wire():
    clients = []

    def connect(chat, provider):
        client, captured = install_wire(chat, provider, "chat_completions")
        clients.append(client)
        return captured

    yield connect
    for client in clients:
        await client.aclose()


@pytest.mark.parametrize(
    "smaller_window,fresh_unfit,soft_only",
    [(False, False, False), (True, False, False), (True, True, False), (False, False, True)],
)
async def test_two_chinese_turns_rebase_large_frozen_snapshot_before_dispatch(
    database, tmp_path, monkeypatch, smaller_window, fresh_unfit, soft_only, caplog, capacity_wire
):
    provider = FakeLLMProvider()

    def respond(request):
        if request.structured_output:
            source = json.loads(request.messages[-1].content)
            return ChatResponse(
                json.dumps(
                    {
                        "facts": [{"text": "保留历史观察摘要", "refs": source["source_refs"]}],
                        "pending": [],
                        "next_steps": [],
                    }
                ),
                0,
            )
        count = sum(not item.structured_output for item in provider.requests)
        return (
            _tool("send_message", {"text": "收到。"}, f"send-{count}")
            if count % 2
            else ChatResponse("已发送。", 0)
        )

    provider._responder = respond
    env, harness, chat, state, message = await _scene(database, tmp_path, provider)
    http_requests = capacity_wire(chat, provider) if fresh_unfit else None
    if fresh_unfit:
        chat._tools.social_service = env.service
    if smaller_window:
        # A small fixed declaration makes the smaller ordinary window viable.
        # The same manifest is used for both admitted chains and their tails.
        contract = chat.runtime.runner.main_contract
        contract._tools = tuple(
            tool for tool in await contract.definitions() if tool.name == "send_message"
        )
    message = replace(message, text="中文提问" * 75)
    old_snapshot = "old-public-material-" * 7500 if smaller_window else "旧资料" * 3000
    if soft_only:
        old_snapshot = "旧公开运行资料" * 9000
    current_snapshot = {"text": old_snapshot}
    read_state = state.snapshot

    def runtime_state():
        return [
            *read_state(),
            {"slot": 2, "text": current_snapshot["text"], "revision": 1},
        ]

    monkeypatch.setattr(state, "snapshot", runtime_state)
    window = {"value": None}
    snapshot = chat._runtime_config.snapshot

    async def configured_snapshot(*args, **kwargs):
        runtime = await snapshot(*args, **kwargs)
        if soft_only:
            return replace(
                runtime,
                context=replace(
                    runtime.context, window_tokens=524288, compaction_window_tokens=90000
                ),
            )
        return (
            replace(runtime, context=replace(runtime.context, window_tokens=window["value"]))
            if window["value"] is not None
            else runtime
        )

    monkeypatch.setattr(chat._runtime_config, "snapshot", configured_snapshot)
    capacity = {"value": ModelCapacity()}
    monkeypatch.setattr(chat.runtime.runner._models, "capacity", lambda task: capacity["value"])
    compose = chat.runtime.main_turns.compose
    compositions = []
    fresh_estimates = []

    async def bounded_compose(**kwargs):
        if fresh_unfit and len(compositions) == 1:
            # Reproduce the planner's compiled fresh input before it substitutes
            # the oversized old epoch. The low connection ceiling is an isolated
            # test boundary; no real profile or model window is changed.
            fresh = chat._prompt_composer.compose(
                **{key: value for key, value in kwargs.items() if key != "before_preparation"},
                short_state=state.snapshot(),
            )
            runtime = kwargs["runtime"]
            fresh_estimates.append(
                estimate_request_tokens(
                    ChatRequest(
                        messages=fresh.messages,
                        model=runtime.llm.model or "fake",
                        temperature=runtime.llm.temperature,
                        max_output_tokens=runtime.llm.max_output_tokens,
                        thinking_enabled=runtime.llm.thinking_enabled,
                        tools=await chat.runtime.runner.main_contract.definitions(),
                        tool_choice="auto",
                    )
                )
            )
        composition = await compose(**kwargs)
        compositions.append(composition)
        if len(compositions) == 1:
            # Set the effective capacity before the first actual dispatch, from
            # the real compiled system/messages and fixed function declarations.
            # It stays unchanged for both ordinary turns and all continuations.
            runtime = kwargs["runtime"]
            request = ChatRequest(
                messages=composition.messages,
                model=runtime.llm.model or "fake",
                temperature=runtime.llm.temperature,
                max_output_tokens=runtime.llm.max_output_tokens,
                thinking_enabled=runtime.llm.thinking_enabled,
                tools=await chat.runtime.runner.main_contract.definitions(),
                tool_choice="auto",
            )
            base = estimate_request_tokens(request)
            margin = 55000 if smaller_window else 15000
            budget = (
                524288
                if soft_only
                else math.ceil((base + margin + 4096) / runtime.context.compaction_trigger_ratio)
            )
            capacity["value"] = ModelCapacity(input_tokens=budget)
            assert base <= int(budget * runtime.context.compaction_trigger_ratio) - 4096
        return composition

    monkeypatch.setattr(chat.runtime.main_turns, "compose", bounded_compose)
    result = await harness.processor.handle(message, MemorySender())
    assert result.reason == "chat" and len(provider.requests) == 2
    first_request = provider.requests[0]
    original_input = tuple(first_request.messages)
    async with database.sessions() as reader:
        saved = (await reader.scalars(select(PromptProjectionModel))).one()
        original_epoch, original_revision, original_payload = (
            saved.epoch_id,
            saved.revision,
            saved.payload_json,
        )
        assert old_snapshot in original_payload
    budget = capacity["value"].input_tokens
    assert all(estimate_request_tokens(request) <= budget for request in provider.requests)
    if soft_only:
        # The persisted public snapshot is legal under the enlarged hard limit.
        # Reopening must still rebase it at the independent maintenance window.
        assert 90000 < estimate_request_tokens(first_request) < budget
    state.update({"slot": 1, "text": "current-safe-snapshot", "expected_revision": 1})
    current_snapshot["text"] = "current-safe-runtime"
    if smaller_window:
        window["value"] = 40000
        # The previous snapshot alone exceeds the new compiler character limit.
        # A fit check must select a capacity epoch before trying to compile it.
        assert len(old_snapshot) > window["value"] * 3 + 12000
    if fresh_unfit:
        # The previous epoch is not recoverable by any rebase because even the
        # fresh request cannot fit this lower synthetic connection ceiling.
        # Copy only Runner's executor so assembly still gets its original
        # metadata budget; this isolates projection/compilation from assembly.
        models = copy(chat.runtime.runner._models)
        monkeypatch.setattr(models, "capacity", lambda task: ModelCapacity(input_tokens=1))
        chat.runtime.runner._models = models
    await database.close()
    sender = MemorySender()
    result = await harness.processor.handle(
        replace(message, message_id="capacity-next", text="当前追问" * 75), sender
    )
    if fresh_unfit:
        assert fresh_estimates and fresh_estimates[-1] > 1
        assert result.reason == "capacity_failure"
        assert len(sender.messages) == 1
        assert "容量限制" in sender.messages[0].text
        assert "已有结果会保留" in sender.messages[0].text
        assert "内部错误" not in sender.messages[0].text
        assert "exception_category=WorkCapacityError" in caplog.text
        assert len(provider.requests) == 2 and len(http_requests) == 2
        assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1
        async with database.sessions() as reader:
            saved = (await reader.scalars(select(PromptProjectionModel))).one()
            assert (saved.epoch_id, saved.revision, saved.payload_json) == (
                original_epoch,
                original_revision,
                original_payload,
            )
            assert saved.invalidated_reason is None
            original_outbound = (
                await reader.scalars(
                    select(ChatEventModel).where(
                        ChatEventModel.canonical_conversation_id == env.context.conversation_id,
                        ChatEventModel.direction == "outbound",
                        ChatEventModel.content == "收到。",
                    )
                )
            ).all()
            assert len(original_outbound) == 1
            assert (await reader.execute(select(work))).first() is None
        assert first_request.messages == original_input and old_snapshot in original_payload
        return
    main_requests = [item for item in provider.requests if not item.structured_output]
    assert result.reason == "chat" and len(main_requests) == 4
    assert main_requests[2].tools == first_request.tools
    assert tuple(item for item in main_requests[2].messages if item.role == "system") == (
        tuple(item for item in first_request.messages if item.role == "system")
    )
    assert capacity["value"].input_tokens == budget
    assert all(estimate_request_tokens(request) <= budget for request in provider.requests)
    if smaller_window:
        assert all(
            estimate_request_tokens(request) <= window["value"] for request in main_requests[2:]
        )
    next_input = json.dumps(
        [item.content for item in main_requests[2].messages], ensure_ascii=False
    )
    assert "current-safe-runtime" in next_input and "current-safe-snapshot" in next_input
    if not smaller_window and not soft_only:
        # This small old snapshot still fits the unchanged maintenance window.
        # Its bytes remain frozen while the fresh state is appended at the end.
        assert old_snapshot in next_input
        async with database.sessions() as reader:
            saved = (await reader.scalars(select(PromptProjectionModel))).one()
            assert saved.epoch_id == original_epoch
        assert first_request.messages == original_input
        return
    assert old_snapshot not in next_input and "original-snapshot" not in next_input
    async with database.sessions() as reader:
        saved = (await reader.scalars(select(PromptProjectionModel))).one()
        assert saved.epoch_id != original_epoch
        assert saved.revision == 1 and saved.rebuild_reason == "capacity"
        assert old_snapshot not in saved.payload_json
    # The old admitted request and its original saved snapshot were not edited
    # to make the second chain fit. Reopening SQLite only creates a new epoch.
    assert first_request.messages == original_input
    assert old_snapshot in original_payload
    assert old_snapshot in json.dumps([item.content for item in original_input], ensure_ascii=False)


@pytest.mark.parametrize("raw_only", [False, True])
async def test_unreachable_soft_reserve_does_not_rebuild_a_hard_fitting_epoch(
    database, tmp_path, monkeypatch, raw_only
):
    provider = FakeLLMProvider()
    provider._responder = lambda request: (
        _tool("send_message", {"text": "收到。"}, "send")
        if len(provider.requests) == 1
        else "已发送。"
    )
    _, harness, chat, _, message = await _scene(database, tmp_path, provider)
    compose = chat.runtime.main_turns.compose
    captured = []

    async def capture(**kwargs):
        captured.append(kwargs)
        return await compose(**kwargs)

    monkeypatch.setattr(chat.runtime.main_turns, "compose", capture)
    assert (await harness.processor.handle(message, MemorySender())).reason == "chat"
    kwargs = captured[0]
    context = kwargs["context"]
    # Test preparation only, with the real admitted event as the selected raw
    # history. No second source or model execution is admitted by this fixture.
    event = context.current_event_id
    raw = (((event,), context.current_message),)
    context = replace(
        context,
        current_event_id=event + 100,
        current_message=ChatMessage(role="user", content="当前问题"),
        history_messages=(context.current_message,),
        history_fragments=raw,
        history_event_fragments=raw,
        visible_event_ids=frozenset({event, event + 100}),
    )
    kwargs = {**kwargs, "context": context}
    runtime = kwargs["runtime"]
    fresh = chat._prompt_composer.compose(
        **{key: value for key, value in kwargs.items() if key != "before_preparation"},
        short_state=chat.runtime.runner.main_contract.state.snapshot(),
    )
    request = ChatRequest(
        messages=fresh.messages,
        model=runtime.llm.model or "fake",
        temperature=runtime.llm.temperature,
        max_output_tokens=runtime.llm.max_output_tokens,
        thinking_enabled=runtime.llm.thinking_enabled,
        tools=await chat.runtime.runner.main_contract.definitions(),
        tool_choice="auto",
    )
    fresh_tokens = estimate_request_tokens(request)
    budget = fresh_tokens + 5000
    soft_window = max(1, fresh_tokens - 1)
    kwargs = {
        **kwargs,
        "runtime": replace(
            runtime, context=replace(runtime.context, compaction_window_tokens=soft_window)
        ),
    }
    assert soft_window < fresh_tokens < budget
    monkeypatch.setattr(
        chat.runtime.runner._models, "capacity", lambda task: ModelCapacity(input_tokens=budget)
    )
    previous = FrozenFragments.load([]).extend_history(raw, raw)
    if not raw_only:
        previous = previous.append_protocol(
            (ChatMessage(role="user", content='runtime_state:{"state":"no_active_work"}'),)
        )
    async with database.sessions() as reader:
        saved = (await reader.scalars(select(PromptProjectionModel))).one()
        snapshot = ProjectionSnapshot(
            epoch_id=saved.epoch_id,
            revision=saved.revision,
            generation=saved.generation,
            context_key=saved.context_key,
            contract_revision=saved.contract_revision,
            payload_json=json.dumps(list(previous.items)),
            rebuild_reason=saved.rebuild_reason,
            source_revision=saved.source_revision,
        )
    monkeypatch.setattr(
        chat.runtime.main_turns._projections, "read", AsyncMock(return_value=snapshot)
    )
    preparations = []
    prepare = main_agent_turns.prepare_history

    async def record(*args, **kwargs):
        result = await prepare(*args, **kwargs)
        preparations.append(result)
        return result

    monkeypatch.setattr(main_agent_turns, "prepare_history", record)
    for _ in range(2):
        composition = await compose(**kwargs)
        assert preparations[-1].reason is None
        assert preparations[-1].previous is not None
        actual = replace(request, messages=composition.messages)
        assert estimate_request_tokens(actual) <= budget
        if not raw_only:
            assert "original-snapshot" in json.dumps([item.content for item in actual.messages])
            assert "no_active_work" in json.dumps([item.content for item in actual.messages])
    assert len(preparations) == 2
    assert len(provider.requests) == 2
