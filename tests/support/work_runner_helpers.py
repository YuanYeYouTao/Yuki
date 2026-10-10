"""Runner fixtures with original journal and effect receipts."""

import json
from types import SimpleNamespace

from tests.conftest import build_harness, make_settings

# P10: explicit Invocation fixture contract; existing assertions are retained.
from tests.support.agent_backend import StubAgentBackend
from tests.support.social_identity_cases import social_env

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.messages import ChatMessage, ChatTool, ToolCall, ToolFunction
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.runtime.subagent_tools import subagent_tools
from qq_ai_bot.runtime.work_control import WorkControl, work_control_tools
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.agent_runner import AgentRuntime


def tool(name, args=None, identity=None):
    return ToolCall(identity or name, ToolFunction(name, json.dumps(args or {})))


START = {"text": "先查原因，再修复。"}


async def case(database, tmp_path, responses, *, reporting="interactive", send_status="succeeded"):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease

    async def validate():
        assert await repository.valid(lease)

    source = {"actor_person_id": env.person, "principal_kind": "person", "origin": "user_message"}
    control = WorkControl(repository, lease, "report-runner", source, validate)
    control.current = await repository.accept(
        lease,
        source_key="report-runner",
        source=source,
        goal="调查并修复",
        reporting=reporting,
    )
    scripted = iter(responses)
    provider = FakeLLMProvider(lambda _: next(scripted))
    harness = build_harness(database, make_settings(database.url), provider)
    chat = harness.processor._chat
    observed = []

    class Backend(StubAgentBackend):
        def definitions(self, runtime, **kwargs):
            return (
                *work_control_tools(),
                *subagent_tools(),
                ChatTool("send_message", "send", {"type": "object"}),
                ChatTool("read_fixture", "read", {"type": "object"}),
                ChatTool("write_fixture", "write", {"type": "object"}),
            )

        def parallel_safe(self, name, runtime):
            return name == "read_fixture"

        def is_side_effecting(self, name, arguments, runtime):
            return name != "read_fixture"

        async def execute_call(self, invocation):
            name = invocation.call.function.name
            arguments = invocation.call.function.arguments
            observed.append(name)
            if name == "send_message":
                target = json.loads(arguments).get("target", {"kind": "space", "id": env.space})
                return json.dumps(
                    {
                        "ok": send_status == "succeeded",
                        "data": {
                            "status": send_status,
                            "target": target,
                        },
                    }
                )
            return json.dumps({"ok": True, "data": {"exit_code": 0}})

        def finalize(self, text, runtime):
            return text

        def exhausted(self, runtime):
            return "exhausted"

    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="10001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="report-runner",
        current_group_id=None,
        bot_user_id="80001",
        gateway=None,
        runtime_config=await chat._runtime_config.snapshot(),
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=12,
        max_model_requests=12,
        work_control=control,
    )
    return SimpleNamespace(
        env=env,
        repository=repository,
        control=control,
        provider=provider,
        runner=chat.runtime.runner,
        runtime=runtime,
        backend=Backend(),
        observed=observed,
        chat=chat,
    )


async def run(test_case):
    return await test_case.runner.run(
        (ChatMessage("user", "请修复并报告"),), test_case.runtime, test_case.backend
    )
