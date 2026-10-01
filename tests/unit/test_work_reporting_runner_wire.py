"""Actual serializers retain the original prefix through reply and exit feedback."""

import json
from itertools import pairwise

import httpx
import pytest
from tests.support.runtime_wire import install_wire
from tests.unit.test_work_reporting_runner import START, case, new_event, response, run, tool

from qq_ai_bot.domain.messages import ChatResponse
from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
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


def claude_wire(test_case):
    captured = []

    def transport(request):
        captured.append(json.loads(request.content))
        reply = test_case.provider._responder(test_case.provider.requests[-1])
        content = [{"type": "text", "text": reply.content}] if reply.content else []
        content.extend(
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.function.name,
                "input": json.loads(call.function.arguments),
            }
            for call in reply.tool_calls
        )
        return httpx.Response(
            200,
            json={
                "id": f"msg-{len(captured)}",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": content,
                "stop_reason": "tool_use" if reply.tool_calls else "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        )

    client = httpx.AsyncClient(
        base_url="https://runtime.example", transport=httpx.MockTransport(transport)
    )
    provider = AnthropicMessagesProvider(
        base_url="https://runtime.example",
        api_key="test",
        timeout_seconds=2,
        max_retries=0,
        client=client,
    )

    async def complete(request):
        test_case.provider.requests.append(request)
        return await provider.complete(request)

    test_case.provider.complete = complete
    profile = ModelProfile(
        id="report-claude",
        provider="anthropic",
        protocol=ModelProtocol.ANTHROPIC_MESSAGES,
        base_url="https://runtime.example",
        api_key_env="UNUSED",
        model="claude-test",
        timeout_seconds=2,
        max_retries=0,
        default_temperature=0.5,
        default_max_output_tokens=1024,
        capabilities=frozenset(ModelCapability) - {ModelCapability.NATIVE_WEB_SEARCH},
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocol", ["chat_completions", "responses", "native_responses", "anthropic_messages"]
)
async def test_reply_continues_same_work_and_exit_feedback_keeps_wire_prefix(
    database, tmp_path, protocol
):
    replies = []
    test_case = await case(database, tmp_path, replies)
    event_id = await new_event(test_case, "现在进展如何？")
    identity = await test_case.repository.enqueue(
        test_case.control.lease.conversation_id,
        1,
        "reply-wire",
        kind="message",
        event_id=event_id,
        work_id=test_case.control.current["id"],
        ready=False,
    )
    await test_case.repository.prepare_input(identity, {"text": "现在进展如何？"})
    work_id = test_case.control.current["id"]
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
    if protocol == "anthropic_messages":
        client, captured = claude_wire(test_case)
    else:
        client, captured = install_wire(
            test_case.chat,
            test_case.provider,
            "responses" if protocol == "native_responses" else protocol,
            native=protocol == "native_responses",
        )
    original_complete = test_case.provider.complete

    async def complete(request):
        reply = await original_complete(request)
        if len(test_case.provider.requests) == 2:
            assert not await test_case.control.pending()  # consumed is not a delivery receipt
            assert await test_case.control.communication_reports(event_ids=(event_id,)) == []
        return reply

    test_case.provider.complete = complete
    try:
        result = await run(test_case)
        assert result.work_id == work_id and result.work_state == "completed"
        assert test_case.control.requests_started == 5 and test_case.control.tools_started == 3
        assert test_case.observed == ["send_message", "send_message", "write_fixture"]
        reports = await test_case.control.communication_reports(
            kind="reply", event_ids=(event_id,), delivered_only=True
        )
        assert len(reports) == 1
        assert test_case.control.communication["final_feedback_given"] is True
        field = "input" if protocol in {"responses", "native_responses"} else "messages"
        for earlier, later in pairwise(captured):
            assert later["tools"] == earlier["tools"]
            assert later.get("system") == earlier.get("system")
            if protocol == "anthropic_messages":

                def without_breakpoint(messages):
                    return [
                        {
                            **message,
                            "content": [
                                {
                                    key: value
                                    for key, value in block.items()
                                    if key != "cache_control"
                                }
                                for block in message["content"]
                            ],
                        }
                        for message in messages
                    ]

                # Claude deliberately moves its one conversation cache breakpoint.
                previous = without_breakpoint(earlier[field])
                following = without_breakpoint(later[field])
                assert following[: len(previous)] == previous
                assert (
                    sum(
                        "cache_control" in block
                        for message in later[field]
                        for block in message["content"]
                    )
                    == 1
                )
            else:
                assert later[field][: len(earlier[field])] == earlier[field]
        assert "不能据此结束交互式 Work" in json.dumps(captured[-1], ensure_ascii=False)
    finally:
        await client.aclose()
