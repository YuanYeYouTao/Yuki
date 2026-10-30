"""Main -> native Code -> real business backend -> Artifact -> public reader.

Before implementation, E03 covers these failure boundaries: a program value
between B/2 and the final B must stay inline; token overrides both above and
below the agent budget must reach CodeHost; a genuinely oversized value must
have an accurate program path in its complete parent Artifact; disabled storage,
I/O failure or actual capacity refusal must not invent a reference; accepted results preserve
the exact receipt and must not dispatch its child again. The model is scripted
at its boundary; all internal execution, source checks and storage are real.
"""

import hashlib
import json
import shutil

import pytest
from sqlalchemy import select
from tests.support.automation_unified_delivery_helpers import setup_run
from tests.support.codemode_cases import requires_worker, worker
from tests.support.event_seed import seed_scoped_event

from yuki.automation.models import RunStatus
from yuki.domain.conversations import ConversationScope
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.persistence.models import ToolArtifactModel
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_schema_v1 import effects
from yuki.services.main_agent_contract import MainAgentContract
from yuki.tool_results.artifacts import ToolArtifactRepository
from yuki.workspace.short_state import ShortState

pytestmark = [requires_worker, pytest.mark.native_worker]


@pytest.mark.parametrize("overflow_vm_bytes", [False, True])
async def test_native_child_keeps_readable_preview_and_exact_read_parameters(
    database,
    tmp_path,
    tool_delivery_evidence,
    overflow_vm_bytes,
):
    # Failure expectation before the projection change: the real Main binding
    # produced a useful top-level preview/read_more, but receipt_view discarded
    # both when its primary data contained only complete (here empty) items.
    # A UTF-8 preview beyond the real 256-KiB VM cap must keep its reference
    # and identity as incomplete, without passing optional oversized fields
    # into the binding or enlarging that independent resource cap.
    evidence_dir, evidence = tool_delivery_evidence
    case = await setup_run(database, tmp_path, mode="silent")
    pinned = worker()
    case.settings.code_mode_enabled = True
    case.settings.code_mode_worker_path = pinned.binary_path
    case.settings.code_mode_worker_sha256 = pinned.sha256
    case.settings.code_mode_launcher_path = pinned.launcher_path
    case.settings.code_mode_launcher_sha256 = pinned.launcher_sha256
    case.settings.agent_tool_result_max_characters = 100000 if overflow_vm_bytes else 8000
    case.settings.tooling_result_token_budget = None
    case.settings.context_compact_trigger_tokens = 500000
    case.chat.runtime.runner.main_contract = MainAgentContract(
        case.chat, ShortState(case.env.store), code_enabled=True
    )
    case.chat.runtime.runner.code_mode_settings = case.settings
    root = tmp_path / "child-preview-artifacts"
    case.chat._tool_artifacts = ToolArtifactRepository(database, root, retention_seconds=600)
    original = "CODE_CHILD_BODY " + ("中" * 120000 if overflow_vm_bytes else "z" * 40000)
    seeded = await seed_scoped_event(
        case.env.service.writer,
        scope=ConversationScope.group("80001", "20001"),
        origin_key="code-child-preview",
        sender_user_id="10001",
        direction="inbound",
        content=original,
    )
    code = """
r = await yuki_query_chat_history({'view': 'search', 'keyword': 'CODE_CHILD_BODY'})
p = r.get('preview')
text = p['value'].get('content', '') if p else ''
{'complete': r['complete'], 'empty_events': r['data']['events'] == [],
 'has_preview': p is not None, 'preview_path': p['path'] if p else None,
 'preview_excerpt': text[:40], 'preview_characters': len(text),
 'read_more': r.get('read_more'), 'child_ref': r['result_ref']}
"""
    if overflow_vm_bytes:
        code = """
r = await yuki_query_chat_history({'view': 'search', 'keyword': 'CODE_CHILD_BODY'})
{'complete': r['complete'], 'data_omitted': r['data'] is None,
 'has_preview': 'preview' in r, 'has_read_more': 'read_more' in r,
 'operation_id': r['operation_id'], 'child_ref': r['result_ref']}
"""
    stage = 0
    child_view = None
    pages = []
    parts = []
    pending = None
    seen = set()

    def respond(request):
        nonlocal stage, child_view, pending
        for message in request.messages:
            if message.role != "tool" or message.tool_call_id in seen:
                continue
            seen.add(message.tool_call_id)
            if message.tool_call_id == "native-preview":
                body = json.loads(message.content)
                child_view = body["result"]
                if not overflow_vm_bytes:
                    assert child_view["has_preview"], child_view
                    pending = next(
                        item for item in child_view["read_more"] if item["path"][-1:] == ["content"]
                    )
            elif (message.tool_call_id or "").startswith("child-page-"):
                body = json.loads(message.content)
                pages.append(body)
                assert body["ok"], body
                page = body["data"]
                assert page["offset"] == pending["offset"]
                parts.append(page["value"])
                pending = (
                    None
                    if page["next_offset"] is None
                    else {**pending, "offset": page["next_offset"]}
                )
        if stage == 0:
            stage = 1
            return _child_response(
                "task_control", {"action": "accept", "goal": "child preview"}, "accept"
            )
        if stage == 1:
            stage = 2
            return _child_response("execute_code", {"code": code}, "native-preview")
        if pending is not None:
            return _child_response("read_tool_artifact", pending, f"child-page-{len(pages)}")
        return _child_response(
            "task_control", {"action": "complete", "result": "checked"}, "finish"
        )

    case.provider._responder = respond
    evidence.update(
        scenario="E03 child preview",
        program=code,
        source_event_id=seeded.event.id,
        model_boundary="scripted ChatRequest; real native worker",
        pages=pages,
        overflow_vm_bytes=overflow_vm_bytes,
    )
    try:
        case.clock.advance(2)
        run = await case.dispatch()
        assert run.status is RunStatus.SUCCEEDED, run
        assert child_view["complete"] is False
        if overflow_vm_bytes:
            assert child_view["data_omitted"]
            assert not child_view["has_preview"] and not child_view["has_read_more"]
            assert child_view["operation_id"] and child_view["child_ref"]
        else:
            assert child_view["has_preview"] and child_view["empty_events"]
            assert child_view["preview_path"] == ["events", 0]
            assert child_view["preview_excerpt"] == original[:40]
            assert child_view["preview_characters"] > 0
            assert "".join(parts) == original[child_view["preview_characters"] :]
            assert all(page["data"]["handle"] == child_view["child_ref"] for page in pages)
    finally:
        evidence["vm_child_projection"] = child_view
        async with database.read_session() as reader:
            rows = list((await reader.scalars(select(ToolArtifactModel))).all())
        evidence["artifacts"] = []
        for row in rows:
            destination = evidence_dir / row.relative_path
            shutil.copyfile(root / row.relative_path, destination)
            evidence["artifacts"].append(
                {
                    "handle": row.handle_id,
                    "file": destination.name,
                    "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                }
            )


def _child_response(name, arguments, call_id):
    return ChatResponse(
        "", 0, tool_calls=(ToolCall(call_id, ToolFunction(name, json.dumps(arguments))),)
    )


@pytest.mark.parametrize(
    "agent_budget,token_budget,repetitions,storage",
    [
        (4000, 4000, 4500, "enabled"),
        (24000, 2000, 3500, "enabled"),
        (4000, 4000, 18000, "enabled"),
        (4000, 4000, 18000, "disabled"),
        (4000, 4000, 18000, "io_error"),
        (4000, 4000, 18000, "capacity"),
    ],
)
async def test_main_code_delivers_program_value_and_reads_its_original_parent(
    database,
    tmp_path,
    tool_delivery_evidence,
    monkeypatch,
    agent_budget,
    token_budget,
    repetitions,
    storage,
):
    evidence_dir, evidence = tool_delivery_evidence
    effective_budget = token_budget * 4
    original = "中\n" * repetitions + "PROGRAM_VALUE_TAIL"
    source_text = "code-delivery-source original event"
    expected = {"source": source_text, "content": original}
    artifacts_enabled = storage != "disabled"
    original_available = storage == "enabled"
    oversized = len(json.dumps(expected, ensure_ascii=False)) > effective_budget
    case = await setup_run(database, tmp_path, mode="silent")
    pinned = worker()
    settings = case.settings
    settings.code_mode_enabled = True
    settings.code_mode_worker_path = pinned.binary_path
    settings.code_mode_worker_sha256 = pinned.sha256
    settings.code_mode_launcher_path = pinned.launcher_path
    settings.code_mode_launcher_sha256 = pinned.launcher_sha256
    settings.agent_tool_result_max_characters = agent_budget
    settings.tooling_result_token_budget = token_budget
    settings.tooling_result_artifact_enabled = artifacts_enabled
    settings.context_compact_trigger_tokens = 500000
    case.chat.runtime.runner.main_contract = MainAgentContract(
        case.chat, ShortState(case.env.store), code_enabled=True
    )
    case.chat.runtime.runner.code_mode_settings = settings
    artifact_root = tmp_path / "code-delivery-artifacts"
    case.chat._tool_artifacts = ToolArtifactRepository(
        database,
        artifact_root,
        retention_seconds=600,
        **({"max_artifact_bytes": 1} if storage == "capacity" else {}),
    )
    if storage == "io_error":

        async def failed_write(**kwargs):
            raise OSError("offline injected storage failure")

        monkeypatch.setattr(case.chat._tool_artifacts, "write_artifact", failed_write)
    seeded = await seed_scoped_event(
        case.env.service.writer,
        scope=ConversationScope.group("80001", "20001"),
        origin_key="code-delivery-source",
        sender_user_id="10001",
        direction="inbound",
        content=source_text,
    )
    script = (
        "r = await yuki_query_chat_history({'view': 'search', "
        "'keyword': 'code-delivery-source'})\n"
        f"{{'source': r['data']['events'][0]['content'], "
        f"'content': '中\\n' * {repetitions} + 'PROGRAM_VALUE_TAIL'}}"
    )
    seen = set()
    stage = 0
    code_result = None
    code_raw = None
    control = None
    pages = []
    parts = []
    next_offset = 0

    def response(name, arguments, identity):
        return ChatResponse(
            "", 0, tool_calls=(ToolCall(identity, ToolFunction(name, json.dumps(arguments))),)
        )

    def respond(request):
        nonlocal stage, code_result, code_raw, control, next_offset
        for message in request.messages:
            if message.role != "tool" or message.tool_call_id in seen:
                continue
            seen.add(message.tool_call_id)
            if message.tool_call_id == "original-code-delivery":
                code_raw = message.content
                code_result = json.loads(code_raw)
                control = current_work_control.get()
            elif (message.tool_call_id or "").startswith("program-page-"):
                decoded = json.loads(message.content)
                pages.append(decoded)
                assert decoded["ok"], decoded
                page = decoded["data"]
                assert page["offset"] == next_offset
                parts.append(page["value"])
                next_offset = page["next_offset"]
        if stage == 0:
            stage = 1
            return response(
                "task_control",
                {"action": "accept", "goal": "Code delivery"},
                "accept-code-delivery",
            )
        if stage == 1:
            stage = 2
            return response("execute_code", {"code": script}, "original-code-delivery")
        assert code_result is not None
        if oversized and original_available and next_offset is not None:
            return response(
                "read_tool_artifact",
                {
                    "handle": code_result["result_ref"],
                    "operation": "get",
                    "path": [*code_result["result_path"], "content"],
                    "offset": next_offset,
                },
                f"program-page-{len(pages)}",
            )
        return response(
            "task_control", {"action": "complete", "result": "checked"}, "complete-code-delivery"
        )

    case.provider._responder = respond
    evidence.update(
        scenario="E03",
        model_boundary="scripted ChatRequest; not Provider wire",
        agent_budget=agent_budget,
        token_budget=token_budget,
        effective_budget=effective_budget,
        artifacts_enabled=artifacts_enabled,
        storage=storage,
        program=script,
        source_event_id=seeded.event.id,
        expected_value=expected,
        pages=pages,
    )
    try:
        case.clock.advance(2)
        run = await case.dispatch()
        evidence["run_status"] = run.status.value
        assert run.status is RunStatus.SUCCEEDED, run
        assert code_result["status"] == "completed", code_result
        assert len(code_raw) <= effective_budget
        if not oversized:
            assert code_result["result"] == expected
            assert code_result["complete"] is True
            assert not code_result.get("result_ref") and not code_result.get("summary_ref")
        else:
            assert code_result["complete"] is False and code_result["truncated"] is True
            assert code_result["result_preview"]
            if original_available:
                assert code_result["result_ref"] == code_result["summary_ref"]
                assert code_result["result_path"] == ["result"]
                assert code_result["read_more"] == [
                    {
                        "handle": code_result["result_ref"],
                        "operation": "get",
                        "path": ["result"],
                        "offset": 0,
                    }
                ]
                assert "".join(parts) == original
            else:
                assert not code_result.get("result_ref") and not code_result.get("summary_ref")
                assert pages == []
                if storage in {"io_error", "capacity"}:
                    assert code_result["artifact_error"] == "artifact_storage_failed"
        assert control is not None and control.session is not None
        async with database.read_session() as reader:
            rows = list(
                (
                    await reader.execute(
                        select(effects).where(effects.c.work_id == control.current["id"])
                    )
                ).mappings()
            )
        parent = next(row for row in rows if row["kind"] == "code_composition")
        children = [
            row
            for row in rows
            if json.loads(row["receipt_json"]).get("outcome", {}).get("tool")
            == "query_chat_history"
        ]
        assert len(children) == 1
        restored = await control.session.journal.effect_result(parent["effect_key"])
        assert restored == code_raw
        evidence["restored_receipt"] = restored
        evidence["execution_receipts"] = [dict(row) for row in rows]
        assert not [name for name, _ in case.env.bot.calls if name.startswith("send_")]
    finally:
        evidence["code_result"] = code_result
        evidence["host_tool_messages"] = [
            {"call_id": message.tool_call_id, "content": message.content}
            for request in case.provider.requests
            for message in request.messages
            if message.role == "tool"
        ]
        async with database.read_session() as reader:
            originals = list((await reader.scalars(select(ToolArtifactModel))).all())
        evidence["artifacts"] = []
        for artifact in originals:
            source = artifact_root / artifact.relative_path
            destination = evidence_dir / source.name
            shutil.copyfile(source, destination)
            content = destination.read_bytes()
            evidence["artifacts"].append(
                {
                    "handle": artifact.handle_id,
                    "file": destination.name,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "bytes": len(content),
                    "source_metadata": json.loads(artifact.access_json),
                }
            )
