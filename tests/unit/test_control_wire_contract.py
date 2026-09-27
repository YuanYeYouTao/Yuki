"""Future adapters share strict envelopes, discoverable methods and safe DTO output."""

import inspect
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from tests.unit.test_control_plane_foundation import context

from qq_ai_bot.control_plane import (
    ControlCommandError,
    ControlCommandService,
    ControlQueryError,
    ControlQueryService,
    OperationRef,
    OperationStatus,
    Page,
    Problem,
    ProblemCode,
    StateEpoch,
)
from qq_ai_bot.control_plane.surface import describe_surface
from qq_ai_bot.control_plane.wire import control_response, decode_command, decode_page


def test_surface_covers_actual_methods_and_permission_projection_without_granting():
    ctx = context("control.plugin.read", "control.operation.read")
    surface = describe_surface(ctx.principal)
    for kind, service in (("query", ControlQueryService), ("command", ControlCommandService)):
        actual = {
            name
            for name, method in inspect.getmembers(service, inspect.iscoroutinefunction)
            if not name.startswith("_")
        }
        projected = {item.name for item in surface.methods if item.kind == kind}
        assert actual == projected
    assert {
        item.capability for item in surface.methods if item.authorized
    } == ctx.principal.granted_capabilities
    assert all(not item.authorized for item in surface.methods if item.kind == "command")
    assert "identity.person.forget" not in {item.capability for item in surface.methods}
    with pytest.raises(ControlQueryError) as exc:
        describe_surface(replace(ctx.principal, authenticated=False))
    assert exc.value.problem.code is ProblemCode.UNAUTHENTICATED


def test_native_json_envelope_keeps_ids_times_unknown_state_and_correlation():
    ctx = context("control.operation.read")
    stamp = datetime(2026, 9, 27, tzinfo=UTC)
    operation = OperationRef(
        operation_id=f"control:{ctx.principal.principal_id.text}:{ctx.request_id.text}",
        status=OperationStatus.UNKNOWN,
        progress=None,
        state_epoch=StateEpoch.V2,
        error_category="effect_unknown",
        created_at=stamp,
        updated_at=stamp,
    )
    encoded = control_response(ctx.request_id, Page([operation], snapshot_at=stamp))
    wire = json.loads(json.dumps(encoded))
    assert wire["request_id"] == ctx.request_id.text
    assert wire["data"]["snapshot_at"] == "2026-09-27T00:00:00Z"
    assert wire["data"]["items"][0]["status"] == "unknown"
    assert wire["data"]["items"][0]["progress"] is None
    assert wire["problem"] is None
    denied = control_response(ctx.request_id, Problem(ProblemCode.CAPABILITY_DENIED))
    assert json.loads(json.dumps(denied))["problem"]["code"] == "capability_denied"
    for raw in (ctx.principal, {"api_key": "secret"}, RuntimeError("private secret")):
        with pytest.raises(TypeError):
            control_response(ctx.request_id, raw)


@pytest.mark.parametrize(
    "body",
    [
        {"offset": 20},
        {"limit": True},
        {"limit": 0},
        {"limit": 101},
        {"cursor": 1},
    ],
)
def test_page_envelope_is_bounded_and_has_no_offset(body):
    with pytest.raises(ControlQueryError):
        decode_page(body)


def test_command_envelope_rejects_client_principal_and_revision_coercion():
    ctx = context("control.config.mutate")
    body = {"request_id": ctx.request_id.text, "expected_revision": 0, "payload": {"action": "set"}}
    decoded = decode_command(body)
    assert decoded.request_id == ctx.request_id
    for invalid in (
        {**body, "principal": "root"},
        {**body, "expected_revision": True},
        {**body, "request_id": "platform-message-1"},
    ):
        with pytest.raises(ControlCommandError):
            decode_command(invalid)
