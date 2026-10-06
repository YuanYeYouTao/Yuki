"""execute_code through the real AgentRunner loop, Work journal and restore."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.conftest import build_harness, make_settings

# P10: explicit Invocation fixture contract; existing assertions are retained.
from tests.support.agent_backend import StubAgentBackend
from tests.support.codemode_cases import BINARY, TOOLS, requires_worker
from tests.support.social_identity_cases import social_env

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.codemode.api_projection import project
from qq_ai_bot.domain.messages import ChatMessage, ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.llm.base import LLMMalformedFunctionCallError
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.mcp.artifact_access import ArtifactAccess
from qq_ai_bot.runtime.work_control import WorkControl, work_control_tools
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.agent_runner import AgentRuntime

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
        code_mode_worker_path=BINARY,
        code_mode_worker_sha256=__import__("hashlib").sha256(BINARY.read_bytes()).hexdigest(),
    )
    chat = build_harness(database, settings, provider).processor._chat
    chat.runtime.runner.code_mode_settings = settings
    # The frozen projection the deployed contract would carry.
    chat.runtime.runner.main_contract = SimpleNamespace(
        revision="manifest-test", script_api=project(TOOLS, "manifest-test")
    )
    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)

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


ACCEPT = {"action": "accept", "goal": "compose", "output_kind": "state_change"}


@pytest.mark.parametrize("segmented", [False, True])
async def test_malformed_recovery_keeps_code_effects_and_root_budget(database, tmp_path, segmented):
    code = {
        "code": "await yuki_workspace_write({'path': 'one'})\n"
        "await yuki_workspace_write({'path': 'two'})"
    }
    responses = iter(
        [
            call("task_control", ACCEPT, "accept"),
            call("execute_code", code, "code"),
            LLMMalformedFunctionCallError("synthetic invalid local-call format"),
            ChatResponse("done", 0),
        ]
    )
    chat, provider, control, runtime, repo = await runner_env(
        database, tmp_path, (), max_tool_calls=1 if segmented else 8
    )

    def respond(_request):
        step = next(responses)
        if isinstance(step, Exception):
            raise step
        return step

    provider._responder = respond
    backend = Backend()
    initial = (ChatMessage("user", "write two files once"),)
    result = await chat.runtime.runner.run(initial, runtime, backend)
    active = control
    if segmented:
        assert result.work_state == "queued"
        assert len(backend.log) == 1 and len(provider.requests) == 2
        active = WorkControl(repo, control.lease, "code-runner", {}, control.validate)
        active.current = await repo.get(control.current["id"])
        result = await chat.runtime.runner.run(
            initial, replace(runtime, work_control=active, max_tool_calls=8), backend
        )
    assert result.text == "done"
    assert [name for name, _ in backend.log] == ["workspace_write", "workspace_write"]
    assert len({identity for _, identity in backend.log}) == 2
    assert len(provider.requests) == 4
    assert active.session.progress["malformed_function_call_recoveries"] == 1
    stored = await repo.get(control.current["id"])
    assert stored["model_requests"] == 4 and stored["tool_calls"] == 2
    failed, corrected = provider.requests[-2:]
    assert corrected.request_chain_id == failed.request_chain_id
    assert corrected.tools == failed.tools
    assert [(m.tool_call_id, m.content) for m in corrected.messages if m.role == "tool"] == [
        (m.tool_call_id, m.content) for m in failed.messages if m.role == "tool"
    ]


async def test_execute_code_requires_admitted_work(database, tmp_path):
    code = {"code": "await yuki_lookup({'q': 1})"}
    responses = iter([call("execute_code", code, "code"), ChatResponse("ok", 0)])
    chat, provider, _control, runtime, _repo = await runner_env(database, tmp_path, responses)
    backend = Backend()
    await chat.runtime.runner.run((ChatMessage("user", "x"),), runtime, backend)
    result = next(m for m in provider.requests[-1].messages if m.tool_call_id == "code")
    assert json.loads(result.content)["error"] == "accept_work_before_execution"
    assert backend.log == []


async def test_execute_code_runs_children_and_pairs_the_outer_call_once(database, tmp_path):
    code = {
        "code": "import asyncio\n"
        "a, b = await asyncio.gather(yuki_lookup({'q': 1}), yuki_lookup({'q': 2}))\n"
        "w = await yuki_workspace_write({'path': 'x'})\n"
        "[a['data']['q'] + b['data']['q'], w['status']]"
    }
    responses = iter(
        [
            call("task_control", ACCEPT, "accept"),
            call("execute_code", code, "code"),
            ChatResponse("done", 0),
        ]
    )
    chat, provider, control, runtime, repo = await runner_env(database, tmp_path, responses)
    backend = Backend()
    await chat.runtime.runner.run((ChatMessage("user", "x"),), runtime, backend)
    paired = [m for m in provider.requests[-1].messages if m.tool_call_id == "code"]
    assert len(paired) == 1
    body = json.loads(paired[0].content)
    assert body["result"] == [3, "succeeded"], body
    names = [name for name, _ in backend.log]
    assert sorted(names[:2]) == ["lookup", "lookup"] and names[2] == "workspace_write"
    # Child operations derive from the outer call, never from Provider IDs.
    assert all("/c" in operation for _, operation in backend.log)
    current = await repo.get(control.current["id"])
    assert current["tool_calls"] == 3  # B01: composition itself is not charged.


@pytest.mark.parametrize("prior_write", [False, True])
async def test_future_overflow_pairs_error_and_allows_correction_without_replaying_effects(
    database, tmp_path, prior_write
):
    prefix = "await yuki_workspace_write({'path': 'first'})\n" if prior_write else ""
    bad = prefix + (
        "import asyncio\nawait asyncio.gather(*[yuki_lookup({'q': i}) for i in range(17)])"
    )
    corrected = "await yuki_lookup({'q': 42})\nawait yuki_workspace_write({'path': 'corrected'})"
    responses = iter(
        [
            call("task_control", ACCEPT, "accept"),
            call("execute_code", {"code": bad}, "bad"),
            call("execute_code", {"code": corrected}, "corrected"),
            call("task_control", {"action": "complete"}, "complete"),
        ]
    )
    chat, provider, control, runtime, repo = await runner_env(database, tmp_path, responses)
    backend = Backend()
    result = await chat.runtime.runner.run((ChatMessage("user", "x"),), runtime, backend)
    paired = [m for m in provider.requests[2].messages if m.tool_call_id == "bad"]
    assert len(paired) == 1
    body = json.loads(paired[0].content)
    assert body["error"] == "code_limit_wait_queue"
    assert body["status"] == ("partial" if prior_write else "failed")
    assert body["executed"] is prior_write
    assert "smaller awaited batches" in body["detail"]
    assert [name for name, _ in backend.log] == (
        (["workspace_write"] if prior_write else []) + ["lookup", "workspace_write"]
    )
    assert len({key for _, key in backend.log}) == len(backend.log)
    assert result.work_state == "completed"
    assert not await control.has_unresolved_effects()
    assert (await repo.get(control.current["id"]))["tool_calls"] == 2 + int(prior_write)


async def test_two_outer_code_calls_run_in_order(database, tmp_path):
    responses = iter(
        [
            call("task_control", ACCEPT, "accept"),
            ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "c1",
                        ToolFunction(
                            "execute_code",
                            json.dumps({"code": "await yuki_send_message({'text': 'one'})"}),
                        ),
                    ),
                    ToolCall(
                        "c2",
                        ToolFunction(
                            "execute_code",
                            json.dumps({"code": "await yuki_send_message({'text': 'two'})"}),
                        ),
                    ),
                ),
            ),
            ChatResponse("done", 0),
        ]
    )
    chat, _provider, _control, runtime, _repo = await runner_env(database, tmp_path, responses)
    backend = Backend()
    await chat.runtime.runner.run((ChatMessage("user", "x"),), runtime, backend)
    # C03: sequential outer compositions; distinct parents, no shared batch state.
    assert [name for name, _ in backend.log] == ["send_message", "send_message"]
    parents = {operation.rsplit("/c", 1)[0] for _, operation in backend.log}
    assert len(parents) == 2


async def test_code_mixed_with_direct_calls_is_refused(database, tmp_path):
    responses = iter(
        [
            call("task_control", ACCEPT, "accept"),
            ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall("d", ToolFunction("lookup", "{}")),
                    ToolCall("c", ToolFunction("execute_code", json.dumps({"code": "1"}))),
                ),
            ),
            ChatResponse("done", 0),
        ]
    )
    chat, provider, _control, runtime, _repo = await runner_env(database, tmp_path, responses)
    backend = Backend()
    await chat.runtime.runner.run((ChatMessage("user", "x"),), runtime, backend)
    results = [m for m in provider.requests[-1].messages if m.tool_call_id in {"c", "d"}]
    assert {json.loads(m.content)["error"] for m in results} == {"execute_code_requires_own_batch"}
    assert backend.log == []


async def test_segment_yield_keeps_outer_call_pending_and_next_segment_resumes(database, tmp_path):
    code = {
        "code": "a = await yuki_lookup({'q': 1})\n"
        "b = await yuki_lookup({'q': 2})\n"
        "[a['data']['q'], b['data']['q']]"
    }
    responses = iter([call("task_control", ACCEPT, "accept"), call("execute_code", code, "code")])
    chat, provider, control, runtime, repo = await runner_env(
        database, tmp_path, responses, max_tool_calls=1
    )
    backend = Backend()
    result = await chat.runtime.runner.run((ChatMessage("user", "x"),), runtime, backend)
    assert result.work_state == "queued"
    assert [name for name, _ in backend.log] == ["lookup"]
    requests_before = len(provider.requests)

    # Next segment: a fresh activation of the same Work restores the journal and
    # resumes the same composition before any new model request.
    from dataclasses import replace

    tail = iter([ChatResponse("done", 0)])
    provider._responder = lambda _: next(tail)  # type: ignore[attr-defined]
    resumed = WorkControl(repo, control.lease, "code-runner", {}, control.validate)
    resumed.current = await repo.get(control.current["id"])
    await chat.runtime.runner.run(
        (ChatMessage("user", "x"),),
        replace(runtime, work_control=resumed, max_tool_calls=8),
        backend,
    )
    assert [name for name, _ in backend.log] == ["lookup", "lookup"]
    assert len({operation for _, operation in backend.log}) == 2
    # The very first request of the new segment already carries the single
    # paired outer result: no model request ran before the program resumed.
    first = provider.requests[requests_before]
    paired = [m for m in first.messages if m.tool_call_id == "code"]
    assert len(paired) == 1, paired
    body = json.loads(paired[0].content)
    assert body["result"] == [1, 2]
    assert [op["status"] for op in body["operations"]] == ["succeeded", "succeeded"]
    assert (await repo.get(control.current["id"]))["tool_calls"] == 2


@pytest.mark.parametrize("missing", ["worker", "contract"])
async def test_unavailable_engine_is_a_typed_refusal(database, tmp_path, missing):
    responses = iter(
        [
            call("task_control", ACCEPT, "accept"),
            call("execute_code", {"code": "1"}, "code"),
            ChatResponse("done", 0),
        ]
    )
    chat, provider, _control, runtime, _repo = await runner_env(database, tmp_path, responses)
    if missing == "worker":
        chat.runtime.runner.code_mode_settings = None
    else:
        chat.runtime.runner.main_contract = SimpleNamespace(revision="x", script_api=None)
    await chat.runtime.runner.run((ChatMessage("user", "x"),), runtime, Backend())
    result = next(m for m in provider.requests[-1].messages if m.tool_call_id == "code")
    assert json.loads(result.content)["error"] == "code_engine_unavailable"
