"""Known auxiliary owners travel as HTTP metadata through actual serializers."""

import json
from dataclasses import replace

import httpx
import pytest
from pydantic import BaseModel
from tests.integration.test_current_material_main_contract import _catalog, _profile
from tests.support.social_identity_cases import social_env
from tests.support.work_compaction import summary_json
from tests.support.work_compaction_capacity_helpers import _runtime, _session
from tests.unit.rollup_test_helpers import _output

from qq_ai_bot.conversation.rollup.models import RollupCandidate, RollupPolicyConfig
from qq_ai_bot.conversation.rollup.service import ConversationRollupService
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.model_runtime.dispatch_guard import model_dispatch_guard
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import ModelExecutionPriority, ModelTask, StructuredOutputMode
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.model_runtime.structured import StructuredTaskRunner
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.services.ordinary_compaction import summarize_records
from qq_ai_bot.services.turn_execution import TurnExecution
from qq_ai_bot.web.bridge_state import BridgeState
from qq_ai_bot.web.deepseek_bridge import DeepSeekSearchBridge
from qq_ai_bot.web.gemini_bridge import GeminiSearchBridge
from qq_ai_bot.web.models import WebMode, WebSearchRequest


class _StructuredResult(BaseModel):
    value: str


@pytest.mark.parametrize(
    "kind", ["work", "ordinary", "rollup", "structured", "structured_work", "utility"]
)
async def test_auxiliary_session_header_keeps_original_body(database, tmp_path, kind):
    wire, headers, neutral = [], [], []

    def transport(request):
        wire.append(json.loads(request.content))
        headers.append(request.headers.get("x-session-id"))
        source = wire[-1]["contents"][-1]["parts"][-1]["text"]
        if kind == "work":
            content = summary_json(source)
        elif kind == "ordinary":
            content = json.dumps(
                {"facts": [{"text": "原资料的摘要", "refs": json.loads(source)["source_refs"]}]}
            )
        elif kind == "rollup":
            ids = json.loads(
                source.split("Available internal source_event_ids: ", 1)[1].split("\n", 1)[0]
            )
            content = _output(ids, "保留原对话来源")
        else:
            content = '{"value":"original structured result"}'
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"role": "model", "parts": [{"text": content}]},
                    }
                ]
            },
        )

    class ObservedGemini(GeminiProvider):
        async def complete(self, request):
            neutral.append(request)
            return await super().complete(request)

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
            if kind in {"work", "structured_work"}:
                control, session, initial = await _session(database, tmp_path)
                owner = (
                    f"yuki:{control.lease.conversation_id}:g{control.lease.generation}:"
                    f"work:{control.current['id']}"
                )
                if kind == "work":
                    session.transcript.append(ChatMessage("assistant", "原先的调查过程" * 1000))
                    runner, runtime = await _runtime(database, control, initial, provider)
                    runner._models = models
                    prepared = await TurnExecution(
                        runner, session.transcript.request().messages, runtime, None
                    ).prepare_request(0, (), WebMode.DISABLED)
                    main = prepared.request
                    assert main.provider_session_id == owner
                    compacted = await runner._compact_work(
                        runtime, ModelExecutionPriority.FOREGROUND, 128000, main
                    )
                    assert compacted.chain_id != session.compaction_anchor.chain_id
                else:
                    await control.reserve_request(auxiliary=True)
                    token = current_work_control.set(control)
                    try:
                        with model_dispatch_guard(control.validate):
                            result = await StructuredTaskRunner(models).run(
                                task=ModelTask.UTILITY_STRUCTURED,
                                instruction="读取所提供的原资料并返回结果。",
                                structured_input={"work_id": control.current["id"]},
                                output_model=_StructuredResult,
                                mode=StructuredOutputMode.TEXT_JSON,
                            )
                        assert result.value == "original structured result"
                    finally:
                        current_work_control.reset(token)
                assert (await control.repository.get(control.current["id"]))["model_requests"] == 1
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
                if kind == "ordinary":
                    main = ChatRequest(messages=(), provider_session_id=owner)
                    summary = await summarize_records(
                        [(f"event:{events[0].id}", events[0].content)],
                        main_request=main,
                        structured_mode=StructuredOutputMode.TEXT_JSON,
                        summary_budget=32768,
                        output_tokens=4096,
                        prepare=lambda request: request,
                        execute=lambda request: models.execute(ModelTask.CHAT_AGENT, request),
                    )
                    assert summary["facts"][0]["refs"] == [f"event:{events[0].id}"]
                elif kind == "rollup":
                    candidate = RollupCandidate(
                        conversation_id=version.conversation_id,
                        generation=version.generation,
                        source_coverage=0,
                        source_rollup_revision=0,
                        previous_summary="",
                        events=events,
                        fingerprint="actual-source-fixture",
                    )
                    service = ConversationRollupService(
                        models=models, config=RollupPolicyConfig(), timeout_seconds=2
                    )
                    text, _kind = await service.summarize_candidate(candidate)
                    assert "保留原对话来源" in text
                else:
                    result = await StructuredTaskRunner(models).run(
                        task=ModelTask.UTILITY_STRUCTURED,
                        instruction="读取所提供的原资料并返回结果。",
                        structured_input={"event_id": events[0].id},
                        output_model=_StructuredResult,
                        mode=StructuredOutputMode.TEXT_JSON,
                        provider_session_id=owner,
                    )
                    assert result.value == "original structured result"
            assert headers == [owner] and len(wire) == 1
            assert wire[0] == provider._build_payload(replace(neutral[0], provider_session_id=None))
            assert "x-session-id" not in json.dumps(wire[0])
        finally:
            if control is not None:
                await control.repository.release(control.lease)
            await models.close()


@pytest.mark.parametrize("kind", ["gemini", "deepseek"])
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
        if kind == "gemini":
            provider = ObservedGemini(
                base_url="https://wire.invalid/",
                api_key="synthetic",
                timeout_seconds=2,
                max_retries=0,
                client=client,
            )
            bridge = GeminiSearchBridge(
                profile=_profile(),
                credential="synthetic",
                provider=provider,
                state=BridgeState(tmp_path / "search.sqlite3"),
            )
        else:
            bridge = DeepSeekSearchBridge(
                api_key="synthetic", state_path=tmp_path / "search.sqlite3", client=client
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
