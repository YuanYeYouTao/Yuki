"""Initial preparation and dispatch use the same authorized tool declarations."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.conftest import build_harness, make_settings
from tests.unit.test_context_observation_sources import add_clue, context_for
from tests.unit.test_history_dispatch_ownership import _scene

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.conversation.observations import ContextObservationRepository
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatResponse, ChatTool
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.memory.context import MEMORY_GROUNDING_RULE, entity_memory_rule
from qq_ai_bot.model_runtime.capacity import ModelCapacity, estimate_request_tokens
from qq_ai_bot.model_runtime.models import ModelCapability, ModelProtocol, ModelSearchMode
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.web.models import WebMode


@pytest.mark.parametrize(
    "protocol,search_mode,allowed,expected_functions,has_native",
    [
        (ModelProtocol.GEMINI, ModelSearchMode.NATIVE, True, {"inspect"}, True),
        (ModelProtocol.GEMINI, ModelSearchMode.NATIVE, False, {"inspect"}, False),
        (
            ModelProtocol.GEMINI,
            ModelSearchMode.BOTH,
            True,
            {"inspect", "web_search", "read_webpage"},
            True,
        ),
        (
            ModelProtocol.GEMINI,
            ModelSearchMode.EXTERNAL,
            True,
            {"inspect", "web_search", "read_webpage"},
            False,
        ),
        (
            ModelProtocol.ANTHROPIC_MESSAGES,
            ModelSearchMode.BOTH,
            True,
            {"inspect", "read_webpage"},
            True,
        ),
        (ModelProtocol.RESPONSES, ModelSearchMode.NATIVE, True, {"inspect"}, True),
    ],
)
async def test_initial_prepared_tool_cost_matches_actual_first_runner_request(
    database, protocol, search_mode, allowed, expected_functions, has_native
):
    provider = FakeLLMProvider(lambda _: ChatResponse("done", 0))
    harness = build_harness(database, make_settings(database.url), provider)
    chat = harness.processor._chat
    runner = chat.runtime.runner
    runner._models.protocol = lambda _: protocol
    runner._models.search_mode = lambda _: search_mode
    runner._models.capabilities = lambda _: frozenset(
        {ModelCapability.TOOLS, ModelCapability.NATIVE_WEB_SEARCH}
    )
    config = await chat._runtime_config.snapshot()
    config = replace(config, web=replace(config.web, mode=WebMode.BOTH.value))
    declarations = (
        ChatTool("inspect", "inspect", {"type": "object"}),
        ChatTool("web_search", "search " * 500, {"type": "object"}),
        ChatTool("read_webpage", "read " * 500, {"type": "object"}),
    )
    capabilities = frozenset({"web_search"}) if allowed else frozenset()
    functions, native = runner.prepare_request_tools(
        declarations, runtime_config=config, allowed_capabilities=capabilities
    )
    assert {tool.name for tool in functions} == expected_functions
    assert bool(native) is has_native
    messages = (ChatMessage("system", "fixed"), ChatMessage("user", "current"))
    prepared = runner._capacity_request(
        ChatRequest(
            messages=messages,
            model=config.llm.model or "fake",
            temperature=config.llm.temperature,
            max_output_tokens=config.llm.max_output_tokens,
            thinking_enabled=config.llm.thinking_enabled,
            tools=functions,
            native_tools=native,
            tool_choice="auto" if functions or native else None,
        )
    )
    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="prepared-tools",
        current_group_id=None,
        bot_user_id="8000",
        gateway=None,
        runtime_config=config,
        current_time=chat._time.current_default(),
        allowed_capabilities=capabilities,
        max_tool_calls=2,
        max_model_requests=2,
        fixed_tools=declarations,
    )
    result = await runner.run(messages, runtime, SimpleNamespace(finalize=lambda text, _: text))
    assert result.text == "done" and len(provider.requests) == 1
    actual = provider.requests[0]
    assert actual.tools == prepared.tools and actual.native_tools == prepared.native_tools
    assert estimate_request_tokens(actual) == estimate_request_tokens(prepared)
    assert tuple(tool.name for tool in declarations) == ("inspect", "web_search", "read_webpage")
    if search_mode is ModelSearchMode.NATIVE:
        # The old full-function estimate could force an unnecessary foreground
        # summary even though the actual native-only request fits this capacity.
        old = replace(prepared, tools=declarations, native_tools=())
        assert estimate_request_tokens(old) > estimate_request_tokens(actual)


async def test_main_native_only_hard_fit_does_not_buy_foreground_observation_summary(
    database, tmp_path, monkeypatch
):
    provider = FakeLLMProvider(lambda _: pytest.fail("fit preparation must not call a model"))
    env, _harness, chat, state, message = await _scene(database, tmp_path, provider)
    runner = chat.runtime.runner
    # The fake host has no search provider. Include the real built-in search
    # schemas in its frozen manifest without executing any network operation.
    monkeypatch.setattr(chat._tools, "_web_catalog_enabled", lambda: True)
    monkeypatch.setattr(runner._models, "protocol", lambda _: ModelProtocol.GEMINI)
    monkeypatch.setattr(
        runner._models, "search_mode", lambda _: ModelSearchMode.NATIVE, raising=False
    )
    await add_clue(database, env, "native-boundary-clue", size=6000)
    context = replace(await context_for(database), projection_scope="main")
    runtime = await chat._runtime_config.snapshot()
    runtime = replace(runtime, web=replace(runtime.web, mode=WebMode.BOTH.value))
    allowed = chat.web_capabilities(runtime)
    definitions = await runner.main_contract.definitions()
    functions, native = runner.prepare_request_tools(
        definitions, runtime_config=runtime, allowed_capabilities=allowed
    )
    assert native and {"web_search", "read_webpage"}.isdisjoint(tool.name for tool in functions)
    fresh = chat._prompt_composer.compose(
        inbound=message,
        context=context,
        runtime=runtime,
        visual_observation=None,
        visual_failure=False,
        short_state=state.snapshot(),
    )
    observations = await ContextObservationRepository(database).read(
        conversation_id=env.context.conversation_id,
        generation=1,
        actor_id=env.person,
        read_scope="main",
    )
    assert len(observations) == 1
    messages = (
        *fresh.messages[:-1],
        *(row.message() for row in observations),
        *fresh.messages[-1:],
    )
    actual = runner._capacity_request(
        ChatRequest(
            messages=messages,
            model=runtime.llm.model or "fake",
            temperature=runtime.llm.temperature,
            max_output_tokens=runtime.llm.max_output_tokens,
            thinking_enabled=runtime.llm.thinking_enabled,
            tools=functions,
            native_tools=native,
            tool_choice="auto",
        )
    )
    actual_cost = estimate_request_tokens(actual)
    budget = actual_cost + 1
    old_cost = estimate_request_tokens(replace(actual, tools=definitions, native_tools=()))
    assert actual_cost <= budget < old_cost
    assert actual_cost > int(budget * runtime.context.compaction_trigger_ratio) - 4096
    monkeypatch.setattr(runner._models, "capacity", lambda _: ModelCapacity(input_tokens=budget))
    composition = await chat.runtime.main_turns.compose(
        inbound=message,
        context=context,
        runtime=runtime,
        visual_observation=None,
        visual_failure=False,
        read_scope="main",
        allowed_capabilities=allowed,
    )
    assert "native-boundary-clue" in str(composition.messages)
    assert "x" * 6000 in str(composition.messages)
    assert composition.preparation_model_requests == 0
    assert provider.requests == []
    request = replace(actual, messages=composition.messages)
    assert estimate_request_tokens(request) <= budget


@pytest.mark.parametrize("cached_manifest", [True, False])
async def test_history_budget_uses_the_complete_single_static_prompt(
    database, tmp_path, monkeypatch, cached_manifest
):
    _env, _harness, chat, _state, message = await _scene(database, tmp_path, FakeLLMProvider())
    runner = chat.runtime.runner
    runtime = await chat._runtime_config.snapshot()
    runtime = replace(runtime, context=replace(runtime.context, window_tokens=524288))
    monkeypatch.setattr(runner._models, "capacity", lambda _: ModelCapacity(input_tokens=524288))
    if cached_manifest:
        await runner.main_contract.definitions()
    assert bool(runner.main_contract._tools) is cached_manifest
    static = chat._prompt_composer.static_messages()
    assert len(static) == 1 and static[0].role == "system"
    assert static[0].content.count(entity_memory_rule(chat._settings.bot_display_name)) == 1
    assert static[0].content.count(MEMORY_GROUNDING_RULE) == 1
    compiled = chat._prompt_composer.compose(
        inbound=message,
        context=await context_for(database),
        runtime=runtime,
        visual_observation=None,
        visual_failure=False,
    )
    assert compiled.messages[0] == static[0]
    captured = []
    normalize = runner._capacity_request

    def capture(request):
        captured.append(request)
        return normalize(request)

    monkeypatch.setattr(runner, "_capacity_request", capture)
    hard = chat._history_input_budget(runtime, maintenance=False)
    assert len(captured) == 1 and captured[0].messages == static
    template = normalize(captured[0])
    fallback = 0 if cached_manifest else 32768
    fixed = estimate_request_tokens(template) + fallback
    assert hard == 524288 - fixed
    # Increasing the soft base is a policy change, not a new hard ceiling or a
    # duplicated system contribution in either budget calculation.
    larger = replace(
        runtime,
        context=replace(
            runtime.context, compaction_window_tokens=runtime.context.compaction_window_tokens * 2
        ),
    )
    assert chat._history_input_budget(larger, maintenance=False) == hard
    for policy in (runtime, larger):
        soft = chat._history_input_budget(policy)
        assert soft == max(
            1,
            int(
                min(524288, policy.context.compaction_window_tokens)
                * policy.context.compaction_trigger_ratio
            )
            - fixed
            - 4096,
        )
    assert all(request.messages == static for request in captured)
