"""Same fake model sequences through AgentRunner, compared to a pre-port golden.

`tests/fixtures/agent_core/runner_golden.json` was captured from the Runner's
own loop at b4fdef7d, before iteration moved into `agent_core.loop`. Set
`YUKI_REGEN_AGENT_GOLDEN=1` only when a contract change is intended.
"""

import json
import os
import re
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.conftest import build_harness, make_settings

# P10: explicit Invocation fixture contract; existing assertions are retained.
from tests.support.agent_backend import StubAgentBackend
from tests.support.social_identity_cases import social_env

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatResponse,
    ChatTool,
    ModelResponseStatus,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.runtime.work_control import WorkControl, work_control_tools
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, work
from qq_ai_bot.services.agent_runner import AgentRuntime

GOLDEN = Path(__file__).parents[1] / "fixtures" / "agent_core" / "runner_golden.json"
_UUID = re.compile(r"[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def call(identity, name="read", arguments="{}"):
    return ToolCall(identity, ToolFunction(name, arguments))


def tool_response(*calls, status=ModelResponseStatus.COMPLETED):
    return ChatResponse("", 0, tool_calls=calls, status=status)


SCENARIOS = {
    "plain_answer": [ChatResponse("你好", 0)],
    "read_send_then_answer": [
        tool_response(call("r1"), call("r2", arguments='{"q":2}'), call("s1", "send_message")),
        ChatResponse("完成", 0),
    ],
    "incomplete_never_executes": [
        tool_response(
            call("t1", "send_message", '{"text":'), status=ModelResponseStatus.INCOMPLETE
        ),
        tool_response(call("t2", "send_message", '{"text":"ok"}')),
        ChatResponse("已发送", 0),
    ],
    "empty_retry": [ChatResponse(" ", 0), ChatResponse("重试后答复", 0)],
    "repeated_batch_no_progress": [
        tool_response(call("a", arguments='{"q":1}')),
        tool_response(call("b", arguments='{"q":1}')),
        tool_response(call("c", arguments='{"q":1}')),
        ChatResponse("停止", 0),
    ],
    "exhaustion": [tool_response(call(f"x{i}", arguments=json.dumps({"q": i}))) for i in range(3)],
    "duplicate_ids_rejected": [
        tool_response(call("dup"), call("dup", arguments='{"q":9}')),
        ChatResponse("重复已拒绝", 0),
    ],
}


def normalize(value):
    return _UUID.sub("<id>", json.dumps(value, ensure_ascii=False, sort_keys=True))


def request_view(request):
    return {
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "tool_calls": [(c.id, c.function.name, c.function.arguments) for c in m.tool_calls],
                "tool_call_id": m.tool_call_id,
            }
            for m in request.messages
        ],
        "tools": [t.name for t in request.tools],
        "tool_choice": request.tool_choice,
    }


class Backend(StubAgentBackend):
    def __init__(self):
        self.executed = []

    def definitions(self, runtime, **kwargs):
        return (
            ChatTool("read", "read", {"type": "object"}),
            ChatTool("send_message", "send", {"type": "object"}),
        )

    def parallel_safe(self, name, runtime):
        return name == "read"

    def is_side_effecting(self, name, arguments, runtime):
        return name != "read"

    async def execute_call(self, invocation):
        name = invocation.call.function.name
        arguments = invocation.call.function.arguments
        self.executed.append((name, arguments))
        return json.dumps({"ok": True, "data": {"tool": name, "arguments": arguments}})

    def finalize(self, text, runtime):
        return text

    def exhausted(self, runtime):
        return "exhausted"


def runtime_for(chat, snapshot, **overrides):
    values = dict(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="differential",
        current_group_id=None,
        bot_user_id="9999",
        gateway=None,
        runtime_config=snapshot,
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=8,
        max_model_requests=3,
    )
    values.update(overrides)
    return AgentRuntime(**values)


