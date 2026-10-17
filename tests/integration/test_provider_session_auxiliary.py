"""Known auxiliary owners travel as HTTP metadata through actual serializers."""

import json
from dataclasses import replace

import httpx
import pytest
from pydantic import BaseModel
from tests.integration.test_current_material_main_contract import _catalog, _profile
from tests.support.social_identity_cases import social_env
from tests.support.work_compaction_capacity_helpers import _runtime, _session

from qq_ai_bot.conversation.frozen_fragments import FrozenFragments
from qq_ai_bot.conversation.projections import PromptProjectionRepository
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.llm.openai_responses import OpenAICompatibleResponsesProvider
from qq_ai_bot.model_runtime.dispatch_guard import model_dispatch_guard
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelExecutionPriority,
    ModelProtocol,
    ModelTask,
    StructuredOutputMode,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.model_runtime.structured import StructuredTaskRunner
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.services.context_compaction import PublicCompactContext, render_context_checkpoint
from qq_ai_bot.services.turn_execution import TurnExecution
from qq_ai_bot.web.bridge_state import BridgeState
from qq_ai_bot.web.models import WebMode, WebSearchRequest
from qq_ai_bot.web.native_bridge import NativeSearchBridge
from qq_ai_bot.web.page_extractor import DirectPageExtractor


class _StructuredResult(BaseModel):
    value: str
    source_ref: str


async def _public_context(database, initial):
    version, events = await EventLedgerRepository(database).read_scope_context(
        ConversationScope.group("80001", "20001"), limit=80
    )
    fragments = FrozenFragments.load([]).extend_history(
        tuple(((event.id,), ChatMessage("user", event.content)) for event in events), ()
    )

    async def adopt(summary, coverage, retained):
        return (
            initial[0],
            render_context_checkpoint(summary, coverage),
            *retained.messages(),
            ChatMessage("user", "private runtime and trigger"),
        )

    return PublicCompactContext(
        (initial[0], *fragments.messages()),
        1,
        fragments,
        version,
        "provider-session-test",
        PromptProjectionRepository(database),
        adopt,
    )


