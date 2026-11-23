"""E2E: an explicit per-task fallback chain, switched only after a request was not accepted.

Real loopback upstreams reach the production adapters through ``ModelClientPool``;
the executor, invocation repository, Work admission/journal and the real Main runner
run unchanged. Facts go to ``test-results/model-fallback-chain-e2e.json`` without
timings, ports or IDs, so two runs of one commit give the same bytes.

Failure cases written before the implementation (task book 2026-11-23 §8):

F1  an empty chain changes today's failure (exception, rows, diagnostics);
F2  the head's connection is refused (retries exhausted) and the fallback is not used,
    or the fallback receives the request more than once;
F3  head HTTP 429 / 503 (after its own retries) / 401 does not switch;
F4  a 0-request head failure (missing key) or a local pool timeout does not switch, or
    its row does not record ``physical_request_count=0`` / the attempt;
F5  a cut reply, a read timeout or HTTP 400/404 (outcome unknown or request-shaped)
    switches: the fallback receives a request;
F6  one hop attempt timed out reading and the retry was refused: the hop is switched
    although one attempt may have been accepted;
F8  a request carrying a fallback's continuation goes to the head (or anywhere else
    when that profile is down);
F9  after a switch, the next request of the same chain tries the head again;
F10 after a restart, a restored continuation of the fallback is refused or rerouted;
F11 the next new chain does not start at the head once it is healthy (sticky state);
F12 a fallback lacking a requested capability, native search or capacity is called;
F13 all hops fail and the raised error is not the last dispatched hop's, or its
    diagnostics miss hops or the summed physical count;
F14 a provider-native request whose head was refused cannot switch to a capable hop;
F15 a fallback hop is dispatched beyond the Work root budget;
F16 cancelling while the head waits still reaches the fallback;
F17 a hot catalog change reroutes a pinned activation, or new requests keep the old chain;
F18 the Admin snapshot drops ``fallbacks`` or a Desktop save of the snapshot loses it;
F19 an invalid chain is accepted (unknown task, unknown/duplicate/head profile, missing
    capability, structured or native-search mismatch);
F20 changing the chain or a fallback profile keeps the Profile revision (a persisted
    continuation of the old fallback would be spliced onto a changed one);
F21 a structured task does not send the head's structured mode to the fallback, or a
    repair retry does not start at the head again;
F23 the watchdog does not report a task whose latest success is a fallback, or does
    not clear once the head succeeds again;
F24 a preempted best-effort background request switches to the fallback.

F7 (empty answer, truncation, content rejection) is the same non-switch rule as the
HTTP 400 case of F5; F22 (public compact) has no continuation and follows F2, an
execution compact carries one and follows F8.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import select
from tests.conftest import build_harness, make_settings
from tests.support.agent_backend import StubAgentBackend
from tests.support.social_identity_cases import social_env
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_admin_api import command

import yuki.model_runtime.executor as executor_module
from yuki.domain.messages import (
    ChatImage,
    ChatMessage,
    ChatRequest,
    ChatTool,
    NativeToolDefinition,
    NativeToolType,
)
from yuki.llm.base import (
    LLMError,
    LLMInvalidRequestError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.db_models import ModelInvocationModel
from yuki.model_runtime.executor import BackgroundModelPreempted, TaskModelExecutor
from yuki.model_runtime.health import model_fallback_facts
from yuki.model_runtime.models import (
    ModelCapability,
    ModelExecutionPriority,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelTask,
    StructuredOutputMode,
)
from yuki.model_runtime.pool import ModelClientPool, create_model_provider
from yuki.model_runtime.profiles import (
    ModelProfileCatalog,
    ModelRuntimeConfigurationError,
    parse_model_profile_catalog,
)
from yuki.model_runtime.repository import ModelInvocationRepository
from yuki.model_runtime.routes import ModelRouter
from yuki.model_runtime.structured import StructuredTaskRunner
from yuki.runtime.origin import TurnOrigin
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_budget import limit
from yuki.runtime.work_budget_schema import budgets
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import journal
from yuki.services.agent_runner import AgentRuntime

admin = admin_fixture

ARTIFACT = Path(__file__).resolve().parents[2] / "test-results" / "model-fallback-chain-e2e.json"
RESULTS: dict[str, Any] = {}
TRIGGERS = ("refused", "http429", "http503", "http401", "missing_key", "pool_timeout")
NON_TRIGGERS = ("cut_reply", "read_timeout", "http400", "http404", "read_timeout_then_refused")
EXPECTED = (
    "F1_empty_chain_unchanged",
    *(f"F2_F4_switch[{name}]" for name in TRIGGERS),
    *(f"F5_F6_no_switch[{name}]" for name in NON_TRIGGERS),
    "F8_F9_F10_work_follows_fallback",
    "F11_new_chain_starts_at_head",
    "F12_F14_hop_prechecks",
    "F13_all_hops_fail",
    "F15_fallback_within_work_budget",
    "F16_cancel_stops_chain",
    "F17_pinned_activation_keeps_chain",
    "F18_admin_snapshot_round_trip",
    "F19_invalid_chains_rejected",
    "F20_revision_covers_chain",
    "F21_structured_task",
    "F23_watchdog_fact",
    "F24_preempted_background",
)
KEY_ENV = "MODEL_FALLBACK_E2E_KEY"
MISSING_KEY_ENV = "MODEL_FALLBACK_E2E_ABSENT_KEY"
CHAT_REPLY = {
    "id": "chat-1",
    "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}


def _responses(*output: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "resp_1",
        "status": "completed",
        "output": list(output),
        "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
    }


RESPONSES_TEXT = _responses(
    {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "done", "annotations": []}],
    }
)
RESPONSES_CALL = _responses(
    {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "read_fixture",
        "arguments": "{}",
        "status": "completed",
    }
)


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    yield
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    scenarios = json.dumps(RESULTS, ensure_ascii=False, indent=2, sort_keys=True)
    ARTIFACT.write_text(
        json.dumps(
            {
                "suite": "tests/integration/test_model_fallback_chain_e2e.py",
                "suite_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "scenarios": RESULTS,
                "scenarios_passed": sorted(RESULTS),
                "all_scenarios_passed": sorted(RESULTS) == sorted(EXPECTED),
                "results_sha256": hashlib.sha256(scenarios.encode()).hexdigest(),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _direct_network(monkeypatch):
    for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(MISSING_KEY_ENV, raising=False)


@pytest.fixture
def phases(monkeypatch):
    """Capture the content-free ``model_phases`` facts the executor records."""
    captured: list[dict[str, Any]] = []
    original = executor_module.record_trace

    async def record(kind: str, payload: object) -> None:
        if kind == "model_phases":
            assert isinstance(payload, dict)
            captured.append(payload)
        await original(kind, payload)

    monkeypatch.setattr(executor_module, "record_trace", record)
    return captured


class _Upstream:
    """Scripted HTTP/1.1 model endpoint.

    Each request consumes one step (the last one repeats): an ``int`` answers that HTTP
    status, a ``dict`` answers 200 with that JSON, ``"cut"`` drops the reply mid-body,
    ``("slow", seconds)`` answers the next step late, ``"close"`` stops listening and
    never answers the request it took (a retry is then refused).
    """

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.bodies: list[dict[str, Any]] = []
        self.received = asyncio.Event()
        self.server: asyncio.Server | None = None
        self.handlers: set[asyncio.Task[Any]] = set()

    @property
    def requests(self) -> int:
        return len(self.bodies)

    def _step(self) -> Any:
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        assert task is not None
        self.handlers.add(task)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = next(
                    int(line.split(b":", 1)[1])
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                self.bodies.append(json.loads(await reader.readexactly(length)))
                self.received.set()
                step = self._step()
                if isinstance(step, tuple):
                    await asyncio.sleep(step[1])
                    step = self._step()
                if step == "close":
                    assert self.server is not None
                    self.server.close()
                    await reader.read()  # until the client gives up on this request
                    return
                body = json.dumps(
                    step if isinstance(step, dict) else {"error": {"code": "synthetic"}}
                ).encode()
                status = 200 if isinstance(step, dict) else step
                if step == "cut":
                    status, body = 200, json.dumps(CHAT_REPLY).encode()
                writer.write(
                    f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n".encode()
                    + (body[: len(body) // 2] if step == "cut" else body)
                )
                await writer.drain()
                if step == "cut":
                    return
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            self.handlers.discard(task)

    async def __aenter__(self) -> str:
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/v1"

    async def __aexit__(self, *exc_info: object) -> None:
        assert self.server is not None
        self.server.close()
        # Idle keep-alive connections end with the scenario, not at loop shutdown.
        for task in tuple(self.handlers):
            task.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)


async def _refused_url() -> str:
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return f"http://127.0.0.1:{port}/v1"


def _profile(
    profile_id: str, base_url: str, protocol: str = "chat_completions", **fields: Any
) -> ModelProfile:
    values: dict[str, Any] = {
        "id": profile_id,
        "provider": "openai",
        "protocol": ModelProtocol(protocol),
        "base_url": base_url,
        "api_key_env": KEY_ENV,
        "model": f"model-{profile_id}",
        "timeout_seconds": 30.0,
        "max_retries": 0,
        "default_temperature": 0.5,
        "default_max_output_tokens": 64,
        "capabilities": frozenset({ModelCapability.TOOLS}),
        **fields,
    }
    return ModelProfile(**values)


def _catalog(*profiles: ModelProfile) -> ModelProfileCatalog:
    head, *rest = profiles
    return ModelProfileCatalog(
        profiles={profile.id: profile for profile in profiles},
        routes={
            task: ModelRoute(
                task=task,
                profile_id=head.id,
                fallback_profile_ids=tuple(profile.id for profile in rest),
            )
            for task in ModelTask
        },
    )


def _executor(
    database,
    *profiles: ModelProfile,
    injected: dict[str, Any] | None = None,
) -> TaskModelExecutor:
    return TaskModelExecutor(
        router=ModelRouter(_catalog(*profiles)),
        pool=ModelClientPool(secret_overrides={KEY_ENV: "synthetic"}, injected_profiles=injected),
        invocations=ModelInvocationRepository(database),
    )


REQUEST = ChatRequest(messages=(ChatMessage("user", "ping"),), model="", max_output_tokens=16)


async def _rows(database) -> list[dict[str, Any]]:
    async with database.read_session() as reader:
        rows = (
            await reader.scalars(select(ModelInvocationModel).order_by(ModelInvocationModel.id))
        ).all()
    return [
        {
            "profile_id": row.profile_id,
            "success": row.success,
            "error_category": row.error_category,
            "physical_request_count": row.physical_request_count,
        }
        for row in rows
    ]


def _hops(captured: list[dict[str, Any]]) -> list[Any]:
    return [item.get("route_hops") for item in captured]


async def _outcome(executor: TaskModelExecutor, request: ChatRequest = REQUEST, **kw: Any):
    try:
        response = await executor.execute(ModelTask.CHAT_AGENT, request, **kw)
    except LLMError as exc:
        return exc, None
    return None, response


# --------------------------------------------------------------------------- F1


async def test_empty_chain_keeps_todays_failure(database, phases):
    url = await _refused_url()
    executor = _executor(database, _profile("head", url))
    error, _ = await _outcome(executor)
    await executor.close()
    assert isinstance(error, LLMUnavailableError)
    assert isinstance(error.__cause__, httpx.ConnectError)
    assert "fallback_hops" not in error.diagnostics
    assert error.diagnostics["physical_request_count"] == 1
    rows = await _rows(database)
    assert rows == [
        {
            "profile_id": "head",
            "success": False,
            "error_category": "LLMUnavailableError",
            "physical_request_count": 1,
        }
    ]
    assert _hops(phases) == [None]
    RESULTS["F1_empty_chain_unchanged"] = {
        "error": type(error).__name__,
        "diagnostic_keys": sorted(error.diagnostics),
        "rows": rows,
    }


# --------------------------------------------------------------------------- F2-F4


@pytest.mark.parametrize("trigger", TRIGGERS)
async def test_not_accepted_head_failure_switches_once(database, phases, trigger):
    upstream = {
        "http429": _Upstream(429),
        "http503": _Upstream(503),
        "http401": _Upstream(401),
    }.get(trigger)
    fallback = _Upstream(CHAT_REPLY)
    injected: dict[str, Any] = {}
    async with fallback as fallback_url:
        if upstream is not None:
            head_url = await upstream.__aenter__()
        else:
            head_url = await _refused_url()
        # One transport retry on the head: its own bounded retry runs before a switch.
        head = _profile("head", head_url, max_retries=1)
        if trigger == "missing_key":
            head = head.model_copy(update={"api_key_env": MISSING_KEY_ENV})
        if trigger == "pool_timeout":

            def full_pool(request: httpx.Request) -> httpx.Response:
                raise httpx.PoolTimeout("no free connection", request=request)

            injected["head"] = create_model_provider(
                head,
                "synthetic",
                client=httpx.AsyncClient(
                    base_url=head_url, transport=httpx.MockTransport(full_pool)
                ),
            )
        executor = _executor(database, head, _profile("fallback", fallback_url), injected=injected)
        try:
            error, response = await _outcome(executor)
        finally:
            await executor.close()
            if upstream is not None:
                await upstream.__aexit__()
    assert error is None and response is not None
    assert response.content == "ok"
    assert fallback.requests == 1
    assert fallback.bodies[0]["model"] == "model-fallback"
    expected_head_category = {
        "refused": "LLMUnavailableError",
        "http429": "LLMRateLimitError",
        "http503": "LLMUnavailableError",
        "http401": "LLMAuthenticationError",
        "missing_key": "LLMConfigurationError",
        "pool_timeout": "LLMTimeoutError",
    }[trigger]
    head_physical = {"missing_key": 0, "http429": 1, "http401": 1}.get(trigger, 2)
    rows = await _rows(database)
    assert rows == [
        {
            "profile_id": "head",
            "success": False,
            "error_category": expected_head_category,
            "physical_request_count": head_physical,
        },
        {
            "profile_id": "fallback",
            "success": True,
            "error_category": None,
            "physical_request_count": 1,
        },
    ]
    if upstream is not None:
        assert upstream.requests == head_physical
    (hops,) = _hops(phases)
    assert [hop["profile_id"] for hop in hops] == ["head", "fallback"]
    assert [hop["outcome"] for hop in hops] == ["failed_not_accepted", "success"]
    assert phases[0]["profile_id"] == "fallback"
    RESULTS[f"F2_F4_switch[{trigger}]"] = {
        "rows": rows,
        "route_hops": hops,
        "fallback_requests": fallback.requests,
    }


# --------------------------------------------------------------------------- F5-F6


@pytest.mark.parametrize("case", NON_TRIGGERS)
async def test_unknown_or_request_shaped_failure_never_switches(database, phases, case):
    upstream = {
        "cut_reply": _Upstream("cut"),
        "read_timeout": _Upstream(("slow", 2.0), CHAT_REPLY),
        "http400": _Upstream(400),
        "http404": _Upstream(404),
        # The first attempt times out reading (maybe accepted); the retry is refused.
        "read_timeout_then_refused": _Upstream("close"),
    }[case]
    fallback = _Upstream(CHAT_REPLY)
    retries = 1 if case == "read_timeout_then_refused" else 0
    async with fallback as fallback_url, upstream as head_url:
        executor = _executor(
            database,
            _profile("head", head_url, timeout_seconds=0.5, max_retries=retries),
            _profile("fallback", fallback_url),
        )
        error, _response = await _outcome(executor)
        await executor.close()
    assert error is not None
    assert fallback.requests == 0
    expected = {
        "cut_reply": LLMUnavailableError,
        "read_timeout": LLMTimeoutError,
        "http400": LLMInvalidRequestError,
        "http404": LLMError,
        "read_timeout_then_refused": LLMUnavailableError,
    }[case]
    assert type(error) is expected, error
    rows = await _rows(database)
    assert [row["profile_id"] for row in rows] == ["head"]
    (hops,) = _hops(phases)
    assert [hop["outcome"] for hop in hops] == ["failed"]
    RESULTS[f"F5_F6_no_switch[{case}]"] = {
        "error": type(error).__name__,
        "cause": type(error.__cause__).__name__ if error.__cause__ else None,
        "head_requests": upstream.requests,
        "fallback_requests": fallback.requests,
    }


# --------------------------------------------------------------------------- F8-F10


async def test_work_chain_follows_the_answering_fallback(database, tmp_path, phases):
    head_upstream = _Upstream(503)
    fallback_upstream = _Upstream(RESPONSES_CALL, RESPONSES_TEXT)
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(repository, lease, "fallback-chain", {}, validate)
    control.current = await repository.accept(
        lease, source_key="fallback-chain", source=control.source, goal="read fixture"
    )
    harness = build_harness(database, make_settings(database.url), FakeLLMProvider())
    chat = harness.processor._chat
    executed: list[str] = []

    async def read_fixture(invocation):
        executed.append(invocation.call.function.name)
        return json.dumps({"ok": True, "value": "fixture"})

    backend = StubAgentBackend(execute_call=read_fixture, parallel_safe=lambda *a: True)
    async with head_upstream as head_url, fallback_upstream as fallback_url:
        head = _profile("head", head_url)
        fallback = _profile("fallback", fallback_url, protocol="responses")
        executor = _executor(database, head, fallback)
        chat.runtime.runner._models = executor
        runtime = AgentRuntime(
            origin=TurnOrigin.USER_MESSAGE,
            actor_user_id="1001",
            actor_is_superuser=False,
            conversation_key="fallback-chain",
            current_group_id=None,
            bot_user_id="9999",
            gateway=None,
            runtime_config=await chat._runtime_config.snapshot(),
            allowed_capabilities=frozenset(),
            fixed_tools=(ChatTool("read_fixture", "read", {"type": "object"}),),
            work_control=control,
        )
        # The production Work activation binds its control for every model call.
        token = current_work_control.set(control)
        try:
            result = await chat.runtime.runner.run(
                (ChatMessage("system", "fixed"), ChatMessage("user", "read the fixture")),
                runtime,
                backend,
            )
        finally:
            current_work_control.reset(token)
        # F8/F10: a new process (fresh pool, same catalog) given the fallback's
        # continuation, as restored from the journal, goes to the fallback only.
        restored = _executor(database, head, fallback)
        first = await restored.execute(ModelTask.CHAT_AGENT, REQUEST)
        assert first.continuation is not None and first.continuation.profile_id == "fallback"
        before = head_upstream.requests
        await restored.close()
        await fallback_upstream.__aexit__()
        followed = ChatRequest(
            messages=(ChatMessage("user", "ping"),),
            model="",
            continuation=first.continuation,
        )
        # Another fresh process while the fallback is down: no reused connection.
        pinned = _executor(database, head, fallback)
        pinned_error, _ = await _outcome(pinned, followed)
        await pinned.close()
        await executor.close()
    persisted = await repository.get(control.current["id"])
    async with database.read_session() as reader:
        phase = await reader.scalar(
            select(journal.c.phase).where(journal.c.work_id == persisted["id"])
        )
    await repository.release(lease)
    assert result.outcome is None or result.outcome.failure is None, result.outcome
    assert executed == ["read_fixture"]
    # F9: the head was tried once at the chain's first request only.
    assert head_upstream.requests == before == 2
    assert fallback_upstream.requests == 3
    second_input = fallback_upstream.bodies[1]["input"]
    assert any(item.get("type") == "function_call_output" for item in second_input)
    # One main request + one auxiliary fallback hop + the follow-up main request.
    assert persisted["model_requests"] == 3
    # F8: the pinned profile is down; nothing is sent to the head.
    assert isinstance(pinned_error, LLMUnavailableError)
    assert "fallback_hops" not in pinned_error.diagnostics
    rows = await _rows(database)
    assert [(row["profile_id"], row["success"]) for row in rows] == [
        ("head", False),
        ("fallback", True),
        ("fallback", True),
        ("head", False),
        ("fallback", True),
        ("fallback", False),
    ]
    RESULTS["F8_F9_F10_work_follows_fallback"] = {
        "rows": rows,
        "head_requests": head_upstream.requests,
        "fallback_requests": fallback_upstream.requests,
        "work_model_requests": persisted["model_requests"],
        "journal_phase": phase,
        "pinned_failure": type(pinned_error).__name__,
    }


# --------------------------------------------------------------------------- F11, F13


async def test_next_chain_starts_at_the_recovered_head(database, phases):
    head_upstream = _Upstream(503, CHAT_REPLY)
    fallback_upstream = _Upstream(CHAT_REPLY)
    async with head_upstream as head_url, fallback_upstream as fallback_url:
        executor = _executor(
            database, _profile("head", head_url), _profile("fallback", fallback_url)
        )
        first = await executor.execute(ModelTask.CHAT_AGENT, REQUEST)
        second = await executor.execute(ModelTask.CHAT_AGENT, REQUEST)
        await executor.close()
    assert first.content == second.content == "ok"
    assert head_upstream.requests == 2 and fallback_upstream.requests == 1
    hops = _hops(phases)
    assert [[hop["profile_id"] for hop in item] for item in hops] == [
        ["head", "fallback"],
        ["head"],
    ]
    RESULTS["F11_new_chain_starts_at_head"] = {"route_hops": hops}


async def test_all_hops_failing_raise_the_last_dispatched_error(database, phases):
    head_url = await _refused_url()
    upstream = _Upstream(503)
    async with upstream as fallback_url:
        images = frozenset({ModelCapability.TOOLS, ModelCapability.IMAGE_INPUT})
        executor = _executor(
            database,
            _profile("head", head_url, capabilities=images),
            _profile("fallback", fallback_url, max_retries=1, capabilities=images),
            # Lacks image input: skipped, never the raised error.
            _profile("textonly", head_url),
        )
        request = ChatRequest(
            messages=(ChatMessage("user", "look", images=(ChatImage(_PIXEL),)),),
            model="",
        )
        error, _ = await _outcome(executor, request)
        await executor.close()
    assert isinstance(error, LLMUnavailableError)
    assert error.diagnostics["http_status"] == 503
    assert error.diagnostics["physical_request_count"] == 3
    hops = error.diagnostics["fallback_hops"]
    assert [(hop["profile_id"], hop["outcome"]) for hop in hops] == [
        ("head", "failed_not_accepted"),
        ("fallback", "failed_not_accepted"),
        ("textonly", "skipped_capability"),
    ]
    assert upstream.requests == 2
    RESULTS["F13_all_hops_fail"] = {
        "error": type(error).__name__,
        "physical_request_count": error.diagnostics["physical_request_count"],
        "fallback_hops": hops,
    }


_PIXEL = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


# --------------------------------------------------------------------------- F12, F14


async def test_hop_prechecks_skip_incapable_fallbacks(database, phases):
    head_url = await _refused_url()
    capable = _Upstream(RESPONSES_TEXT)
    never = _Upstream(RESPONSES_TEXT)
    native = frozenset(
        {ModelCapability.TOOLS, ModelCapability.NATIVE_WEB_SEARCH, ModelCapability.REASONING}
    )
    async with capable as capable_url, never as never_url:
        head = _profile("head", head_url, protocol="responses", capabilities=native)
        executor = _executor(
            database,
            head,
            # No native web search: a provider-native request skips it.
            _profile("plain", never_url, protocol="responses"),
            # Too small for this request.
            _profile(
                "small",
                never_url,
                protocol="responses",
                capabilities=native,
                max_input_tokens=1,
            ),
            _profile("capable", capable_url, protocol="responses", capabilities=native),
        )
        request = ChatRequest(
            messages=(ChatMessage("user", "search the news " * 20),),
            model="",
            native_tools=(NativeToolDefinition(NativeToolType.WEB_SEARCH),),
        )
        error, response = await _outcome(executor, request)
        await executor.close()
    assert error is None and response is not None and response.content == "done"
    assert never.requests == 0 and capable.requests == 1
    assert any(tool.get("type") == "web_search" for tool in capable.bodies[0]["tools"])
    (hops,) = _hops(phases)
    assert [(hop["profile_id"], hop["outcome"]) for hop in hops] == [
        ("head", "failed_not_accepted"),
        ("plain", "skipped_capability"),
        ("small", "skipped_capacity"),
        ("capable", "success"),
    ]
    RESULTS["F12_F14_hop_prechecks"] = {"route_hops": hops}


# --------------------------------------------------------------------------- F15


async def test_fallback_hop_cannot_exceed_the_work_budget(database, tmp_path):
    head_upstream = _Upstream(503)
    fallback_upstream = _Upstream(RESPONSES_TEXT)
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(repository, lease, "budget", {}, validate)
    control.current = await repository.accept(
        lease, source_key="budget", source=control.source, goal="one request only"
    )
    async with database.immediate_session() as writer:
        await limit(writer, control.current["id"], models=1, tools=None)
    harness = build_harness(database, make_settings(database.url), FakeLLMProvider())
    chat = harness.processor._chat
    async with head_upstream as head_url, fallback_upstream as fallback_url:
        executor = _executor(
            database,
            _profile("head", head_url),
            _profile("fallback", fallback_url, protocol="responses"),
        )
        chat.runtime.runner._models = executor
        runtime = AgentRuntime(
            origin=TurnOrigin.USER_MESSAGE,
            actor_user_id="1001",
            actor_is_superuser=False,
            conversation_key="budget",
            current_group_id=None,
            bot_user_id="9999",
            gateway=None,
            runtime_config=await chat._runtime_config.snapshot(),
            allowed_capabilities=frozenset(),
            fixed_tools=(ChatTool("read_fixture", "read", {"type": "object"}),),
            work_control=control,
        )
        token = current_work_control.set(control)
        try:
            result = await chat.runtime.runner.run(
                (ChatMessage("system", "fixed"), ChatMessage("user", "one request only")),
                runtime,
                StubAgentBackend(),
            )
        finally:
            current_work_control.reset(token)
        await executor.close()
    persisted = await repository.get(control.current["id"])
    async with database.read_session() as reader:
        spent = await reader.scalar(
            select(budgets.c.models).where(budgets.c.root_id == control.current["id"])
        )
    await repository.release(lease)
    assert head_upstream.requests == 1 and fallback_upstream.requests == 0
    assert spent == 1
    failure = result.outcome.failure
    assert failure is not None
    RESULTS["F15_fallback_within_work_budget"] = {
        "failure_code": failure.code,
        "budget_models": spent,
        "work_model_requests": persisted["model_requests"],
        "fallback_requests": fallback_upstream.requests,
    }


# --------------------------------------------------------------------------- F16, F24


async def test_cancel_while_the_head_waits_stops_the_chain(database):
    head_upstream = _Upstream(("slow", 30.0), CHAT_REPLY)
    fallback_upstream = _Upstream(CHAT_REPLY)
    async with head_upstream as head_url, fallback_upstream as fallback_url:
        executor = _executor(
            database, _profile("head", head_url), _profile("fallback", fallback_url)
        )
        task = asyncio.create_task(executor.execute(ModelTask.CHAT_AGENT, REQUEST))
        await head_upstream.received.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await executor.close()
    assert fallback_upstream.requests == 0
    RESULTS["F16_cancel_stops_chain"] = {"fallback_requests": fallback_upstream.requests}


async def test_preempted_background_request_does_not_switch(database):
    head_upstream = _Upstream(("slow", 30.0), CHAT_REPLY)
    fallback_upstream = _Upstream(CHAT_REPLY)
    async with head_upstream as head_url, fallback_upstream as fallback_url:
        executor = _executor(
            database, _profile("head", head_url), _profile("fallback", fallback_url)
        )
        task = asyncio.create_task(
            executor.execute(
                ModelTask.MEMORY_SELF_REFLECTION,
                REQUEST,
                priority=ModelExecutionPriority.BEST_EFFORT_BACKGROUND,
            )
        )
        await head_upstream.received.wait()
        async with executor.foreground_activity():
            with pytest.raises(BackgroundModelPreempted):
                await task
        await executor.close()
    assert fallback_upstream.requests == 0
    RESULTS["F24_preempted_background"] = {"fallback_requests": fallback_upstream.requests}


# --------------------------------------------------------------------------- F17, F20


async def test_pinned_activation_keeps_its_chain(database):
    head_url = await _refused_url()
    old_fallback = _Upstream(CHAT_REPLY)
    new_fallback = _Upstream(CHAT_REPLY)
    async with old_fallback as old_url, new_fallback as new_url:
        head = _profile("head", head_url)
        executor = _executor(database, head, _profile("old", old_url))
        with executor.pin():
            executor.apply_catalog(
                _catalog(head, _profile("new", new_url)),
                ModelClientPool(secret_overrides={KEY_ENV: "synthetic"}),
            )
            await executor.execute(ModelTask.CHAT_AGENT, REQUEST)
        await executor.execute(ModelTask.CHAT_AGENT, REQUEST)
        await executor.close()
    assert old_fallback.requests == 1 and new_fallback.requests == 1
    RESULTS["F17_pinned_activation_keeps_chain"] = {
        "old_chain_requests": old_fallback.requests,
        "new_chain_requests": new_fallback.requests,
    }


def test_profile_revision_covers_the_chain():
    head = _profile("head", "https://head.test/v1")
    fallback = _profile("fallback", "https://fallback.test/v1")

    def revision(*profiles: ModelProfile) -> str:
        return TaskModelExecutor(
            router=ModelRouter(_catalog(*profiles)),
            pool=ModelClientPool(injected_profiles={}),
        ).profile_revision(ModelTask.CHAT_AGENT)

    revisions = {
        "no_chain": revision(head),
        "chain": revision(head, fallback),
        "fallback_changed": revision(head, fallback.model_copy(update={"model": "other"})),
        "fallback_connect_tuned": revision(
            head, fallback.model_copy(update={"connect_timeout_seconds": 3.0})
        ),
    }
    assert len({revisions["no_chain"], revisions["chain"], revisions["fallback_changed"]}) == 3
    # Transport timing is not serialized and keeps existing chains.
    assert revisions["fallback_connect_tuned"] == revisions["chain"]
    # An empty chain keeps the revision of origin/main 89f82e4f for this profile:
    # deploying this splits no existing Work chain.
    assert revisions["no_chain"] == (
        "25624487e4fd7062fc0831c37a0962aadd0b35e49a98eb271fc822b61c842120"
    )
    RESULTS["F20_revision_covers_chain"] = {
        "distinct": ["chain", "fallback_changed", "no_chain"],
        "no_chain_matches_main": True,
        "connect_timeout_ignored": True,
    }


# --------------------------------------------------------------------------- F18-F19


async def test_admin_snapshot_round_trips_fallbacks(admin):
    client, api, *_ = admin
    current = (await client.get("/admin/v1/models")).json()["data"]
    document = current["document"]
    assert document["fallbacks"] == {}
    document["profiles"]["backup"] = dict(document["profiles"]["main"])
    document["fallbacks"] = {"chat_agent": ["backup"]}
    applied = await client.post(
        "/admin/v1/models/apply", json=command(current["revision"], document=document)
    )
    assert applied.status_code == 200, applied.text
    files = api.commands.model.files
    assert files.loaded_catalog.routes[ModelTask.CHAT_AGENT].fallback_profile_ids == ("backup",)

    # F18: Desktop reads the snapshot and saves it back unchanged.
    shown = (await client.get("/admin/v1/models")).json()["data"]
    assert shown["document"]["fallbacks"] == {"chat_agent": ["backup"]}
    saved = await client.post(
        "/admin/v1/models/apply", json=command(shown["revision"], document=shown["document"])
    )
    assert saved.status_code == 200, saved.text
    assert files.loaded_catalog.routes[ModelTask.CHAT_AGENT].fallback_profile_ids == ("backup",)
    assert "[fallbacks]" in files._settings.model_profiles_file.read_text(encoding="utf-8")

    # F19 through the management entry: an invalid chain is refused, the file is kept.
    latest = (await client.get("/admin/v1/models")).json()["data"]
    invalid = dict(latest["document"], fallbacks={"chat_agent": ["missing"]})
    refused = await client.post(
        "/admin/v1/models/apply", json=command(latest["revision"], document=invalid)
    )
    assert refused.status_code == 422, refused.text
    assert files.loaded_catalog.routes[ModelTask.CHAT_AGENT].fallback_profile_ids == ("backup",)
    RESULTS["F18_admin_snapshot_round_trip"] = {
        "snapshot_fallbacks": shown["document"]["fallbacks"],
        "kept_after_desktop_save": True,
        "invalid_refused_status": refused.status_code,
    }


_BASE_TOML = """
schema_version = 3

