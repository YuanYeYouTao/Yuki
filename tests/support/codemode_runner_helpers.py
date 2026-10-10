"""execute_code through the real AgentRunner loop, Work journal and restore."""

import json
from types import SimpleNamespace

from tests.conftest import build_harness, make_settings

# P10: explicit Invocation fixture contract; existing assertions are retained.
from tests.support.agent_backend import StubAgentBackend
from tests.support.codemode_cases import BINARY, TOOLS, requires_worker, worker
from tests.support.social_identity_cases import social_env

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.codemode.api_projection import project
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.runtime.work_control import WorkControl, work_control_tools
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.tool_results.access import ArtifactAccess

pytestmark = requires_worker


def call(name, args, identity):
    return ChatResponse(
        "", 0, tool_calls=(ToolCall(identity, ToolFunction(name, json.dumps(args))),)
    )


class Backend(StubAgentBackend):
    """Minimal explicit-invocation backend with an independent downstream log."""

    def __init__(self):
        self.log = []

    def definitions(self, runtime, **kwargs):
        merged = {tool.name: tool for tool in (*work_control_tools(), *TOOLS)}
        return tuple(sorted(merged.values(), key=lambda tool: tool.name))

    def parallel_safe(self, name, runtime):
        return name in {"lookup", "search_chat_history"}

    def is_side_effecting(self, name, arguments, runtime):
        return name not in {"lookup", "search_chat_history"}

    async def execute_call(self, invocation):
        self.log.append((invocation.call.function.name, invocation.identity.operation_id))
        return json.dumps({"ok": True, "data": json.loads(invocation.call.function.arguments)})

    def finalize(self, text, runtime):
        return text

    def exhausted(self, runtime):
        return "exhausted"

    def post_commit_recovery_text(self):
        return None


async def runner_env(database, tmp_path, responses, *, max_tool_calls=8):
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
        revision="manifest-test", script_api=project(TOOLS, "manifest-test")
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
        delegated_authority=None,
        conversation_key="code-runner",
        current_group_id=None,
        bot_user_id="80001",
        gateway=None,
        runtime_config=await chat._runtime_config.snapshot(),
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=max_tool_calls,
        max_model_requests=8,
        work_control=control,
        fixed_tools=Backend().definitions(None),
    )
    return chat, provider, control, runtime, repo


ACCEPT = {"action": "accept", "goal": "compose"}
