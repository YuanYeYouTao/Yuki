"""Capacity accounts for model inputs without changing their actual protocol."""

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from qq_ai_bot.domain.messages import (
    ChatImage,
    ChatMessage,
    ChatRequest,
    ChatTool,
    FunctionCallOutput,
    NativeToolDefinition,
    NativeToolType,
    ProviderContinuation,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.llm.openai_responses import OpenAIResponsesProvider
from qq_ai_bot.model_runtime.capacity import (
    ModelCapacity,
    estimate_request_tokens,
    estimate_text_tokens,
    estimate_tools_tokens,
)
from qq_ai_bot.services.chat import ChatService


def declaration():
    return ChatTool(
        name="web_search",
        description="Find public sources.",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
    )


def initial_request():
    return ChatRequest(
        messages=(
            ChatMessage(role="system", content="Fixed contract."),
            *(ChatMessage(role="user", content=f"Historical event {i}.") for i in range(178)),
        ),
        model="gemini-3.8-flash",
        tools=(declaration(),),
        max_output_tokens=8192,
        tool_choice="auto",
    )


def host_metadata(tool):
    return replace(
        tool,
        namespace="directory" * 10000,
        aliases=("alias" * 10000,),
        use_when=("host guidance" * 10000,),
        tags=("tag" * 10000,),
        schema_version="version" * 10000,
        result_cacheable=False,
    )


def test_host_catalog_and_diagnostic_growth_does_not_consume_model_capacity():
    request = initial_request()
    noisy = replace(
        request,
        tools=(host_metadata(request.tools[0]),),
        conversation_prefix_hash="hash" * 10000,
        request_shape_hash="shape" * 10000,
        request_chain_id="chain" * 10000,
        prompt_snapshot_fingerprint="snapshot" * 10000,
        static_prompt_revision="revision" * 10000,
    )
    assert estimate_request_tokens(noisy) == estimate_request_tokens(request)
    assert estimate_tools_tokens(noisy.tools) == estimate_tools_tokens(request.tools)


@pytest.mark.parametrize("field", ["content", "schema", "reasoning", "call", "receipt"])
def test_real_model_input_growth_still_consumes_capacity(field):
    request = initial_request()
    large = "真实资料" * 3000
    if field == "schema":
        request = replace(
            request,
            tools=(replace(request.tools[0], parameters={"description": large}),),
        )
    else:
        message = {
            "content": ChatMessage(role="user", content=large),
            "reasoning": ChatMessage(role="assistant", reasoning_content=large),
            "call": ChatMessage(
                role="assistant", tool_calls=(ToolCall("call", ToolFunction("search", large)),)
            ),
            "receipt": ChatMessage(role="tool", tool_call_id="call", content=large),
        }[field]
        request = replace(request, messages=(*request.messages, message))
    assert estimate_request_tokens(request) > estimate_request_tokens(initial_request()) + 20000


def test_complete_opaque_state_counts_signatures_and_results_without_mutating_it():
    continuation = ProviderContinuation(
        provider="gemini",
        protocol="gemini",
        payload=(
            {
                "role": "model",
                "parts": [{"thoughtSignature": "original", "text": "answer"}],
                "_call_ids": ["original-call"],
            },
        ),
        profile_id="original-profile",
    )
    request = replace(
        initial_request(),
        continuation=continuation,
        continuation_items=(FunctionCallOutput("original-call", "original-result"),),
    )
    before = deepcopy(request)
    grown = replace(
        continuation,
        payload=({"parts": [{"thoughtSignature": "S" * 30000}]},),
    )
    assert estimate_request_tokens(replace(request, continuation=grown)) > (
        estimate_request_tokens(request) + 9000
    )
    assert (
        estimate_request_tokens(
            replace(request, continuation_items=(FunctionCallOutput("original-call", "R" * 30000),))
        )
        > estimate_request_tokens(request) + 9000
    )
    assert estimate_request_tokens(
        replace(request, continuation=replace(continuation, profile_id="route" * 10000))
    ) == estimate_request_tokens(request)
    assert request == before


@pytest.mark.parametrize("media_key", ["inlineData", "inline_data"])
def test_media_allowance_is_retained_for_portable_and_opaque_media(media_key):
    request = ChatRequest(messages=(ChatMessage(role="user", content="image"),))
    image = ChatImage("data:image/png;base64," + "A" * 400)
    with_image = replace(request, messages=(replace(request.messages[0], images=(image,)),))
    assert estimate_request_tokens(with_image) >= estimate_request_tokens(request) + 4096
    longer_image = replace(image, data_url="data:image/png;base64," + "A" * 100000)
    assert estimate_request_tokens(
        replace(request, messages=(replace(request.messages[0], images=(longer_image,)),))
    ) == estimate_request_tokens(with_image)
    opaque = ProviderContinuation(
        provider="gemini",
        protocol="gemini",
        payload=({"role": "user", "parts": [{media_key: {"data": "A" * 100000}}]},),
    )
    assert estimate_request_tokens(replace(request, continuation=opaque)) >= (
        estimate_request_tokens(request) + 4096
    )
    short_media = replace(
        opaque, payload=({"role": "user", "parts": [{media_key: {"data": "A"}}]},)
    )
    assert estimate_request_tokens(replace(request, continuation=short_media)) == (
        estimate_request_tokens(replace(request, continuation=opaque))
    )
    unknown = replace(opaque, payload=({media_key: {"data": "A" * 100000}},))
    assert estimate_request_tokens(replace(request, continuation=unknown)) > (
        estimate_request_tokens(replace(request, continuation=opaque)) + 20000
    )


def test_native_and_structured_output_declarations_remain_in_capacity():
    request = initial_request()
    native = replace(request, native_tools=(NativeToolDefinition(NativeToolType.WEB_SEARCH),))
    assert estimate_request_tokens(native) > estimate_request_tokens(request)
    structured = replace(
        native,
        structured_output=True,
        response_format={"type": "json_schema", "json_schema": {"schema": {"enum": ["x" * 30000]}}},
    )
    assert estimate_request_tokens(structured) > estimate_request_tokens(native) + 9000


@pytest.mark.parametrize("property_name", ["data_url", "inlineData", "inline_data"])
@pytest.mark.parametrize("request_field", ["tools", "response_format"])
def test_media_named_schema_properties_are_counted_as_complete_schema(property_name, request_field):
    tool = declaration()
    properties = {property_name: {"type": "string", "description": "original schema"}}
    initial = replace(tool, parameters={"type": "object", "properties": properties})
    grown = replace(
        initial,
        parameters={
            "type": "object",
            "properties": {property_name: {"type": "string", "description": "真实完整声明" * 3000}},
        },
    )
    if request_field == "tools":
        request = replace(initial_request(), tools=(initial,))
        grown_request = replace(request, tools=(grown,))
    else:
        request = replace(
            initial_request(),
            response_format={"type": "json_schema", "json_schema": {"schema": initial.parameters}},
        )
        grown_request = replace(
            request,
            response_format={"type": "json_schema", "json_schema": {"schema": grown.parameters}},
        )
    before = deepcopy(request)
    assert estimate_tools_tokens((grown,)) > estimate_tools_tokens((initial,)) + 20000
    assert estimate_request_tokens(grown_request) > estimate_request_tokens(request) + 20000
    assert request == before


@pytest.mark.asyncio
@pytest.mark.parametrize("property_name", ["data_url", "inlineData", "inline_data"])
@pytest.mark.parametrize("part_kind", ["functionCall", "functionResponse"])
async def test_gemini_opaque_business_json_is_fully_counted_and_wire_unchanged(
    property_name, part_kind
):
    provider = GeminiProvider(
        base_url="https://example.invalid/", api_key="unused", timeout_seconds=1, max_retries=0
    )
    try:

        def request(size):
            business = {property_name: {"notes": "real tool argument " * size}}
            part = (
                {"functionCall": {"name": "web_search", "args": business, "id": "call"}}
                if part_kind == "functionCall"
                else {
                    "functionResponse": {"name": "web_search", "response": business, "id": "call"}
                }
            )
            part["thoughtSignature"] = "original-signature"
            return replace(
                initial_request(),
                continuation=ProviderContinuation(
                    provider="gemini",
                    protocol="gemini",
                    payload=(
                        {
                            "role": "model" if part_kind == "functionCall" else "user",
                            "parts": [part],
                            "_call_ids": ["call"],
                        },
                    ),
                ),
            )

        small, large = request(1), request(10000)
        before = deepcopy(large)
        wire = provider._build_payload(large)
        assert estimate_request_tokens(large) > estimate_request_tokens(small) + 60000
        assert estimate_text_tokens(json.dumps(wire, ensure_ascii=False)) > (
            estimate_text_tokens(json.dumps(provider._build_payload(small), ensure_ascii=False))
            + 60000
        )
        assert large == before
        assert provider._build_payload(large) == wire
        assert wire["contents"][-1]["parts"][-1]["thoughtSignature"] == "original-signature"
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("property_name", ["data_url", "inlineData", "inline_data"])
async def test_anthropic_opaque_tool_input_is_not_mistaken_for_prepared_media(property_name):
    provider = AnthropicMessagesProvider(
        base_url="https://example.invalid/", api_key="unused", timeout_seconds=1, max_retries=0
    )
    try:

        def request(size):
            return replace(
                initial_request(),
                continuation=ProviderContinuation(
                    provider="anthropic",
                    protocol="anthropic_messages",
                    payload=(
                        {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "web_search",
                                    "id": "call",
                                    "input": {property_name: {"notes": "business data " * size}},
                                }
                            ],
                        },
                    ),
                ),
            )

        small, large = request(1), request(10000)
        before = deepcopy(large)
        wire = provider._build_payload(large)
        assert estimate_request_tokens(large) > estimate_request_tokens(small) + 40000
        assert estimate_text_tokens(json.dumps(wire, ensure_ascii=False)) > (
            estimate_text_tokens(json.dumps(provider._build_payload(small), ensure_ascii=False))
            + 40000
        )
        assert large == before
        assert provider._build_payload(large) == wire
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_gemini_estimation_preserves_actual_wire_prefix_and_original_signature():
    provider = GeminiProvider(
        base_url="https://example.invalid/", api_key="unused", timeout_seconds=1, max_retries=0
    )
    try:
        initial = initial_request()
        continuation = ProviderContinuation(
            provider="gemini",
            protocol="gemini",
            payload=(
                {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {
                                "name": "web_search",
                                "args": {"query": "eta"},
                                "id": "c1",
                            },
                            "thoughtSignature": "original-signature",
                        }
                    ],
                    "_call_ids": ["c1"],
                },
            ),
        )
        request = replace(
            initial,
            tools=(host_metadata(initial.tools[0]),),
            continuation=continuation,
            continuation_items=(FunctionCallOutput("c1", json.dumps({"sources": ["result"]})),),
        )
        before = deepcopy(request)
        initial_wire = provider._build_payload(initial)
        continued_wire = provider._build_payload(request)
        estimate_request_tokens(request)
        assert request == before
        assert provider._build_payload(request) == continued_wire
        assert (
            continued_wire["contents"][: len(initial_wire["contents"])] == initial_wire["contents"]
        )
        assert continued_wire["systemInstruction"] == initial_wire["systemInstruction"]
        assert continued_wire["tools"] == initial_wire["tools"]
        assert (
            continued_wire["contents"][-2]["parts"][0]["thoughtSignature"] == "original-signature"
        )
        assert continued_wire["contents"][-1]["parts"][0]["functionResponse"]["id"] == "c1"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_responses_opaque_input_replaces_budgeting_content_mirror_once():
    provider = OpenAIResponsesProvider(
        base_url="https://example.invalid/", api_key="unused", timeout_seconds=1, max_retries=0
    )
    try:
        item = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "original"}],
        }
        continuation = ProviderContinuation(
            provider="openai", protocol="responses", payload=(item,)
        )
        request = ChatRequest(
            messages=(ChatMessage(role="user", content="mirror", response_item=continuation),),
            model="example-model",
        )
        changed_mirror = replace(
            request, messages=(replace(request.messages[0], content="mirror" * 10000),)
        )
        before = deepcopy(request)
        assert provider._build_payload(request) == provider._build_payload(changed_mirror)
        assert estimate_request_tokens(request) == estimate_request_tokens(changed_mirror)
        assert provider._build_payload(request)["input"] == [item]
        grown_payload = replace(continuation, payload=({**item, "encrypted_content": "S" * 30000},))
        grown = replace(
            request, messages=(replace(request.messages[0], response_item=grown_payload),)
        )
        assert estimate_request_tokens(grown) > estimate_request_tokens(request) + 9000
        assert request == before
    finally:
        await provider.close()


def test_chat_history_tool_budget_uses_same_frozen_model_declaration_view():
    tools = (declaration(),)
    contract = SimpleNamespace(_tools=tools)
    service = SimpleNamespace(
        _models=SimpleNamespace(capacity=lambda _: ModelCapacity()),
        _settings=SimpleNamespace(system_prompt="fixed"),
        runtime=SimpleNamespace(runner=SimpleNamespace(main_contract=contract)),
    )
    runtime = SimpleNamespace(
        context=SimpleNamespace(window_tokens=96000, compaction_trigger_ratio=0.9),
        llm=SimpleNamespace(max_output_tokens=8192),
    )
    before = ChatService._history_input_budget(service, runtime)
    contract._tools = (host_metadata(tools[0]),)
    assert ChatService._history_input_budget(service, runtime) == before
    contract._tools = (replace(tools[0], description="真正工具合同" * 3000),)
    assert ChatService._history_input_budget(service, runtime) < before - 20000
