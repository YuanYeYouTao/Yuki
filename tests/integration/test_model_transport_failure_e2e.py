"""E2E: model requests fail fast while connecting and report a dropped reply as unknown.

Real loopback sockets reach every protocol adapter through the production
``ModelClientPool``; the Work scenarios run the real Main runner, Work admission and
journal. Facts go to ``test-results/model-transport-failure-e2e.json`` without timings
or ports, so two runs of one commit give the same bytes.

Failure cases written before the implementation (fallback task book 2026-11-23 §2, Q12):

T1  a TCP connection is accepted but TLS never answers: the request waits for
    ``timeout_seconds`` instead of ``connect_timeout_seconds`` (any protocol);
T2  the production proxy shape (CONNECT 200, then the tunnel TLS stalls) is not bounded
    by ``connect_timeout_seconds``;
T3  a connect-phase timeout is not retried under the existing transport rule
    (``max_retries`` + 1 connection attempts, still ``LLMTimeoutError``);
T4  a Profile that does not set the field keeps the 600-second connect wait;
T5  a slow upstream answer (read phase) is cut by ``connect_timeout_seconds``;
T6  setting or defaulting ``connect_timeout_seconds`` changes the Profile revision, which
    would open a new chain for every persistent Work on deployment;
T7  the Admin snapshot drops the field, or a Desktop save of the snapshot loses it;
T8  a reply cut mid-body surfaces as a bare ``httpx.RemoteProtocolError`` (any protocol);
T9  Responses re-sends a request whose reply was cut (outcome unknown);
T10 in a Work, the cut reply leaves the journal or recovery outside the existing
    transport-unknown path: a plain request must close ``paired`` with the retryable
    provider failure, a provider-native request must keep ``dispatched`` and fail with
    ``LLMNativeToolError`` (no automatic replay, cold resume refuses to re-buy);
T11 Responses does not retry a proxy that refuses CONNECT (502/403): the request never
    reached the provider, so it must be re-sent under the transport rule and either
    succeed through a later tunnel or fail as ``LLMUnavailableError`` once exhausted.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import ssl
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import trustme
from sqlalchemy import select
from tests.conftest import build_harness, make_settings
from tests.support.agent_backend import StubAgentBackend
from tests.support.social_identity_cases import social_env
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_admin_api import command

from yuki.domain.messages import ChatMessage, ChatRequest, ChatTool
from yuki.http_environment import load_http_environment
from yuki.llm.base import LLMError
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.executor import TaskModelExecutor
from yuki.model_runtime.models import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelSearchMode,
    ModelTask,
)
from yuki.model_runtime.pool import ModelClientPool
from yuki.model_runtime.profiles import ModelProfileCatalog
from yuki.model_runtime.routes import ModelRouter
from yuki.runtime.origin import TurnOrigin
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_journal import JournalUnavailable
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import journal
from yuki.services.agent_runner import AgentRuntime

admin = admin_fixture

ARTIFACT = Path(__file__).resolve().parents[2] / "test-results" / "model-transport-failure-e2e.json"
RESULTS: dict[str, Any] = {}
PROTOCOLS = ("chat_completions", "responses", "anthropic_messages", "gemini")
EXPECTED = (
    *(f"S1_connect_hang_bounded[{name}]" for name in PROTOCOLS),
    "S2_proxy_tunnel_tls_hang_bounded",
    "S3_default_connect_timeout_bounds_600s_profile",
    *(f"S4_slow_read_unaffected[{name}]" for name in PROTOCOLS),
    "S5_revision_ignores_connect_timeout",
    "S6_admin_snapshot_round_trip",
    *(f"S7_cut_reply_unknown[{name}]" for name in PROTOCOLS),
    "S8_work_cut_reply[plain]",
    "S8_work_cut_reply[native]",
    "S9_proxy_refusal_retried[recovers]",
    "S9_proxy_refusal_retried[exhausted]",
)
KEY_ENV = "MODEL_TRANSPORT_E2E_KEY"
PROVIDERS = {
    "chat_completions": "openai",
    "responses": "openai",
    "anthropic_messages": "anthropic",
    "gemini": "gemini",
}
REPLIES: dict[str, dict[str, Any]] = {
    "chat_completions": {
        "id": "chat-1",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "ok"},
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    },
    "responses": {
        "id": "resp_1",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "ok", "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    },
    "anthropic_messages": {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    },
    "gemini": {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}
        ],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
    },
}
_PROXY_ENV = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
)


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    yield
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    scenarios = json.dumps(RESULTS, ensure_ascii=False, indent=2, sort_keys=True)
    ARTIFACT.write_text(
        json.dumps(
            {
                "suite": "tests/integration/test_model_transport_failure_e2e.py",
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
    """Loopback endpoints are reached directly unless a scenario installs its proxy."""
    for name in _PROXY_ENV:
        monkeypatch.delenv(name, raising=False)


class _Silent:
    """Accepts TCP and never answers: TLS (or a tunnel's TLS) stays in its handshake."""

    def __init__(self) -> None:
        self.connections = 0

    async def handle(self, reader, writer):
        self.connections += 1
        try:
            while await reader.read(65536):
                pass
        except ConnectionError:
            pass
        finally:
            writer.close()


class _StallingProxy:
    """CONNECT proxy that confirms the tunnel, then never relays (11-17 production shape)."""

    def __init__(self) -> None:
        self.tunnels = 0

    async def handle(self, reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            self.tunnels += 1
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            while await reader.read(65536):
                pass
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


class _Upstream:
    """Plain HTTP/1.1 model endpoint: ``slow`` answers late, ``cut`` drops mid-body."""

    def __init__(self, mode: str, reply: dict[str, Any], delay: float = 0.0) -> None:
        self.mode = mode
        self.body = json.dumps(reply).encode()
        self.delay = delay
        self.requests = 0

    async def handle(self, reader, writer):
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = next(
                    int(line.split(b":", 1)[1])
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                await reader.readexactly(length)
                self.requests += 1
                status = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                if self.mode == "cut":
                    # Headers promise the whole body; the connection ends halfway.
                    writer.write(
                        status
                        + f"Content-Length: {len(self.body)}\r\n\r\n".encode()
                        + self.body[: len(self.body) // 2]
                    )
                    await writer.drain()
                    return
                await asyncio.sleep(self.delay)
                writer.write(status + f"Content-Length: {len(self.body)}\r\n\r\n".encode())
                writer.write(self.body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


async def _serve(handler) -> tuple[asyncio.Server, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def _profile(protocol: str, base_url: str, **fields: Any) -> ModelProfile:
    values: dict[str, Any] = {
        "id": f"e2e-{protocol}",
        "provider": PROVIDERS[protocol],
        "protocol": ModelProtocol(protocol),
        "base_url": base_url,
        "api_key_env": KEY_ENV,
        "model": "synthetic-model",
        "timeout_seconds": 30.0,
        "max_retries": 2,
        "default_temperature": 0.5,
        "default_max_output_tokens": 64,
        **fields,
    }
    return ModelProfile(**values)


REQUEST = ChatRequest(
    messages=(ChatMessage("user", "ping"),), model="synthetic-model", max_output_tokens=16
)


async def _complete(profile: ModelProfile) -> tuple[str, str | None, float]:
    pool = ModelClientPool(secret_overrides={KEY_ENV: "synthetic"})
    started = time.monotonic()
    try:
        response = await pool.get(profile).complete(REQUEST)
    except LLMError as exc:
        cause = exc.__cause__
        return (
            type(exc).__name__,
            type(cause).__name__ if cause else None,
            (time.monotonic() - started),
        )
    except Exception as exc:  # The pre-fix bare transport error is the failure under test.
        return f"bare:{type(exc).__name__}", None, time.monotonic() - started
    finally:
        await pool.close()
    return f"ok:{response.content}", None, time.monotonic() - started


# --------------------------------------------------------------------------- S1-S3


@pytest.mark.parametrize("protocol", PROTOCOLS)
async def test_connect_hang_fails_within_connect_timeout_and_retries(protocol):
    silent = _Silent()
    server, port = await _serve(silent.handle)
    try:
        # T1/T3: 30-second read budget, 0.2-second connect budget, two transport retries.
        outcome, cause, elapsed = await _complete(
            _profile(protocol, f"https://127.0.0.1:{port}/v1", connect_timeout_seconds=0.2)
        )
    finally:
        server.close()
    assert (outcome, cause) == ("LLMTimeoutError", "ConnectTimeout")
    assert silent.connections == 3  # max_retries + 1 under the existing transport rule
    # Three 0.2 s connects plus bounded backoff (<= 2 s each); far below one 30 s timeout.
    assert elapsed < 10
    RESULTS[f"S1_connect_hang_bounded[{protocol}]"] = {
        "error": outcome,
        "cause": cause,
        "connection_attempts": silent.connections,
        "finished_well_before_timeout_seconds": elapsed < 10,
    }


async def test_proxy_tunnel_tls_hang_is_a_connect_timeout(monkeypatch, tmp_path):
    proxy = _StallingProxy()
    server, port = await _serve(proxy.handle)
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{port}")
    load_http_environment(tmp_path / "missing.env")  # the production startup entry
    try:
        # T2: CONNECT is confirmed, then the TLS handshake through the tunnel stalls.
        outcome, cause, elapsed = await _complete(
            _profile("responses", "https://model.test/v1", connect_timeout_seconds=0.2)
        )
    finally:
        server.close()
    assert (outcome, cause) == ("LLMTimeoutError", "ConnectTimeout")
    assert proxy.tunnels == 3
    assert elapsed < 10
    RESULTS["S2_proxy_tunnel_tls_hang_bounded"] = {
        "error": outcome,
        "cause": cause,
        "tunnel_attempts": proxy.tunnels,
        "finished_well_before_timeout_seconds": elapsed < 10,
    }


async def test_unset_connect_timeout_uses_default_not_the_600_second_timeout():
    silent = _Silent()
    server, port = await _serve(silent.handle)
    profile = _profile(
        "responses", f"https://127.0.0.1:{port}/v1", timeout_seconds=600.0, max_retries=0
    )
    try:
        # T4: the production Profile shape (600 s, field absent).
        assert "connect_timeout_seconds" not in profile.model_fields_set
        async with asyncio.timeout(60):
            outcome, cause, elapsed = await _complete(profile)
    finally:
        server.close()
    assert (outcome, cause) == ("LLMTimeoutError", "ConnectTimeout")
    assert silent.connections == 1
    assert DEFAULT_CONNECT_TIMEOUT_SECONDS - 0.5 < elapsed < DEFAULT_CONNECT_TIMEOUT_SECONDS + 5
    RESULTS["S3_default_connect_timeout_bounds_600s_profile"] = {
        "error": outcome,
        "cause": cause,
        "default_connect_timeout_seconds": DEFAULT_CONNECT_TIMEOUT_SECONDS,
        "elapsed_matches_default": True,
    }


# --------------------------------------------------------------------------- S4


@pytest.mark.parametrize("protocol", PROTOCOLS)
async def test_slow_read_is_not_cut_by_connect_timeout(protocol):
    upstream = _Upstream("slow", REPLIES[protocol], delay=1.0)
    server, port = await _serve(upstream.handle)
    try:
        # T5: the reply takes five times the connect budget; the read budget is 30 s.
        outcome, cause, elapsed = await _complete(
            _profile(protocol, f"http://127.0.0.1:{port}/v1", connect_timeout_seconds=0.2)
        )
    finally:
        server.close()
    assert outcome == "ok:ok", (outcome, cause)
    assert upstream.requests == 1
    assert elapsed >= 1.0
    RESULTS[f"S4_slow_read_unaffected[{protocol}]"] = {
        "outcome": outcome,
        "requests": upstream.requests,
        "reply_delay_exceeds_connect_timeout": True,
    }


# --------------------------------------------------------------------------- S5-S6


def _executor(profile: ModelProfile) -> TaskModelExecutor:
    return TaskModelExecutor(
        router=ModelRouter(
            ModelProfileCatalog(
                profiles={profile.id: profile},
                routes={task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask},
            )
        ),
        pool=ModelClientPool(injected_profiles={profile.id: FakeLLMProvider()}),
    )


async def test_profile_revision_ignores_connect_timeout():
    base = _profile("responses", "https://model.test/v1")
    revisions = {
        "unset": _executor(base).profile_revision(ModelTask.CHAT_AGENT),
        "explicit_default": _executor(
            base.model_copy(update={"connect_timeout_seconds": DEFAULT_CONNECT_TIMEOUT_SECONDS})
        ).profile_revision(ModelTask.CHAT_AGENT),
        "explicit_3s": _executor(
            _profile("responses", "https://model.test/v1", connect_timeout_seconds=3.0)
        ).profile_revision(ModelTask.CHAT_AGENT),
    }
    # T6: the field is transport timing, never serialized; existing chains stay intact.
    assert len(set(revisions.values())) == 1, revisions
    # A real serialized setting still changes the revision.
    assert (
        _executor(base.model_copy(update={"model": "other"})).profile_revision(ModelTask.CHAT_AGENT)
        != revisions["unset"]
    )
    RESULTS["S5_revision_ignores_connect_timeout"] = {"identical_revisions": sorted(revisions)}


async def test_admin_snapshot_round_trips_connect_timeout(admin):
    client, api, *_ = admin
    current = (await client.get("/admin/v1/models")).json()["data"]
    document = current["document"]
    assert "connect_timeout_seconds" not in document["profiles"]["main"]
    document["profiles"]["main"]["connect_timeout_seconds"] = 7.5
    applied = await client.post(
        "/admin/v1/models/apply", json=command(current["revision"], document=document)
    )
    assert applied.status_code == 200, applied.text
    loaded = api.commands.model.files.loaded_catalog.profiles["main"]
    assert loaded.connect_timeout_seconds == 7.5

    # T7: Desktop reads the snapshot and saves it back unchanged.
    shown = (await client.get("/admin/v1/models")).json()["data"]
    assert shown["document"]["profiles"]["main"]["connect_timeout_seconds"] == 7.5
    assert "connect_timeout_seconds" in shown["profile_schema"]["properties"]
    saved = await client.post(
        "/admin/v1/models/apply", json=command(shown["revision"], document=shown["document"])
    )
    assert saved.status_code == 200, saved.text
    path = api.commands.model.files._settings.model_profiles_file
    assert "connect_timeout_seconds = 7.5" in path.read_text(encoding="utf-8")
    assert api.commands.model.files.loaded_catalog.profiles["main"].connect_timeout_seconds == 7.5
    RESULTS["S6_admin_snapshot_round_trip"] = {
        "snapshot_value": shown["document"]["profiles"]["main"]["connect_timeout_seconds"],
        "kept_after_desktop_save": True,
    }


# --------------------------------------------------------------------------- S7-S8


@pytest.mark.parametrize("protocol", PROTOCOLS)
async def test_cut_reply_is_an_unknown_outcome_without_resend(protocol):
    upstream = _Upstream("cut", REPLIES[protocol])
    server, port = await _serve(upstream.handle)
    # T9: Responses keeps its connect/timeout-only retry rule even with retries left.
    # The other dialects keep their own existing rule; one attempt isolates the mapping.
    retries = 2 if protocol == "responses" else 0
    try:
        outcome, cause, _elapsed = await _complete(
            _profile(protocol, f"http://127.0.0.1:{port}/v1", max_retries=retries)
        )
    finally:
        server.close()
    # T8: the existing transport-unknown category, never a bare httpx exception.
    assert (outcome, cause) == ("LLMUnavailableError", "RemoteProtocolError")
    assert upstream.requests == 1
    RESULTS[f"S7_cut_reply_unknown[{protocol}]"] = {
        "error": outcome,
        "cause": cause,
        "max_retries": retries,
        "requests": upstream.requests,
    }


@pytest.mark.parametrize("native_requested", [False, True], ids=["plain", "native"])
async def test_work_cut_reply_follows_transport_unknown_receipts(
    database, tmp_path, native_requested
):
    upstream = _Upstream("cut", REPLIES["responses"])
    server, port = await _serve(upstream.handle)
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(repository, lease, "cut-reply", {}, validate)
    control.current = await repository.accept(
        lease, source_key="cut-reply", source=control.source, goal="audit sources"
    )
    harness = build_harness(database, make_settings(database.url), FakeLLMProvider())
    chat = harness.processor._chat
    profile = _profile(
        "responses",
        f"http://127.0.0.1:{port}/v1",
        search_mode=ModelSearchMode.NATIVE if native_requested else ModelSearchMode.EXTERNAL,
        capabilities=frozenset(
            {ModelCapability.TOOLS, ModelCapability.NATIVE_WEB_SEARCH, ModelCapability.REASONING}
        ),
    )
    pool = ModelClientPool(secret_overrides={KEY_ENV: "synthetic"})
    chat.runtime.runner._models = TaskModelExecutor(
        router=ModelRouter(
            ModelProfileCatalog(
                profiles={profile.id: profile},
                routes={task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask},
            )
        ),
        pool=pool,
    )
    backend = StubAgentBackend()
    backend.execute_call = AsyncMock(side_effect=AssertionError("no tool execution"))
    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        actor_is_superuser=False,
        conversation_key="cut-reply",
        current_group_id=None,
        bot_user_id="9999",
        gateway=None,
        runtime_config=await chat._runtime_config.snapshot(),
        allowed_capabilities=frozenset({"web"}),
        fixed_tools=(ChatTool("read_fixture", "read", {"type": "object"}),),
        work_control=control,
    )
    try:
        result = await chat.runtime.runner.run(
            (ChatMessage("system", "fixed"), ChatMessage("user", "audit sources")),
            runtime,
            backend,
        )
    finally:
        await pool.close()
        server.close()
    backend.execute_call.assert_not_awaited()
    persisted = await repository.get(control.current["id"])
    failure = result.outcome.failure
    async with database.read_session() as reader:
        phase = await reader.scalar(
            select(journal.c.phase).where(journal.c.work_id == persisted["id"])
        )
    # T9/T10: one dispatch, no transport resend, one reserved logical request.
    assert upstream.requests == 1 and persisted["model_requests"] == 1
    assert failure.diagnostics["physical_request_count"] == 1
    if native_requested:
        # Provider-native effects may have happened upstream: no automatic replay.
        assert (failure.code, failure.retryable) == ("LLMNativeToolError", False)
        assert result.work_state == persisted["state"] == "failed"
        assert phase == "dispatched"
        with pytest.raises(JournalUnavailable, match="work_response_not_persisted"):
            await control.session.journal.load(lease, persisted["id"], control.session.contract)
    else:
        # Same receipts as the existing read-timeout path: closed request, budget kept.
        assert (failure.code, failure.retryable) == ("LLMUnavailableError", True)
        assert result.work_state == persisted["state"] == "queued"
        assert phase == "paired"
    await repository.release(lease)
    RESULTS[f"S8_work_cut_reply[{'native' if native_requested else 'plain'}]"] = {
        "failure_code": failure.code,
        "retryable": failure.retryable,
        "work_state": persisted["state"],
        "journal_phase": phase,
        "upstream_requests": upstream.requests,
        "model_requests": persisted["model_requests"],
    }


# --------------------------------------------------------------------------- S9


class _RefusingProxy:
    """CONNECT proxy answering ``refusals`` statuses first, then tunnelling upstream."""

    def __init__(self, refusals: list[int], upstream_port: int) -> None:
        self.refusals = list(refusals)
        self.upstream_port = upstream_port
        self.connects: list[str] = []

    async def handle(self, reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            if self.refusals:
                status = self.refusals.pop(0)
                self.connects.append(str(status))
                writer.write(f"HTTP/1.1 {status} Refused\r\nContent-Length: 0\r\n\r\n".encode())
                await writer.drain()
                return
            self.connects.append("tunnel")
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            upstream_reader, upstream_writer = await asyncio.open_connection(
                "127.0.0.1", self.upstream_port
            )

            async def pipe(source, target):
                with contextlib.suppress(ConnectionError):
                    while data := await source.read(65536):
                        target.write(data)
                        await target.drain()
                target.close()

            await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


@pytest.mark.parametrize(
    "case,refusals",
    [("recovers", [502, 403]), ("exhausted", [502, 403, 502])],
    ids=["recovers", "exhausted"],
)
async def test_responses_retries_a_refused_proxy_connect(monkeypatch, tmp_path, case, refusals):
    ca = trustme.CA()
    ca_path = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(str(ca_path))
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert("model.test").configure_cert(context)
    upstream = _Upstream("slow", REPLIES["responses"])
    upstream_server = await asyncio.start_server(upstream.handle, "127.0.0.1", 0, ssl=context)
    proxy = _RefusingProxy(refusals, upstream_server.sockets[0].getsockname()[1])
    proxy_server, proxy_port = await _serve(proxy.handle)
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy_port}")
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_path))
    load_http_environment(tmp_path / "missing.env")
    try:
        # T11: two transport retries; each refusal is a request that never left the proxy.
        outcome, cause, _elapsed = await _complete(
            _profile("responses", "https://model.test/v1", max_retries=2)
        )
    finally:
        proxy_server.close()
        upstream_server.close()
    if case == "recovers":
        assert outcome == "ok:ok", (outcome, cause)
        assert proxy.connects == ["502", "403", "tunnel"] and upstream.requests == 1
    else:
        assert (outcome, cause) == ("LLMUnavailableError", "ProxyError")
        assert proxy.connects == ["502", "403", "502"] and upstream.requests == 0
    RESULTS[f"S9_proxy_refusal_retried[{case}]"] = {
        "outcome": outcome,
        "cause": cause,
        "proxy_connects": proxy.connects,
        "upstream_requests": upstream.requests,
    }
