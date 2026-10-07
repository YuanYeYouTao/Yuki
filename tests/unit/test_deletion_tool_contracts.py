"""Deletion contracts: typed results, preserved effects, exact artifact pages."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qq_ai_bot.capabilities.binding import InProcessToolBinding
from qq_ai_bot.capabilities.results import ToolExecutionResult, ToolResultBudgeter
from qq_ai_bot.tool_results.artifacts import _get_json
from qq_ai_bot.workspace.store import WorkspaceError, WorkspaceStore


@pytest.mark.asyncio
async def test_live_binding_rejects_string_boolean_envelopes():
    async def handler(_args, _context):
        return {"ok": "false", "mutation_committed": "false"}

    binding = InProcessToolBinding("core", "write", handler)
    with pytest.raises(TypeError, match="ToolExecutionResult"):
        await binding.invoke({}, SimpleNamespace())


@pytest.mark.asyncio
async def test_optional_archive_failure_preserves_committed_effect():
    class DiskFull:
        async def write_artifact(self, **_kwargs):
            raise OSError("disk full")

    result = ToolExecutionResult(ok=True, mutation_committed=True, data={"body": "x" * 10000})
    rendered = await ToolResultBudgeter(max_characters=1200, artifacts=DiskFull()).render(result)
    payload = json.loads(rendered.text)
    assert payload["ok"] is True and payload["mutation_committed"] is True
    assert payload["result_unavailable"] is True
    assert payload["artifact_error"] == "artifact_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        {str(i): '中文"\\' * 3 for i in range(1800)},
        ['中文"\\' * 3 for _ in range(1800)],
        '中文"\\' * 1800,
    ],
)
async def test_artifact_page_survives_small_item_budget(value):
    base = {"handle": "testhandle", "mode": "json", "logical_root": "data"}
    page = _get_json(value, path=(), offset=0, limit=5, base=base, max_characters=24000)
    result = ToolExecutionResult(
        ok=True,
        data=page,
        mutation_committed=False,
        provider_id="artifacts",
        tool_name="read_tool_artifact",
    )
    rendered = await ToolResultBudgeter(max_characters=24000, item_limit=5).render(result)
    payload = json.loads(rendered.text)
    assert payload["data"] == page
    assert "value" in page and page["next_offset"] == 5
    assert len(rendered.text) <= 24000
    assert len(json.dumps({"result": rendered.text}, ensure_ascii=False).encode()) <= 49152


def test_snapshot_replay_checks_bytes_and_name(tmp_path: Path):
    store = WorkspaceStore(tmp_path / "store")
    source = tmp_path / "input"
    source.write_bytes(b"first")
    identity = "cda8c9dc-2ea1-47fd-8c69-295acde050d0"
    with source.open("rb") as stream:
        original = store.snapshot(stream.fileno(), "file.txt", artifact_id=identity)
    with source.open("rb") as stream:
        assert store.snapshot(stream.fileno(), "file.txt", artifact_id=identity) == original
    with (
        source.open("rb") as stream,
        pytest.raises(WorkspaceError, match="snapshot_identity_conflict"),
    ):
        store.snapshot(stream.fileno(), "other.txt", artifact_id=identity)
    source.write_bytes(b"second")
    with (
        source.open("rb") as stream,
        pytest.raises(WorkspaceError, match="snapshot_identity_conflict"),
    ):
        store.snapshot(stream.fileno(), "file.txt", artifact_id=identity)
    assert store.read_bytes(identity)[1] == b"first"


def test_control_frontend_names_match_authoritative_bindings():
    import re

    from qq_ai_bot.control_plane.surface import _METHODS

    source = Path("frontend/src/control-methods.ts").read_text(encoding="utf-8")
    for kind in ("query", "command"):
        match = re.search(rf"export const {kind}Methods = (\[.*?\]) as const;", source, re.S)
        assert match is not None
        assert json.loads(re.sub(r",\s*\]$", "]", match.group(1))) == [
            item.name for item in _METHODS if item.kind == kind
        ]
    for item in _METHODS:
        owner = SimpleNamespace(**{item.name: object()})
        assert item.bind(owner) is getattr(owner, item.name)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["pause", "resume", "cancel"])
async def test_plugin_automation_management_passes_real_actor(action):
    from unittest.mock import AsyncMock

    from tests.unit.test_plugin_facades import invocation

    from qq_ai_bot.domain.tool_actor import ToolActor
    from qq_ai_bot.plugin_host.facades import HostPluginContext, PluginFacadeServices
    from yuki_plugin_sdk.errors import PluginPermissionError
    from yuki_plugin_sdk.permissions import PluginPermission

    service = SimpleNamespace(
        **{name: AsyncMock(return_value=True) for name in ("pause", "resume", "cancel")}
    )
    host = HostPluginContext(
        plugin_id="example.plugin",
        approved_permissions=(PluginPermission.AUTOMATION_MANAGE_SELF,),
        services=PluginFacadeServices(automation=service),
    )
    trusted = invocation()
    with host.bind(trusted):
        result = await getattr(host.automation, action)("7")
    assert result.ok
    getattr(service, action).assert_awaited_once_with(
        7, actor=ToolActor.from_inbound(trusted.inbound), conversation_key=trusted.conversation_key
    )
    denied = HostPluginContext(
        plugin_id="example.plugin",
        approved_permissions=(),
        services=PluginFacadeServices(automation=service),
    )
    with denied.bind(trusted), pytest.raises(PluginPermissionError):
        await getattr(denied.automation, action)("7")
    assert getattr(service, action).await_count == 1


@pytest.mark.asyncio
async def test_online_plugin_client_binds_revision_and_explicit_permissions():
    import httpx

    from qq_ai_bot.plugin_host.control_client import plugin_mutation

    calls = []

    async def handler(request):
        calls.append(request)
        if "queries" in request.url.path:
            return httpx.Response(200, json={"data": {"fields": {"revision": 123}}})
        payload = json.loads(request.content)
        assert payload["expected_revision"] == 123
        assert payload["payload"] == {
            "resource_id": "example",
            "action": "approve",
            "spec": {"permissions": ["message.current.read"]},
        }
        assert payload["request_id"] == request.headers["X-Request-ID"]
        return httpx.Response(200, json={"data": {"success": True}})

    async with httpx.AsyncClient(
        base_url="http://localhost", transport=httpx.MockTransport(handler)
    ) as client:
        assert (
            await plugin_mutation(
                client, "example", "approve", permissions=["message.current.read"]
            )
        )["success"]
    assert len(calls) == 2


def test_plugin_automation_uses_rebound_canonical_creator_not_old_account():
    from dataclasses import replace
    from datetime import UTC, datetime

    from qq_ai_bot.automation.authority import AuthorityContext, DelegatedAuthority, PermissionLevel
    from qq_ai_bot.automation.models import AutomationContext, TurnOrigin
    from qq_ai_bot.automation.registry import CapabilityExecutionContext
    from qq_ai_bot.plugin_host.automation_adapter import _automation_invocation

    grant = DelegatedAuthority(
        creator_user_id="old-account",
        bot_user_id="bot",
        created_from_message_id="historical",
        created_at="2026-10-08",
        permission_level=PermissionLevel.USER,
        granted_capabilities=("plugin.example.action",),
        capability_schema_versions={},
        canonical_creator_person_id="creator",
    )
    current = AuthorityContext(
        origin=TurnOrigin.SCHEDULED_AUTOMATION,
        actor_user_id="rebound-account",
        actor_person_id="creator",
        actor_is_superuser=False,
        bot_user_id="bot",
        delegated_authority=grant,
        allowed_capabilities=frozenset(grant.granted_capabilities),
    )
    now = datetime.now(UTC)
    context = CapabilityExecutionContext(
        authority=current,
        automation_id=1,
        automation_run_id=2,
        step_id="step",
        creator_user_id="old-account",
        bot_user_id="bot",
        current_group_id=None,
        scheduled_for=now,
        actual_started_at=now,
        local_time=now,
        timezone="UTC",
        automation_context=AutomationContext(),
        conversation_key="private:target",
        canonical_creator_person_id="creator",
        canonical_target_person_id="target",
        canonical_conversation_id="conversation",
    )
    projected = _automation_invocation("example", context)
    assert projected.actor_user_id == "rebound-account"
    assert projected.actor_person_id == "creator" and projected.person_id == "target"
    stolen = current.model_copy(
        update={"actor_user_id": "old-account", "actor_person_id": "new-owner"}
    )
    with pytest.raises(RuntimeError, match="creator"):
        _automation_invocation("example", replace(context, authority=stolen))


@pytest.mark.asyncio
async def test_attachment_snapshot_large_stream_replays_and_rejects_changed_source(
    tmp_path, monkeypatch
):
    from qq_ai_bot.workspace.service import WorkspaceService

    payload = b"a" * (4 * 1024 * 1024 + 1)
    source = tmp_path / "source.bin"
    source.write_bytes(payload)

    async def authorized_path(**arguments):
        return SimpleNamespace(), source

    calls = []

    async def checkout(name, arguments, *, request_id):
        calls.append((name, arguments, request_id))
        return {"path": arguments["name"]}

    store = WorkspaceStore(tmp_path / "snapshots")
    service = WorkspaceService(store)
    service.conversation_media = SimpleNamespace(authorized_path=authorized_path)
    service.sandbox = SimpleNamespace(execute=checkout)
    runtime = SimpleNamespace(
        effective_conversation_id="conversation", turn_snapshot=None, gateway=None
    )
    arguments = {"event_id": 1, "attachment_index": 0, "destination": "large.bin"}

    def no_full_read(*args, **kwargs):
        raise AssertionError("attachment snapshot must stream the descriptor")

    monkeypatch.setattr(Path, "read_bytes", no_full_read)
    first = await service.execute(
        "save_conversation_attachment_to_workspace",
        arguments,
        runtime=runtime,
        request_id="operation",
    )
    replay = await service.execute(
        "save_conversation_attachment_to_workspace",
        arguments,
        runtime=runtime,
        request_id="operation",
    )
    assert first == replay and first["immutable"] and first["file_imported"]
    assert first["size"] == len(payload) and len(store.list()["items"]) == 1
    assert calls[0] == calls[1] and calls[0][0] == "workspace_checkout"
    with pytest.raises(WorkspaceError, match="snapshot_identity_conflict"):
        await service.execute(
            "save_conversation_attachment_to_workspace",
            {**arguments, "event_id": 2},
            runtime=runtime,
            request_id="operation",
        )
    assert len(calls) == 2 and len(store.list()["items"]) == 1


@pytest.mark.asyncio
async def test_database_system_failure_does_not_become_control_conflict(database, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError
    from tests.unit.test_control_command_mutations import _command, _context, _principal

    from qq_ai_bot.control_plane import ControlCommandService
    from qq_ai_bot.domain.identity import PersonId
    from qq_ai_bot.persistence.control_command import ControlCommandAdapter

    adapter = ControlCommandAdapter(database)

    async def broken(*args, **kwargs):
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(adapter, "_load_receipt", broken)
    context = _context(_principal("identity.person.disable"), PersonId.new())
    with pytest.raises(SQLAlchemyError, match="database unavailable"):
        await ControlCommandService(adapter).disable_person(context, _command(context.request_id))


@pytest.mark.parametrize("operation", ["inspect", "get", "search"])
def test_too_small_artifact_page_is_explicit_not_false_eof(operation):
    from qq_ai_bot.tool_results.artifacts import _inspect_json, _search_json

    common = dict(path=(), offset=0, limit=5, base={"handle": "original"}, max_characters=100)
    if operation == "get":
        result = _get_json("x" * 10000, **common)
    elif operation == "inspect":
        result = _inspect_json({"x" * 1000: "needle"}, **common)
    else:
        result = _search_json({"x" * 1000: "needle"}, query="needle", **common)
    assert result["error_code"] == "artifact_budget_too_small"
    assert result["handle"] == "original" and result["next_offset"] == 0


@pytest.mark.asyncio
async def test_online_plugin_transport_authenticates_and_never_retries_unknown(monkeypatch):
    import httpx
    from tests.conftest import make_settings

    from qq_ai_bot.plugin_host.control_client import plugin_control, plugin_mutation

    monkeypatch.setenv("YUKI_CONTROL_CREDENTIAL", "test-credential")
    mutations = []

    async def handler(request):
        assert request.headers["Origin"] == "http://127.0.0.1:18765"
        if request.url.path.endswith("/login"):
            assert json.loads(request.content) == {"credential": "test-credential"}
            return httpx.Response(
                200, headers={"Set-Cookie": "yuki_session=fixture; Path=/"}, json={}
            )
        assert request.headers["Cookie"] == "yuki_session=fixture"
        if request.url.path.endswith("/session"):
            return httpx.Response(200, json={"csrf": "trusted-csrf"})
        assert request.headers["X-Yuki-CSRF"] == "trusted-csrf"
        if "/queries/" in request.url.path:
            return httpx.Response(200, json={"data": {"fields": {"revision": 12}}})
        mutations.append(request)
        raise httpx.ReadTimeout("lost response", request=request)

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: factory(**kwargs, transport=httpx.MockTransport(handler)),
    )
    async with plugin_control(make_settings("sqlite+aiosqlite:///:memory:")) as client:
        with pytest.raises(RuntimeError, match="query original request"):
            await plugin_mutation(client, "example", "enable")
    assert len(mutations) == 1


@pytest.mark.parametrize(
    "field", ["ok", "retryable", "uncertain", "mutation_committed", "finalize_after_commit"]
)
def test_typed_result_rejects_string_booleans(field):
    with pytest.raises(TypeError, match="bool"):
        ToolExecutionResult(**{"ok": True, field: "false"})


@pytest.mark.asyncio
@pytest.mark.parametrize("executed", [True, False])
async def test_coordinator_execution_budget_uses_typed_fact_not_display(executed):
    from tests.support.agent_backend import StubAgentBackend

    from qq_ai_bot.capabilities.coordinator import ToolInvocationCoordinator
    from qq_ai_bot.domain.messages import ToolCall, ToolFunction
    from qq_ai_bot.runtime.effect_outcomes import current_result_capture

    async def execute(invocation):
        capture = current_result_capture.get()
        assert capture is not None
        capture.outcome = ToolExecutionResult(
            ok=executed,
            data={"executed": executed},
            mutation_committed=executed,
            provider_id="core",
            tool_name=invocation.call.function.name,
        )
        return json.dumps({"ok": not executed, "executed": not executed})

    backend = StubAgentBackend(execute_call=execute)
    call = ToolCall("original", ToolFunction("send_message", "{}"))
    result = await ToolInvocationCoordinator().execute_batch(
        (call,),
        backend,
        SimpleNamespace(work_control=None),
        remaining_calls=1,
        max_parallel_calls=1,
    )
    assert result.calls[0][2] is executed
    assert result.executed_count == int(executed)
    assert result.evidence[call.id]["executed"] is executed


@pytest.mark.asyncio
@pytest.mark.parametrize("executed", [True, False])
async def test_replayed_original_outcome_controls_budget_without_redispatch(
    database, tmp_path, executed
):
    from tests.support.agent_backend import StubAgentBackend
    from tests.unit.test_tool_effect_audit import active_work

    from qq_ai_bot.capabilities.coordinator import ToolInvocationCoordinator
    from qq_ai_bot.domain.messages import ToolCall, ToolFunction
    from qq_ai_bot.runtime.effect_outcomes import current_result_capture

    _, work, _ = await active_work(database, tmp_path)
    calls = []

    async def execute(invocation):
        calls.append(invocation.identity.operation_id)
        capture = current_result_capture.get()
        assert capture is not None
        capture.outcome = ToolExecutionResult(
            ok=executed,
            data={"executed": executed},
            mutation_committed=executed,
            provider_id="core",
            tool_name=invocation.call.function.name,
        )
        return json.dumps({"ok": not executed, "executed": not executed})

    backend = StubAgentBackend(execute_call=execute)
    call = ToolCall("original", ToolFunction("send_message", "{}"))
    for _ in range(2):
        result = await ToolInvocationCoordinator().execute_batch(
            (call,),
            backend,
            SimpleNamespace(work_control=work.control),
            remaining_calls=1,
            max_parallel_calls=1,
        )
        assert result.calls[0][2] is executed
        assert result.executed_count == int(executed)
        assert result.evidence[call.id]["executed"] is executed
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_host_predispatch_rejection_needs_no_display_fact():
    from unittest.mock import AsyncMock

    from tests.support.agent_backend import StubAgentBackend

    from qq_ai_bot.capabilities.coordinator import ToolInvocationCoordinator
    from qq_ai_bot.domain.messages import ToolCall, ToolFunction

    execute = AsyncMock()
    reject = AsyncMock(return_value="host refused before dispatch")
    result = await ToolInvocationCoordinator().execute_batch(
        (ToolCall("original", ToolFunction("send_message", "{}")),),
        StubAgentBackend(execute_call=execute),
        SimpleNamespace(work_control=None),
        remaining_calls=1,
        max_parallel_calls=1,
        before_execute=reject,
    )
    execute.assert_not_awaited()
    assert result.executed_count == 0 and result.calls[0][2] is False
