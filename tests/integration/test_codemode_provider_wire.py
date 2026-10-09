"""The shared Pi runner pairs only Provider-requested calls on every wire dialect."""

import json
from dataclasses import asdict, replace

import httpx
import pytest
from tests.support.codemode_cases import requires_worker
from tests.support.codemode_runner_helpers import ACCEPT, Backend, call, runner_env

from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatResponse
from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.llm.openai_responses import OpenAIResponsesProvider
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
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_journal import WorkJournal, decode_transcript

pytestmark = requires_worker

DIALECTS = [
    ("openai_compatible", "chat_completions"),
    ("openai", "responses"),
    ("anthropic", "anthropic_messages"),
    ("gemini", "gemini"),
]
SIGNATURE = "synthetic-private-continuation"


def answer(response, protocol, sequence):
    calls = response.tool_calls
    if protocol == "chat_completions":
        message = {"role": "assistant", "content": response.content}
        if calls:
            message.update(
                tool_calls=[asdict(c) for c in calls],
                reasoning_details=[{"type": "reasoning.encrypted", "data": SIGNATURE}],
            )
        return {
            "choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]
        }
    if protocol == "responses":
        output = (
            [
                {"id": f"reason-{sequence}", "type": "reasoning", "encrypted_content": SIGNATURE},
                *[
                    {
                        "id": f"item-{c.id}",
                        "type": "function_call",
                        "call_id": c.id,
                        "name": c.function.name,
                        "arguments": c.function.arguments,
                        "status": "completed",
                    }
                    for c in calls
                ],
            ]
            if calls
            else [
                {
                    "id": f"message-{sequence}",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": response.content}],
                }
            ]
        )
        return {"id": f"r-{sequence}", "status": "completed", "output": output}
    if protocol == "anthropic_messages":
        content = (
            [
                {"type": "thinking", "thinking": "synthetic reasoning", "signature": SIGNATURE},
                *[
                    {
                        "type": "tool_use",
                        "id": c.id,
                        "name": c.function.name,
                        "input": json.loads(c.function.arguments),
                    }
                    for c in calls
                ],
            ]
            if calls
            else [{"type": "text", "text": response.content}]
        )
        return {"content": content, "stop_reason": "tool_use" if calls else "end_turn"}
    parts = (
        [
            {
                "functionCall": {
                    "id": c.id,
                    "name": c.function.name,
                    "args": json.loads(c.function.arguments),
                },
                "thoughtSignature": SIGNATURE,
            }
            for c in calls
        ]
        if calls
        else [{"text": response.content}]
    )
    return {"candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": parts}}]}


def wire_calls_and_results(payload, protocol):
    """Inspect captured bytes, never the Runner's normalized transcript."""
    if protocol == "chat_completions":
        messages = payload["messages"]
        calls = [c["function"]["name"] for m in messages for c in m.get("tool_calls", [])]
        results = [(m["tool_call_id"], m["content"]) for m in messages if m["role"] == "tool"]
    elif protocol == "responses":
        calls = [i["name"] for i in payload["input"] if i.get("type") == "function_call"]
        results = [
            (i["call_id"], i["output"])
            for i in payload["input"]
            if i.get("type") == "function_call_output"
        ]
    elif protocol == "anthropic_messages":
        blocks = [
            b for m in payload["messages"] if isinstance(m["content"], list) for b in m["content"]
        ]
        calls = [b["name"] for b in blocks if b["type"] == "tool_use"]
        results = [(b["tool_use_id"], b["content"]) for b in blocks if b["type"] == "tool_result"]
    else:
        parts = [p for c in payload["contents"] for p in c["parts"]]
        calls = [p["functionCall"]["name"] for p in parts if "functionCall" in p]
        results = [
            (p["functionResponse"]["id"], p["functionResponse"]["response"]["output"])
            for p in parts
            if "functionResponse" in p
        ]
    return calls, results


def wire_work_receipts(value):
    """Inspect the fresh-chain evidence as serialized on each native wire."""
    if isinstance(value, dict):
        return [receipt for item in value.values() for receipt in wire_work_receipts(item)]
    if isinstance(value, list):
        return [receipt for item in value for receipt in wire_work_receipts(item)]
    if isinstance(value, str):
        try:
            material = json.loads(value)
        except ValueError:
            return []
        if isinstance(material, dict) and material.get("kind") == "work_unobserved_tool_round":
            return material["calls"]
        # The current Host envelope contains the original observation as nested
        # JSON text; inspect the captured wire recursively, not host internals.
        return wire_work_receipts(material)
    return []