[profiles.head]
provider = "openai"
base_url = "https://head.test/v1"
model = "head"
timeout_seconds = 30
max_retries = 0
default_temperature = 0.5
default_max_output_tokens = 64
{head_extra}

[profiles.backup]
provider = "openai"
base_url = "https://backup.test/v1"
model = "backup"
timeout_seconds = 30
max_retries = 0
default_temperature = 0.5
default_max_output_tokens = 64
capabilities = {backup_capabilities}

[routes]
chat_agent = "head"
memory_extraction = "head"
memory_self_reflection = "head"
memory_dream = "head"
plugin_agent_session = "head"
utility_structured = "head"

{fallbacks}
"""


def _toml(
    fallbacks: str,
    *,
    backup_capabilities: str = '["tools"]',
    head_extra: str = 'capabilities = ["tools"]',
) -> str:
    return _BASE_TOML.format(
        fallbacks=fallbacks, backup_capabilities=backup_capabilities, head_extra=head_extra
    )


INVALID_CHAINS = {
    "unknown_task": _toml('[fallbacks]\nnot_a_task = ["backup"]'),
    "unknown_profile": _toml('[fallbacks]\nchat_agent = ["missing"]'),
    "head_repeated": _toml('[fallbacks]\nchat_agent = ["head"]'),
    "duplicate": _toml('[fallbacks]\nchat_agent = ["backup", "backup"]'),
    "missing_required_capability": _toml(
        '[fallbacks]\nchat_agent = ["backup"]', backup_capabilities="[]"
    ),
    "structured_mode_mismatch": _toml(
        '[fallbacks]\nmemory_extraction = ["backup"]',
        backup_capabilities="[]",
        head_extra='structured_output_mode = "json_schema"\n'
        'capabilities = ["tools", "structured_output"]',
    ),
    "native_search_mismatch": _toml(
        '[fallbacks]\nchat_agent = ["backup"]',
        head_extra='protocol = "responses"\nsearch_mode = "native"\n'
        'capabilities = ["tools", "native_web_search"]',
    ),
}


def test_invalid_chains_are_rejected():
    rejected = {}
    for name, content in INVALID_CHAINS.items():
        with pytest.raises(ModelRuntimeConfigurationError) as caught:
            parse_model_profile_catalog(content)
        rejected[name] = type(caught.value).__name__
    valid = parse_model_profile_catalog(_toml('[fallbacks]\nchat_agent = ["backup"]'))
    assert valid.routes[ModelTask.CHAT_AGENT].fallback_profile_ids == ("backup",)
    assert valid.routes[ModelTask.MEMORY_DREAM].fallback_profile_ids == ()
    assert (
        parse_model_profile_catalog(_toml("")).routes[ModelTask.CHAT_AGENT].fallback_profile_ids
        == ()
    )
    RESULTS["F19_invalid_chains_rejected"] = {"rejected": rejected}


# --------------------------------------------------------------------------- F21


class _Extracted(BaseModel):
    value: str


async def test_structured_task_sends_the_head_mode_to_the_fallback(database):
    head_upstream = _Upstream(503)
    emitted = {
        **CHAT_REPLY,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "emit_result", "arguments": '{"value": 1}'},
                        }
                    ],
                },
            }
        ],
    }
    valid = json.loads(json.dumps(emitted))
    valid["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '{"value": "x"}'
    fallback_upstream = _Upstream(emitted, valid)
    async with head_upstream as head_url, fallback_upstream as fallback_url:
        executor = _executor(
            database,
            _profile("head", head_url, structured_output_mode=StructuredOutputMode.FUNCTION_TOOL),
            _profile(
                "fallback",
                fallback_url,
                structured_output_mode=StructuredOutputMode.JSON_SCHEMA,
                capabilities=frozenset({ModelCapability.TOOLS, ModelCapability.STRUCTURED_OUTPUT}),
            ),
        )
        result = await StructuredTaskRunner(executor).run(
            task=ModelTask.MEMORY_EXTRACTION,
            instruction="extract",
            structured_input={"text": "x"},
            output_model=_Extracted,
            validation_retries=1,
        )
        await executor.close()
    assert result.value == "x"
    # The head was tried first for the request and again for the repair retry.
    assert head_upstream.requests == 2 and fallback_upstream.requests == 2
    assert all(
        body["tools"][0]["function"]["name"] == "emit_result" for body in fallback_upstream.bodies
    )
    assert all("response_format" not in body for body in fallback_upstream.bodies)
    RESULTS["F21_structured_task"] = {
        "head_requests": head_upstream.requests,
        "fallback_requests": fallback_upstream.requests,
        "fallback_mode": "function_tool",
    }


# --------------------------------------------------------------------------- F23


async def test_watchdog_fact_follows_latest_success(database):
    head_upstream = _Upstream(503, CHAT_REPLY)
    fallback_upstream = _Upstream(CHAT_REPLY)
    async with head_upstream as head_url, fallback_upstream as fallback_url:
        executor = _executor(
            database, _profile("head", head_url), _profile("fallback", fallback_url)
        )
        invocations = ModelInvocationRepository(database)
        before = await model_fallback_facts(executor, invocations)
        await executor.execute(ModelTask.CHAT_AGENT, REQUEST)
        during = await model_fallback_facts(executor, invocations)
        await executor.execute(ModelTask.CHAT_AGENT, REQUEST)
        after = await model_fallback_facts(executor, invocations)
        await executor.close()
    chat = "model_fallback:chat_agent"
    # No success yet: undetermined, never cleared or opened.
    assert before[chat]["healthy"] is None
    assert during[chat] == {"healthy": False, "state": "fallback", "durable": True}
    assert after[chat]["healthy"] is True
    # Tasks without a configured chain are not watched.
    no_chain = TaskModelExecutor(
        router=ModelRouter(
            ModelProfileCatalog(
                profiles={"head": _profile("head", head_url)},
                routes={task: ModelRoute(task=task, profile_id="head") for task in ModelTask},
            )
        ),
        pool=ModelClientPool(injected_profiles={}),
    )
    assert await model_fallback_facts(no_chain, invocations) == {}
    RESULTS["F23_watchdog_fact"] = {
        "before": before[chat],
        "during": during[chat],
        "after": after[chat],
    }
