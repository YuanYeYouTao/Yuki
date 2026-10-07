"""Safe schema feedback and send admission facts through the current backend."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select
from tests.conftest import build_harness, make_settings
from tests.unit.test_capability_runtime_security import _descriptor, _entry, _runtime
from tests.unit.test_tool_effect_audit import active_work

from qq_ai_bot.capabilities import (
    CapabilityTrustSource,
    InProcessToolProvider,
    ToolProviderRegistry,
)
from qq_ai_bot.capabilities.coordinator import ToolInvocationCoordinator
from qq_ai_bot.capabilities.invocation import direct_invocations
from qq_ai_bot.capabilities.validation import JsonSchemaCapabilityValidator
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    ChatTool,
    InboundMessage,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_schema_v1 import effects
from qq_ai_bot.services.main_agent_backend import MainAgentBackend, UnsentFinalResponseError
from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository


@pytest.mark.parametrize(
    "schema,arguments,expected",
    [
        (
            {"properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
            {"secret_unknown_name": "private-value"},
            'required fields: ["run_id"]',
        ),
        (
            {"properties": {"run_id": {"type": "string"}}},
            {"run_id": 12345},
            'run_id: type; expected type: "string"',
        ),
        (
            {"properties": {"action": {"enum": ["get", "cancel"]}}},
            {"action": "private-value"},
            'action: enum; allowed values: ["get", "cancel"]',
        ),
        (
            {"properties": {}, "additionalProperties": False},
            {"secret_unknown_name": "private-value"},
            "undeclared fields are not allowed",
        ),
        (
            {
                "additionalProperties": {
                    "type": "object",
                    "properties": {"action": {"enum": ["get", "cancel"]}},
                }
            },
            {"secret_unknown_name": {"action": "private-value"}},
            "*.action: enum",
        ),
        (
            {
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {"type": "object", "properties": {"run_id": {"type": "string"}}},
                    }
                }
            },
            {"tasks": [{"run_id": 12345}]},
            "tasks.[].run_id: type",
        ),
    ],
)
def test_schema_feedback_contains_declared_constraints_without_instance_data(
    schema, arguments, expected
):
    validator = JsonSchemaCapabilityValidator()
    schema = {"type": "object", **schema}
    assert (
        validator.admit(
            (_entry(_descriptor("fixture", namespace="workspace.read", schema=schema)),)
        )
        == ()
    )
    result = validator.validate("fixture", json.dumps(arguments))
    assert not result.ok and expected in result.detail
    assert "private-value" not in result.detail
    assert "secret_unknown_name" not in result.detail
    assert "12345" not in result.detail
    assert len(result.detail) <= 1024


def test_local_ref_and_alternative_feedback_preserves_schema_paths_and_hides_values():
    validator = JsonSchemaCapabilityValidator()
    schema = {
        "type": "object",
        "$defs": {
            "request": {
                "type": "object",
                "properties": {"mode": {"enum": ["get", "cancel"]}, "run_id": {"type": "string"}},
                "anyOf": [
                    {"required": ["run_id"]},
                    {"properties": {"mode": {"const": "get"}}, "required": ["mode"]},
                ],
            }
        },
        "properties": {"request": {"$ref": "#/$defs/request"}},
    }
    assert (
        validator.admit(
            (_entry(_descriptor("fixture", namespace="workspace.read", schema=schema)),)
        )
        == ()
    )
    result = validator.validate(
        "fixture", '{"request":{"mode":"cancel","unknown-secret":"private-value"}}'
    )
    assert not result.ok and "request" in result.detail and "run_id" in result.detail
    assert "private-value" not in result.detail and "unknown-secret" not in result.detail
    assert len(result.detail) <= 1024


async def backend_case(database, tmp_path, origin, receipt, *, tool_name="send_message"):
    env, work, tool_runtime = await active_work(database, tmp_path)
    chat = build_harness(database, make_settings(database.url)).processor._chat
    inbound = (
        InboundMessage(
            message_id="feedback-fixture",
            event_type="message:test",
            scope_type=ScopeType.GROUP,
            sender=SenderIdentity("10001"),
            text="fixture",
            bot_user_id="80001",
            group_id="20001",
            person_id=env.person,
            space_id=env.space,
            conversation_id=env.context.conversation_id,
        )
        if origin is TurnOrigin.USER_MESSAGE
        else None
    )
    tool_runtime = replace(
        tool_runtime,
        inbound=inbound,
        origin=origin,
        space_id=env.space,
        runtime_config=await chat._runtime_config.snapshot(),
    )
    dispatches = []

    async def dispatch(name, arguments, runtime):
        dispatches.append((name, arguments))
        return receipt

    registry = ToolProviderRegistry()
    registry.register(
        InProcessToolProvider(
            provider_id="core",
            source=CapabilityTrustSource.CORE,
            definitions=lambda _: (
                ChatTool(
                    tool_name,
                    "send",
                    {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                ),
            ),
            execute=dispatch,
        )
    )
    backend = MainAgentBackend(chat, tool_runtime)
    backend._catalog = registry.catalog(tool_runtime)
    backend._callable_tool_names = {tool_name}
    backend._capability_runtime = _runtime(*backend._catalog.entries)
    backend._capability_runtime.initial_exposure()
    # The experiment binds typed Host Invocations rather than main's mutable
    # begin_batch/execute fixture. Preserve all admission/usage/source assertions.
    runtime = SimpleNamespace(work_control=None, origin=origin, delegated_authority=None)
    return backend, runtime, work, dispatches


@pytest.mark.asyncio
async def test_schema_rejection_keeps_call_receipt_and_attempt_budget_without_execution(
    database, tmp_path
):
    backend, runtime, work, dispatches = await backend_case(
        database, tmp_path, TurnOrigin.USER_MESSAGE, {}
    )
    runtime.work_control = work.control
    call = ToolCall(
        "original-rejected-call", ToolFunction("send_message", '{"unknown-secret":"private-value"}')
    )
    result = await ToolInvocationCoordinator().execute_batch(
        (call,), backend, runtime, remaining_calls=1, max_parallel_calls=1
    )
    assert result.calls[0][0].id == call.id and result.calls[0][2] is False
    receipt = json.loads(result.calls[0][1])
    assert receipt["executed"] is False and receipt["mutation_committed"] is False
    assert "text" in receipt["detail"] and "private-value" not in receipt["detail"]
    assert result.executed_count == 0 and work.control.tools_started == 1
    assert dispatches == []
    async with database.sessions() as session:
        row = (
            (
                await session.execute(
                    select(effects).where(effects.c.effect_key == work.call_key(call.id))
                )
            )
            .mappings()
            .one()
        )
    fact = json.loads(row["receipt_json"])["outcome"]
    assert row["state"] == "accepted" and fact["ok"] is False and fact["executed"] is False
    assert fact["mutation_committed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("projection", ["short", "minimal", "artifact"])
async def test_new_input_race_before_binding_keeps_typed_nonexecution_and_original_attempt(
    database, tmp_path, monkeypatch, projection
):
    backend, runtime, work, dispatches = await backend_case(
        database, tmp_path, TurnOrigin.USER_MESSAGE, {}, tool_name="terminal_exec"
    )
    runtime.work_control = work.control
    config = backend._runtime.runtime_config
    assert config is not None and config.tooling is not None
    backend._runtime = replace(
        backend._runtime,
        before_model_request=AsyncMock(),
        runtime_config=replace(
            config,
            agent=replace(
                config.agent, tool_result_max_characters=4096 if projection == "short" else 64
            ),
            tooling=replace(
                config.tooling,
                result_token_budget=None,
                result_artifact_enabled=projection == "artifact",
            ),
        ),
    )
    if projection == "artifact":
        backend._service._tool_artifacts = ToolArtifactRepository(
            database, tmp_path / "race-artifacts", retention_seconds=60
        )
    # The first two checks admit the call (WorkSession, then backend). Input
    # arrives before the binding's last guard, after the original attempt charge.
    pending = AsyncMock(side_effect=[[], [], [{"id": 1}]])
    monkeypatch.setattr(WorkControl, "pending", pending)
    entry = backend._catalog.by_model_name("terminal_exec")
    assert entry is not None and entry.descriptor.binding is not None
    invoke = AsyncMock()
    monkeypatch.setattr(type(entry.descriptor.binding), "invoke", invoke)
    call = ToolCall("original-race-call", ToolFunction("terminal_exec", '{"text":"fixture"}'))
    token = current_work_control.set(work.control)
    try:
        result = await ToolInvocationCoordinator().execute_batch(
            (call,), backend, runtime, remaining_calls=1, max_parallel_calls=1
        )
    finally:
        current_work_control.reset(token)
    assert pending.await_count == 3
    invoke.assert_not_awaited()
    assert dispatches == []
    backend._runtime.before_model_request.assert_awaited_once()
    assert result.calls[0][0].id == call.id
    assert result.calls[0][2] is False and result.executed_count == 0
    assert work.control.tools_started == 1
    receipt = json.loads(result.calls[0][1])
    assert receipt["executed"] is False and receipt["mutation_committed"] is False
    assert receipt["error_code"] == "new_input_before_execution"
    assert bool(receipt.get("truncated")) == (projection != "short")
    assert bool(receipt.get("artifact_handle")) == (projection == "artifact")
    async with database.sessions() as session:
        row = (
            (
                await session.execute(
                    select(effects).where(effects.c.effect_key == work.call_key(call.id))
                )
            )
            .mappings()
            .one()
        )
    durable = json.loads(row["receipt_json"])
    fact = durable["outcome"]
    assert row["state"] == "accepted" and fact["executed"] is False
    assert fact["ok"] is False and fact["mutation_committed"] is False
    assert fact["error_code"] == "new_input_before_execution"
    assert bool(durable.get("artifact_handle")) == (projection == "artifact")
    restored = await work.control.repository.get(work.control.current["id"])
    assert restored is not None and restored["tool_calls"] == 1
    assert restored["model_requests"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [TurnOrigin.USER_MESSAGE, TurnOrigin.SELF_INITIATIVE])
async def test_unadmitted_send_does_not_consume_unsent_final_opportunity(
    database, tmp_path, origin
):
    backend, runtime, _work, dispatches = await backend_case(database, tmp_path, origin, {})
    call = ToolCall("unadmitted", ToolFunction("send_message", "{}"))
    statements = []

    def capture(_connection, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        result = await backend.execute_call(direct_invocations((call,), runtime)[0])
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert json.loads(result)["executed"] is False
    assert statements == [] and dispatches == []
    assert backend.response_feedback("Internal answer", runtime)
    with pytest.raises(UnsentFinalResponseError):
        backend.response_feedback("Another internal answer", runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [TurnOrigin.USER_MESSAGE, TurnOrigin.SELF_INITIATIVE])
@pytest.mark.parametrize("status", ["succeeded", "failed", "unknown", "not_executed"])
async def test_actual_send_receipt_controls_correction_without_retrying_failed_or_unknown(
    database, tmp_path, origin, status
):
    receipt = {"ok": status == "succeeded", "data": {"status": status}}
    if status == "not_executed":
        receipt["data"]["executed"] = False
    if status == "unknown":
        receipt["uncertain"] = True
    backend, runtime, _work, dispatches = await backend_case(database, tmp_path, origin, receipt)
    call = ToolCall("actual-attempt", ToolFunction("send_message", '{"text":"fixture"}'))
    await backend.execute_call(direct_invocations((call,), runtime)[0])
    assert len(dispatches) == 1
    assert bool(backend.response_feedback("Internal answer", runtime)) == (status == "not_executed")
    assert backend.response_feedback("NO_REPLY", runtime) is None
    assert len(dispatches) == 1


@pytest.mark.asyncio
async def test_tool_budget_rejection_does_not_mark_send_attempted(database, tmp_path):
    backend, runtime, _work, dispatches = await backend_case(
        database, tmp_path, TurnOrigin.USER_MESSAGE, {}
    )
    call = ToolCall("budget-denied", ToolFunction("send_message", '{"text":"fixture"}'))
    result = await ToolInvocationCoordinator().execute_batch(
        (call,), backend, runtime, remaining_calls=0, max_parallel_calls=1
    )
    assert result.executed_count == 0 and json.loads(result.calls[0][1])["executed"] is False
    assert dispatches == [] and backend.response_feedback("Internal answer", runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["succeeded", "failed", "unknown"])
async def test_later_schema_rejection_cannot_erase_an_earlier_send_attempt(
    database, tmp_path, status
):
    receipt = {"ok": status == "succeeded", "data": {"status": status}}
    if status == "unknown":
        receipt["uncertain"] = True
    backend, runtime, _work, dispatches = await backend_case(
        database, tmp_path, TurnOrigin.USER_MESSAGE, receipt
    )
    first = ToolCall("first", ToolFunction("send_message", '{"text":"fixture"}'))
    await backend.execute_call(direct_invocations((first,), runtime)[0])
    denied = ToolCall("second", ToolFunction("send_message", "{}"))
    assert (
        json.loads(await backend.execute_call(direct_invocations((denied,), runtime)[0]))[
            "executed"
        ]
        is False
    )
    assert backend.response_feedback("Internal answer", runtime) is None
    assert len(dispatches) == 1