@pytest.mark.parametrize(
    "kind", ["execution_compact", "public_compact", "structured_main", "work_summary", "utility"]
)
async def test_auxiliary_session_header_keeps_original_body(database, tmp_path, kind):
    wire, headers, neutral, responses = [], [], [], []

    def transport(request):
        wire.append(json.loads(request.content))
        headers.append(request.headers.get("x-session-id"))
        source = wire[-1]["contents"][-1]["parts"][-1]["text"]
        if kind in {"execution_compact", "public_compact"}:
            content = "保留原目标、已验证发现和原内部来源引用。"
        else:
            original = json.loads(source)
            source_ref = (
                f"work:{original['work_id']}"
                if "work_id" in original
                else f"event:{original['event_id']}"
            )
            content = json.dumps({"value": "original structured result", "source_ref": source_ref})
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"role": "model", "parts": [{"text": content}]},
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 137,
                    "candidatesTokenCount": 21,
                    "cachedContentTokenCount": 53,
                    "thoughtsTokenCount": 7,
                    "totalTokenCount": 165,
                },
            },
        )

    class ObservedGemini(GeminiProvider):
        async def complete(self, request):
            neutral.append(request)
            response = await super().complete(request)
            responses.append(response)
            return response

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/", transport=httpx.MockTransport(transport)
    ) as client:
        provider = ObservedGemini(
            base_url="https://wire.invalid/",
            api_key="synthetic",
            timeout_seconds=2,
            max_retries=0,
            client=client,
        )
        models = TaskModelExecutor(
            router=ModelRouter(_catalog(_profile())),
            pool=ModelClientPool(injected_profiles={"main": provider}),
        )
        control = None
        try:
            if kind in {"execution_compact", "public_compact", "work_summary"}:
                control, session, initial = await _session(database, tmp_path)
                work_owner = (
                    f"yuki:{control.lease.conversation_id}:g{control.lease.generation}:"
                    f"work:{control.current['id']}"
                )
                owner = work_owner
                if kind in {"execution_compact", "public_compact"}:
                    session.transcript.append(ChatMessage("assistant", "原先的调查过程" * 1000))
                    runner, runtime = await _runtime(
                        database,
                        control,
                        initial,
                        provider,
                        contract_workspace=tmp_path / "contract",
                    )
                    runner._models = models
                    if kind == "public_compact":
                        context = await _public_context(database, initial)
                        initial = (
                            *context.messages,
                            ChatMessage("user", "private runtime and trigger"),
                        )
                        runtime = replace(
                            runtime,
                            work_control=None,
                            public_compaction=context,
                            execution_id="event:public-compact",
                            canonical_conversation_id=context.version.conversation_id,
                            conversation_generation=context.version.generation,
                            auxiliary_requests=[0],
                        )
                        owner = (
                            f"yuki:{context.version.conversation_id}:"
                            f"g{context.version.generation}:main"
                        )
                    execution = TurnExecution(runner, initial, runtime, None)
                    if kind == "execution_compact":
                        execution.state.transcript = session.transcript
                    execution.state.definitions = await runner.main_contract.model_definitions()
                    execution.state.fixed_definitions = execution.state.definitions
                    prepared = await execution.prepare_request(0, (), WebMode.DISABLED)
                    main = prepared.request
                    assert main.provider_session_id == owner
                    if kind == "public_compact":
                        execution.runtime = replace(execution.runtime, compact_focus="原决定")
                    compacted = await execution.compact_current(
                        0, main, ModelExecutionPriority.FOREGROUND, 128000
                    )
                    if kind == "execution_compact":
                        assert compacted.request_chain_id != main.request_chain_id
                        assert neutral[0].messages[:-1] == main.messages
                        assert not any("原先的调查过程" in m.content for m in compacted.messages)
                    else:
                        assert neutral[0].messages[:-1] == context.messages
                        assert not any("private" in m.content for m in neutral[0].messages)
                        snapshot = await context.repository.read(context.version.conversation_id)
                        assert snapshot.selected_summary_text == responses[0].content
                        assert any(
                            "Conversation compact checkpoint" in m.content
                            for m in compacted.messages
                        )
                    assert neutral[0].tools == main.tools
                    assert neutral[0].response_format is None
                else:
                    await control.reserve_request(auxiliary=True)
                    token = current_work_control.set(control)
                    try:
                        with model_dispatch_guard(control.validate):
                            result = await StructuredTaskRunner(models).run(
                                task=ModelTask.UTILITY_STRUCTURED,
                                instruction="读取所提供的原资料并返回结果。",
                                structured_input={
                                    "work_id": control.current["id"],
                                    "source_event_id": control.source["trigger_event_id"],
                                },
                                output_model=_StructuredResult,
                                mode=StructuredOutputMode.TEXT_JSON,
                            )
                        assert result.value == "original structured result"
                        assert result.source_ref == f"work:{control.current['id']}"
                    finally:
                        current_work_control.reset(token)
                assert (await control.repository.get(control.current["id"]))[
                    "model_requests"
                ] == int(kind != "public_compact")
            else:
                env = await social_env(database, tmp_path)
                version, events = await EventLedgerRepository(database).read_scope_context(
                    ConversationScope.group("80001", "20001"), limit=80
                )
                assert version.conversation_id == env.context.conversation_id
                owner = (
                    f"yuki:{version.conversation_id}:g{version.generation}:main"
                    if kind != "utility"
                    else None
                )
                result = await StructuredTaskRunner(models).run(
                    task=ModelTask.UTILITY_STRUCTURED,
                    instruction="读取所提供的原资料并返回结果。",
                    structured_input={"event_id": events[0].id},
                    output_model=_StructuredResult,
                    mode=StructuredOutputMode.TEXT_JSON,
                    provider_session_id=owner,
                )
                assert result.value == "original structured result"
                assert result.source_ref == f"event:{events[0].id}"
            assert headers == [owner] and len(wire) == 1
            assert wire[0] == provider._build_payload(replace(neutral[0], provider_session_id=None))
            assert "x-session-id" not in json.dumps(wire[0])
            assert responses[0].prompt_tokens == 137
            assert responses[0].completion_tokens == 28
            assert responses[0].cached_prompt_tokens == 53
            assert responses[0].reasoning_tokens == 7
            assert responses[0].total_tokens == 165
        finally:
            if control is not None:
                await control.repository.release(control.lease)
            await models.close()


