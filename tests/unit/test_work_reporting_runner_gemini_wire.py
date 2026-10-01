"""Gemini's signed continuation preserves interactive Work feedback and receipts."""

import json
from itertools import pairwise

import httpx
import pytest
from tests.unit.test_work_reporting_runner import START, case, new_event, response, tool

from qq_ai_bot.domain.messages import ChatMessage, ChatResponse
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelTask,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.model_runtime.routes import ModelRouter


def gemini_wire(test_case):
    captured = []

    def transport(request):
        captured.append(json.loads(request.content))
        reply = test_case.provider._responder(test_case.provider.requests[-1])
        parts = [{"text": reply.content}] if reply.content else []
        parts.extend(
            {
                "functionCall": {
                    "id": call.id,
                    "name": call.function.name,
                    "args": json.loads(call.function.arguments),
                },
                "thoughtSignature": f"signature-{call.id}",
            }
            for call in reply.tool_calls
        )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"role": "model", "parts": parts},
                    }
                ]
            },
        )

    client = httpx.AsyncClient(
        base_url="https://gemini.invalid", transport=httpx.MockTransport(transport)
    )
    provider = GeminiProvider(
        base_url="https://gemini.invalid",
        api_key="synthetic",
        timeout_seconds=2,
        max_retries=0,
        client=client,
    )

    async def complete(request):
        test_case.provider.requests.append(request)
        return await provider.complete(request)

    test_case.provider.complete = complete
    profile = ModelProfile(
        id="report-gemini",
        provider="gemini",
        protocol=ModelProtocol.GEMINI,
        base_url="https://gemini.invalid",
        api_key_env="UNUSED",
        model="gemini-3.8-flash",
        timeout_seconds=2,
        max_retries=0,
        default_temperature=0.5,
        default_max_output_tokens=1024,
        capabilities=frozenset({ModelCapability.TOOLS, ModelCapability.REASONING}),
    )
    test_case.runner._models = TaskModelExecutor(
        router=ModelRouter(
            ModelProfileCatalog(
                profiles={profile.id: profile},
                routes={task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask},
            )
        ),
        pool=ModelClientPool(injected_profiles={profile.id: test_case.provider}),
    )
    return client, captured


def content_parts(payload):
    # Gemini combines consecutive user messages, extending the last content item.
    return [(content["role"], part) for content in payload["contents"] for part in content["parts"]]


@pytest.mark.asyncio
async def test_gemini_reply_continues_original_work_and_explicit_exit_keeps_signed_prefix(
    database, tmp_path
):
    test_case = await case(database, tmp_path, [])
    event_id = await new_event(test_case, "现在进展如何？")
    work_id = test_case.control.current["id"]
    input_id = await test_case.repository.enqueue(
        test_case.control.lease.conversation_id,
        1,
        "gemini-reply-wire",
        kind="message",
        event_id=event_id,
        work_id=work_id,
        ready=False,
    )
    await test_case.repository.prepare_input(input_id, {"text": "现在进展如何？"})
    scripted = iter(
        [
            response(tool("send_message", START, "start")),
            response(
                tool(
                    "send_message",
                    {
                        "text": "已找到线索，接下来继续修复。",
                        "work_report": {"kind": "reply", "reply_to_event_ids": [event_id]},
                    },
                    "reply",
                )
            ),
            response(tool("write_fixture", identity="write")),
            ChatResponse("阶段说明后的内部正文", 0),
            response(tool("task_control", {"action": "complete"}, "complete")),
        ]
    )
    test_case.provider._responder = lambda _: next(scripted)
    client, captured = gemini_wire(test_case)
    original_complete = test_case.provider.complete

    async def complete(request):
        reply = await original_complete(request)
        if len(test_case.provider.requests) == 2:
            assert not await test_case.control.pending()
            assert await test_case.control.communication_reports(event_ids=(event_id,)) == []
        return reply

    test_case.provider.complete = complete
    try:
        result = await test_case.runner.run(
            (ChatMessage("system", "固定主 Agent 合同"), ChatMessage("user", "请修复并报告")),
            test_case.runtime,
            test_case.backend,
        )
        assert result.work_id == work_id and result.work_state == "completed"
        assert test_case.control.current["id"] == work_id
        assert test_case.control.requests_started == len(captured) == 5
        assert test_case.control.tools_started == 3
        assert test_case.observed == ["send_message", "send_message", "write_fixture"]
        reports = await test_case.control.communication_reports(
            kind="reply", event_ids=(event_id,), delivered_only=True
        )
        assert len(reports) == 1
        assert test_case.control.communication["final_feedback_given"] is True
        assert captured[0]["systemInstruction"] == {"parts": [{"text": "固定主 Agent 合同"}]}
        declarations = captured[0]["tools"][0]["functionDeclarations"]
        assert declarations == [
            {
                "name": definition.name,
                "description": definition.description,
                "parametersJsonSchema": definition.parameters,
            }
            for definition in test_case.provider.requests[0].tools
        ]
        assert {definition["name"] for definition in declarations} >= {
            "send_message",
            "task_control",
            "subagent_start",
            "read_fixture",
            "write_fixture",
        }
        for earlier, later in pairwise(captured):
            for field in ("systemInstruction", "tools", "toolConfig", "generationConfig"):
                assert later[field] == earlier[field]
            previous = content_parts(earlier)
            assert content_parts(later)[: len(previous)] == previous
        parts = content_parts(captured[-1])
        signed_calls = [part for _, part in parts if "functionCall" in part]
        assert [part["functionCall"]["id"] for part in signed_calls] == ["start", "reply", "write"]
        assert [part["thoughtSignature"] for part in signed_calls] == [
            "signature-start",
            "signature-reply",
            "signature-write",
        ]
        receipts = [part["functionResponse"] for _, part in parts if "functionResponse" in part]
        assert [receipt["id"] for receipt in receipts] == ["start", "reply", "write"]
        serialized = json.dumps(captured[-1], ensure_ascii=False)
        assert "阶段说明后的内部正文" in serialized
        assert "不能据此结束交互式 Work" in serialized
    finally:
        await client.aclose()