@pytest.mark.parametrize("vendor,protocol", DIALECTS)
@pytest.mark.parametrize("boundary", [False, True])
async def test_code_children_never_become_provider_function_calls(
    database, tmp_path, vendor, protocol, boundary
):
    # Exact quantum settles the VM before the next model dispatch. Unlike an
    # in-flight VM, its unseen portable receipt must survive a fresh chat chain.
    prefix = "for i in range(5):\n    await yuki_lookup({'q': i})\n" if boundary else ""
    script = {
        "code": prefix + "a = await yuki_lookup({'q': 1})\n"
        "b = await yuki_lookup({'q': 2})\n"
        "w = await yuki_workspace_write({'path': 'x'})\n"
        "[a['data']['q'] + b['data']['q'], w['status']]"
    }
    responses = iter(
        [
            call("task_control", ACCEPT, "accept"),
            call("execute_code", script, "outer"),
            ChatResponse("done", 0),
        ]
    )
    chat, _, control, runtime, repo = await runner_env(database, tmp_path, iter(()))
    captured = []

    def transport(request):
        captured.append(request.content)
        return httpx.Response(200, json=answer(next(responses), protocol, len(captured)))

    async with httpx.AsyncClient(
        base_url="https://code-wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        kinds = {
            "responses": DeepSeekResponsesProvider
            if vendor == "deepseek"
            else OpenAIResponsesProvider,
            "anthropic_messages": AnthropicMessagesProvider,
            "gemini": GeminiProvider,
        }
        kind = kinds.get(protocol, OpenAICompatibleProvider)
        adapter = kind(
            base_url="https://code-wire.invalid/v1/",
            api_key="synthetic",
            timeout_seconds=2,
            max_retries=0,
            client=client,
            **({"provider_name": vendor} if protocol == "chat_completions" else {}),
        )
        profile = ModelProfile(
            id="code-wire",
            provider=vendor,
            protocol=ModelProtocol(protocol),
            base_url="https://code-wire.invalid/v1/",
            api_key_env="UNUSED",
            model="synthetic",
            timeout_seconds=2,
            max_retries=0,
            default_temperature=0.5,
            default_max_output_tokens=8192,
            capabilities={ModelCapability.TOOLS, ModelCapability.REASONING},
        )
        models = TaskModelExecutor(
            router=ModelRouter(
                ModelProfileCatalog(
                    profiles={profile.id: profile},
                    routes={
                        task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask
                    },
                )
            ),
            pool=ModelClientPool(injected_profiles={profile.id: adapter}),
        )
        chat.runtime.runner._models = models
        # The dialect stays explicitly configured, independent of task contents.
        runtime = replace(runtime, canonical_conversation_id=control.lease.conversation_id)
        backend = Backend()
        result = await chat.runtime.runner.run(
            (ChatMessage("user", "creation-time chat"),),
            # Exhaust model admission as well, so boundary cannot use the one
            # closing request and must project its result on the next chain.
            replace(runtime, max_model_requests=2) if boundary else runtime,
            backend,
        )
        if boundary:
            assert result.work_state == "queued"
            fresh = WorkControl(
                repo, control.lease, control.source_key, dict(control.source), control.validate
            )
            fresh.bind_context_access(control.context_access)
            fresh.current = await repo.get(control.current["id"])
            control = fresh
            runtime = replace(runtime, work_control=fresh)
            await chat.runtime.runner.run(
                (ChatMessage("user", "current approved chat"),), runtime, backend
            )
        assert len(captured) == 3
        payload = json.loads(captured[-1])
        names, results = wire_calls_and_results(payload, protocol)
        assert names == ([] if boundary else ["task_control", "execute_code"])
        assert [identity for identity, _ in results] == ([] if boundary else ["accept", "outer"])
        if boundary:
            assert b"creation-time chat" not in captured[-1]
            assert b"current approved chat" in captured[-1]
            assert SIGNATURE.encode() not in captured[-1]
            receipts = wire_work_receipts(payload)
            assert len(receipts) == 1 and receipts[0]["call_id"] == "outer"
            assert receipts[0]["tool"] == "execute_code"
            body = json.loads(receipts[0]["result"])
        else:
            body = json.loads(results[-1][1])
        assert body["result"] == [3, "succeeded"]
        expected_tools = ["lookup"] * (7 if boundary else 2) + ["workspace_write"]
        assert [i["tool"] for i in body["operations"]] == expected_tools
        assert [name for name, _ in backend.log] == expected_tools
        assert (await repo.get(control.current["id"]))["tool_calls"] == len(expected_tools)
        # A fresh reader loads the original private protocol checkpoint. Root
        # business reactivation intentionally creates a new chain after a paired
        # composition; it is not the byte-for-byte replay boundary tested here.
        owner = control.session
        await owner.save("paired")
        loaded = await WorkJournal(repo).load(
            control.lease, control.current["id"], owner.contract, source_control=control
        )
        assert loaded.record is not None
        restored = decode_transcript(json.loads(loaded.record["payload_json"])["transcript"])
        sequence = restored.request()
        responses = iter([ChatResponse("done", 0)])
        original = owner.transcript.request()
        common = dict(
            model="synthetic", max_output_tokens=8192, tools=runtime.fixed_tools, tool_choice="auto"
        )
        await adapter.complete(
            ChatRequest(
                messages=original.messages,
                continuation=original.continuation,
                continuation_items=original.items,
                **common,
            )
        )
        responses = iter([ChatResponse("done", 0)])
        await adapter.complete(
            ChatRequest(
                messages=sequence.messages,
                continuation=sequence.continuation,
                continuation_items=sequence.items,
                **common,
            )
        )
        assert captured[-2] == captured[-1]
        if not boundary and (vendor not in {"openai", "azure_openai"} or protocol == "responses"):
            assert SIGNATURE.encode() in captured[2]
        assert SIGNATURE not in json.dumps(body)
        assert len(backend.log) == (8 if boundary else 3)  # Recovery never repeats effects.
