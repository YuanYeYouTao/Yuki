"""Diagnostic parent/child projection is private, read-only and never a receipt owner."""

import json

import pytest
from tests.support.codemode_cases import effect_rows, environment, requires_worker, run_code
from tests.unit.test_control_plane_foundation import context
from tests.unit.test_execution_trace import decoded, rows

from qq_ai_bot.control_plane import ControlQueryError, ControlQueryService, PageRequest
from qq_ai_bot.control_plane.query_types import ExecutionTraceFilter
from qq_ai_bot.execution_trace.recorder import TraceRecorder, trace_span
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.runtime.work_activation import current_work_control

pytestmark = requires_worker
PRIVATE = "private-domain-result-must-not-be-mirrored-in-trace"


@pytest.mark.parametrize("scenario", ["normal", "diagnostic_loss", "unknown"])
async def test_code_trace_preserves_parentage_without_controlling_effects(
    database, tmp_path, monkeypatch, scenario
):
    env = await environment(database, tmp_path)
    env.domain.replies["lookup"] = {"ok": True, "data": {"text": PRIVATE}}
    if scenario == "unknown":
        env.domain.replies["send_message"] = {
            "ok": False,
            "uncertain": True,
            "error": "lost_response",
        }
    recorder = TraceRecorder(database)
    if scenario == "diagnostic_loss":

        async def lost(*_args):
            raise OSError("offline diagnostic writer unavailable")

        monkeypatch.setattr(recorder, "_insert", lost)
    token = current_work_control.set(env.control)
    try:
        async with trace_span(
            "turn", {}, recorder=recorder, conversation_id=env.control.lease.conversation_id
        ):
            body, outer = await run_code(
                env,
                "await yuki_send_message({'text': 'one'})\n"
                "await yuki_lookup({})\n"
                "await yuki_send_message({'text': 'two'})",
            )
    finally:
        current_work_control.reset(token)
    expected = 1 if scenario == "unknown" else 3
    assert len(env.domain.log) == expected
    before = await effect_rows(database, env.control.current["id"])
    assert before[1:] == (expected, expected)
    assert body["status"] == ("partial" if scenario == "unknown" else "completed")
    if scenario == "diagnostic_loss":
        assert recorder.record_failures > 0
        assert not await rows(database)
        return
    assert recorder.record_failures == 0
    evidence = await rows(database)
    parents = [r for r in evidence if r.kind == "code_composition_start"]
    children = [r for r in evidence if r.kind == "code_child_start"]
    assert len(parents) == 1 and len(children) == expected
    parent = parents[0]
    assert decoded(parent)["data"]["operation_id"] == outer.identity.operation_id
    assert all(r.parent_operation_id == parent.operation_id for r in children)
    for ordinal, row in enumerate(children):
        data = decoded(row)["data"]
        assert data["parent_effect_key"] == outer.identity.operation_id
        assert data["operation_id"] == f"{outer.identity.operation_id}/c{ordinal}"
        assert data["child_ordinal"] == ordinal
        assert row.work_id == env.control.current["id"]
    assert PRIVATE not in json.dumps([decoded(row) for row in evidence])

    # The existing control reader returns hierarchy metadata with its original
    # authorization. Refresh/paging has no invocation or resume service attached.
    service = ControlQueryService(ControlQueryAdapter(database))
    metadata = context("control.execution.metadata.read")
    scope = ExecutionTraceFilter(work_id=env.control.current["id"])
    page = await service.list_execution_trace(metadata, PageRequest(limit=100), scope=scope)
    assert len(page.items) == len(evidence)
    assert all(item.payload is None for item in page.items)
    assert any(item.parent_operation_id == parent.operation_id for item in page.items)
    with pytest.raises(ControlQueryError):
        await service.read_execution_trace(metadata, parent.id)
    content = context("control.execution.metadata.read", "control.execution.content.read")
    read = await service.read_execution_trace(content, parent.id)
    assert read.payload["data"]["operation_id"] == outer.identity.operation_id
    assert await effect_rows(database, env.control.current["id"]) == before
    assert len(env.domain.log) == expected