@pytest.mark.parametrize("kind", ["gemini", "anthropic", "responses"])
async def test_native_search_uses_actual_work_owner_without_changing_body(database, tmp_path, kind):
    control, _session_state, _initial = await _session(database, tmp_path)
    owner = (
        f"yuki:{control.lease.conversation_id}:g{control.lease.generation}:"
        f"work:{control.current['id']}"
    )
    wire, headers, neutral = [], [], []

    def transport(request):
        wire.append(json.loads(request.content))
        headers.append(request.headers.get("x-session-id"))
        if kind == "gemini":
            body = {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"role": "model", "parts": [{"text": "实际检索原文"}]},
                        "groundingMetadata": {
                            "groundingChunks": [
                                {"web": {"uri": "https://example.com/original", "title": "原来源"}}
                            ]
                        },
                    }
                ]
            }
        elif kind == "responses":
            body = {
                "id": "native-search-original",
                "status": "completed",
                "output": [
                    {
                        "type": "web_search_call",
                        "id": "original-search",
                        "status": "completed",
                        "action": {
                            "type": "search",
                            "query": "核对原事实",
                            "sources": [{"url": "https://example.com/original", "title": "原来源"}],
                        },
                    },
                    {
                        "type": "message",
                        "id": "result",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "实际检索原文"}],
                    },
                ],
            }
        else:
            body = {
                "id": "native-search-original",
                "stop_reason": "end_turn",
                "content": [
                    {"type": "server_tool_use", "name": "web_search", "id": "original-search"},
                    {
                        "type": "web_search_tool_result",
                        "tool_use_id": "original-search",
                        "content": [
                            {
                                "type": "web_search_result",
                                "url": "https://example.com/original",
                                "title": "原来源",
                                "content": "实际检索原文",
                            }
                        ],
                    },
                ],
            }
        return httpx.Response(200, json=body)

    class ObservedGemini(GeminiProvider):
        async def complete(self, request):
            neutral.append(request)
            return await super().complete(request)

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/", transport=httpx.MockTransport(transport)
    ) as client:
        provider_type = {
            "gemini": ObservedGemini,
            "anthropic": AnthropicMessagesProvider,
            "responses": OpenAICompatibleResponsesProvider,
        }[kind]
        provider = provider_type(
            base_url="https://wire.invalid/",
            api_key="synthetic",
            timeout_seconds=2,
            max_retries=0,
            client=client,
        )
        profile = _profile().model_copy(
            update={
                "provider": "fake" if kind == "gemini" else kind,
                "protocol": {
                    "gemini": ModelProtocol.GEMINI,
                    "anthropic": ModelProtocol.ANTHROPIC_MESSAGES,
                    "responses": ModelProtocol.RESPONSES,
                }[kind],
            }
        )
        bridge = NativeSearchBridge(
            profile=profile,
            credential="synthetic",
            provider=provider,
            state=BridgeState(tmp_path / "search.sqlite3"),
            extractor=DirectPageExtractor(
                state=BridgeState(tmp_path / "search.sqlite3"), timeout_seconds=2
            ),
        )
        try:
            token = current_work_control.set(control)
            try:
                first = await bridge._search(WebSearchRequest("核对原事实", extract_max_results=0))
            finally:
                current_work_control.reset(token)
            second = await bridge._search(WebSearchRequest("核对原事实", extract_max_results=0))
            assert first.sources == second.sources
            assert headers == [owner, None] and len(wire) == 2
            assert wire[0] == wire[1]
            if kind == "gemini":
                assert wire[0] == provider._build_payload(
                    replace(neutral[0], provider_session_id=None)
                )
            assert "x-session-id" not in json.dumps(wire[0])
            assert (await control.repository.get(control.current["id"]))["model_requests"] == 1
        finally:
            await control.repository.release(control.lease)
            await bridge.close()
