"""execute_code through the real AgentRunner loop, Work journal and restore."""

import json
from types import SimpleNamespace

from tests.conftest import build_harness, make_settings

# P10: explicit Invocation fixture contract; existing assertions are retained.
from tests.support.agent_backend import StubAgentBackend
from tests.support.codemode_cases import BINARY, TOOLS, worker
from tests.support.social_identity_cases import social_env

from yuki.capabilities.validation import JsonSchemaCapabilityValidator
from yuki.codemode.api_projection import project
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.llm.fake import FakeLLMProvider
from yuki.runtime.origin import TurnOrigin
from yuki.runtime.work_control import WorkControl, work_control_tools
from yuki.runtime.work_repository import WorkRepository
from yuki.services.agent_runner import AgentRuntime
from yuki.tool_results.access import ArtifactAccess

FIXED_TOOLS = tuple(
    sorted(
        {tool.name: tool for tool in (*work_control_tools(), *TOOLS)}.values(),
        key=lambda tool: tool.name,
    )
)


def _schema_view():
    validator = JsonSchemaCapabilityValidator()
    validator.admit_tools(FIXED_TOOLS)
    return validator.freeze()


def call(name, args, identity):
    return ChatResponse(
        "", 0, tool_calls=(ToolCall(identity, ToolFunction(name, json.dumps(args))),)
    )


class Backend(StubAgentBackend):
    """Minimal explicit-invocation backend with an independent downstream log."""

    def __init__(self):
        self.log = []

    def parallel_safe(self, name, runtime):
        return name in {"lookup", "search_chat_history"}

    def is_side_effecting(self, name, arguments, runtime):
        return name not in {"lookup", "search_chat_history"}

    async def execute_call(self, invocation):
        from yuki.capabilities.results import ToolExecutionResult
        from yuki.runtime.effect_outcomes import current_result_capture

        self.log.append((invocation.call.function.name, invocation.identity.operation_id))
        # A real binding publishes its typed outcome and returns the model projection.
        outcome = ToolExecutionResult(
            ok=True,
            data=json.loads(invocation.call.function.arguments),
            provider_id="fixture",
            tool_name=invocation.call.function.name,
        )
        capture = current_result_capture.get()
        if capture is not None:
            capture.outcome = outcome
        return json.dumps(outcome.model_payload())

    def finalize(self, text, runtime):
        return text

    def post_commit_recovery_text(self):
        return None


async def runner_env(database, tmp_path, responses):
    provider = FakeLLMProvider(lambda _: next(responses))
    settings = make_settings(
        database.url,
        code_mode_enabled=True,
        code_mode_worker_path=BINARY,
        code_mode_worker_sha256=__import__("hashlib").sha256(BINARY.read_bytes()).hexdigest(),
        code_mode_launcher_path=worker().launcher_path,
        code_mode_launcher_sha256=worker().launcher_sha256,
    )
    chat = build_harness(database, settings, provider).processor._chat
    chat.runtime.runner.code_mode_settings = settings
    # The frozen projection the deployed contract would carry.
    chat.runtime.runner.main_contract = SimpleNamespace(
        revision="manifest-test",
        script_api=project(TOOLS, "manifest-test"),
        # Control calls are validated against the same frozen schemas.
        schema_view=_schema_view(),
        invariant_messages=chat._prompt_composer.invariant_messages,
    )
    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1, seconds=300)

    async def validate():
        assert await repo.valid(lease)

    # The real entrypoint binds canonical read identity before accepting Work.
    # Anonymous controls can save a note but cannot read it on business resume.
    control = WorkControl(
        repo, lease, "code-runner", {"origin": TurnOrigin.USER_MESSAGE.value}, validate
    )
    control.bind_context_access(
        ArtifactAccess(lease.conversation_id, 1, env.person, read_scope="main")
    )
    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="10001",
        actor_is_superuser=False,
        conversation_key="code-runner",
        current_group_id=None,
        bot_user_id="80001",
        gateway=None,
        runtime_config=await chat._runtime_config.snapshot(),
        allowed_capabilities=frozenset(),
        work_control=control,
        fixed_tools=FIXED_TOOLS,
    )
    return chat, provider, control, runtime, repo


ACCEPT = {"action": "start", "goal": "compose"}