async def run_scenario(database, name):
    responses = iter(SCENARIOS[name])
    provider = FakeLLMProvider(lambda _request: next(responses))
    harness = build_harness(database, make_settings(database.url), provider)
    chat = harness.processor._chat
    backend = Backend()
    runtime = runtime_for(chat, await chat._runtime_config.snapshot())
    try:
        result = await chat.runtime.runner.run(
            (ChatMessage("system", "fixed"), ChatMessage("user", "开始")), runtime, backend
        )
        outcome = {
            "text": result.text,
            "tool_calls_used": result.tool_calls_used,
            "model_requests": result.model_requests,
            "suppress_delivery": result.suppress_delivery,
            "work_state": result.work_state,
        }
    except Exception as exc:
        outcome = {"raised": type(exc).__name__}
    return {
        "requests": [request_view(r) for r in provider.requests],
        "executed": backend.executed,
        "result": outcome,
    }


async def run_work_scenario(database, tmp_path):
    def control_call(identity, args, name="task_control"):
        return tool_response(call(identity, name, json.dumps(args)))

    responses = iter(
        [
            control_call(
                "accept", {"action": "accept", "goal": "render", "output_kind": "state_change"}
            ),
            control_call("render", {}, "render_fixture"),
            control_call("complete", {"action": "complete"}),
            ChatResponse("图片已经生成", 0),
        ]
    )
    provider = FakeLLMProvider(lambda _request: next(responses))
    harness = build_harness(database, make_settings(database.url), provider)
    chat = harness.processor._chat
    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)

    async def validate():
        assert await repo.valid(lease)

    control = WorkControl(repo, lease, "differential", {}, validate)
    executed = []

    class WorkBackend(Backend):
        def definitions(self, runtime, **kwargs):
            return tuple(
                sorted(
                    (*work_control_tools(), ChatTool("render_fixture", "r", {"type": "object"})),
                    key=lambda tool: tool.name,
                )
            )

        def parallel_safe(self, name, runtime):
            return False

        def is_side_effecting(self, name, arguments, runtime):
            return True

        async def execute_call(self, invocation):
            name = invocation.call.function.name
            executed.append(name)
            return json.dumps({"ok": True, "data": {"artifact_id": "png", "exit_code": 0}})

    runtime = runtime_for(
        chat,
        await chat._runtime_config.snapshot(),
        conversation_key="differential-work",
        actor_user_id="10001",
        bot_user_id="80001",
        max_model_requests=8,
        work_control=control,
    )
    result = await chat.runtime.runner.run((ChatMessage("user", "画图"),), runtime, WorkBackend())
    async with database.sessions() as reader:
        rows = (
            (
                await reader.execute(
                    select(effects.c.effect_key, effects.c.kind, effects.c.state).order_by(
                        effects.c.created
                    )
                )
            )
            .mappings()
            .all()
        )
        budget = (
            (await reader.execute(select(work.c.state, work.c.model_requests, work.c.tool_calls)))
            .mappings()
            .one()
        )
    return {
        "requests": [request_view(r) for r in provider.requests],
        "executed": executed,
        "effects": [dict(row) for row in rows],
        "work": dict(budget),
        "result": {
            "text": result.text,
            "tool_calls_used": result.tool_calls_used,
            "model_requests": result.model_requests,
            "work_state": result.work_state,
            "ending": control.ending,
        },
    }


async def observed(database, tmp_path, name):
    if name == "work_accept_render_complete":
        return await run_work_scenario(database, tmp_path)
    return await run_scenario(database, name)


NAMES = [*SCENARIOS, "work_accept_render_complete"]


@pytest.mark.parametrize("name", NAMES)
async def test_runner_matches_pre_port_golden(database, tmp_path, name):
    actual = json.loads(normalize(await observed(database, tmp_path, name)))
    golden = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
    if os.environ.get("YUKI_REGEN_AGENT_GOLDEN"):
        golden[name] = actual
        GOLDEN.write_text(json.dumps(golden, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    assert actual == golden[name]
