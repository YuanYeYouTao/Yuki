"""Main delivery boundaries before their product fixes.

Failure expectations: an indivisible numeric leaf cannot escape B; an array
item with an over-limit nested collection cannot be passed off as a complete
allowed item; real Artifact capacity refusal preserves known bounded content
and the original accepted effect; multi-part sends keep each original status
and internal event anchor when long prepared/delivered bodies are omitted.
The partial-send variant loses the second transport confirmation and must not
repeat either part. These scenarios use real Main bindings, Work and SQLite.
"""

import hashlib
import json
import shutil

import pytest
from sqlalchemy import select, update
from tests.support.automation_unified_delivery_helpers import setup_run
from tests.support.event_seed import seed_scoped_event

from yuki.capabilities.models import CapabilityTrustSource
from yuki.capabilities.provider import InProcessToolProvider
from yuki.capabilities.results import ToolExecutionResult, ToolResultBudgeter
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.conversations import ConversationScope
from yuki.domain.messages import ChatResponse, ChatTool, ToolCall, ToolFunction
from yuki.execution_trace.db_models import ExecutionTraceStateModel
from yuki.persistence.models import ToolArtifactModel
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_schema_v1 import effects
from yuki.social.db_models import SocialOperationModel
from yuki.tool_results.artifacts import ToolArtifactRepository


def _response(name, arguments, call_id):
    return ChatResponse(
        "", 0, tool_calls=(ToolCall(call_id, ToolFunction(name, json.dumps(arguments))),)
    )


def _objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


async def _original_receipt(database, control, tool, raw):
    async with database.read_session() as reader:
        rows = list(
            (
                await reader.execute(
                    select(effects).where(effects.c.work_id == control.current["id"])
                )
            ).mappings()
        )
    row = next(
        row
        for row in rows
        if json.loads(row["receipt_json"]).get("outcome", {}).get("tool") == tool
    )
    assert row["state"] == "accepted"
    assert await control.session.journal.effect_result(row["effect_key"]) == raw
    return dict(row)


async def _save_originals(database, store_root, evidence_dir, evidence):
    async with database.read_session() as reader:
        rows = list((await reader.scalars(select(ToolArtifactModel))).all())
        evidence["execution_receipts"] = [
            dict(row) for row in (await reader.execute(select(effects))).mappings()
        ]
    evidence["artifacts"] = []
    for row in rows:
        destination = evidence_dir / row.relative_path
        shutil.copyfile(store_root / row.relative_path, destination)
        content = destination.read_bytes()
        evidence["artifacts"].append(
            {
                "handle": row.handle_id,
                "file": destination.name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
            }
        )


@pytest.mark.parametrize("invalidation", ["privacy", "generation"])
@pytest.mark.parametrize("failure", ["capacity", "io_error", "inline", "disabled"])
async def test_nonwork_storage_fallback_cannot_publish_invalidated_original_sources(
    database,
    tmp_path,
    monkeypatch,
    tool_delivery_evidence,
    invalidation,
    failure,
):
    # Failure expectation before the owner recheck: there is no accepted Work
    # writer to enforce source/privacy fences; storage fails before its normal
    # publication writer. The fallback must not turn old content into inline
    # success, and neither failure permits replaying the original read.
    evidence_dir, evidence = tool_delivery_evidence
    case = await setup_run(database, tmp_path, mode="silent", work_enabled=False)
    case.settings.agent_tool_result_max_characters = 8000
    case.settings.tooling_result_token_budget = None
    case.settings.tooling_result_artifact_enabled = failure != "disabled"
    marker = "INVALIDATED-ORIGINAL-BODY "
    seeded = await seed_scoped_event(
        case.env.service.writer,
        scope=ConversationScope.group("80001", "20001"),
        origin_key="fallback-original",
        sender_user_id="10001",
        direction="inbound",
        content=marker + "z" * (200 if failure == "inline" else 40000),
    )
    store_root = tmp_path / "invalidated-artifacts"
    store = ToolArtifactRepository(
        database,
        store_root,
        retention_seconds=600,
        **({"max_artifact_bytes": 1} if failure == "capacity" else {}),
    )
    case.chat._tool_artifacts = store
    original_write = store.write_artifact
    attempts = []

    async def invalidate():
        async with database.immediate_session() as writer:
            if invalidation == "privacy":
                state = await writer.get(ExecutionTraceStateModel, 1)
                if state is None:
                    writer.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
                else:
                    state.privacy_generation += 1
            else:
                await writer.execute(
                    update(CanonicalConversationModel)
                    .where(CanonicalConversationModel.id == case.env.context.conversation_id)
                    .values(generation=CanonicalConversationModel.generation + 1)
                )

    async def late_write(**kwargs):
        attempts.append(
            {
                "source_event_ids": kwargs.get("source_event_ids"),
                "source_privacy_generation": kwargs.get("source_privacy_generation"),
            }
        )
        await invalidate()
        return await original_write(**kwargs)

    def io_failure(*args):
        raise OSError("offline publication failure before the source writer")

    if failure in {"inline", "disabled"}:
        original_render = ToolResultBudgeter.render

        async def late_render(budgeter, result):
            rendered = await original_render(budgeter, result)
            if result.tool_name == "query_chat_history":
                attempts.append(
                    {
                        "source_event_ids": result.source_event_ids,
                        "source_privacy_generation": result.source_privacy_generation,
                    }
                )
                await invalidate()
            return rendered

        monkeypatch.setattr(ToolResultBudgeter, "render", late_render)
    else:
        monkeypatch.setattr(store, "write_artifact", late_write)
        if failure == "io_error":
            monkeypatch.setattr(store, "_publish", io_failure)
    observed = []

    def respond(request):
        if len(case.provider.requests) == 1:
            return _response(
                "query_chat_history",
                {"view": "search", "keyword": marker.strip()},
                "original-invalidated-read",
            )
        observed.extend(
            message.content
            for message in request.messages
            if message.tool_call_id == "original-invalidated-read"
        )
        return "done"

    case.provider._responder = respond
    evidence.update(
        scenario="E05 nonwork fallback",
        invalidation=invalidation,
        failure=failure,
        source_event_id=seeded.event.id,
        model_results=observed,
        storage_attempts=attempts,
        effective_budget=8000,
    )
    try:
        case.clock.advance(2)
        run = await case.dispatch()
        evidence["run_status"] = run.status.value
        assert attempts and attempts[0]["source_privacy_generation"] == 0
        assert attempts[0]["source_event_ids"] == (seeded.event.id,)
        assert not any(marker in content for content in observed)
        assert len(attempts) == 1
    finally:
        await _save_originals(database, store_root, evidence_dir, evidence)


@pytest.mark.parametrize(
    "boundary", ["numeric_leaf", "nested_item_limit", "capacity", "json_key", "disabled"]
)
async def test_main_keeps_bounded_content_and_original_effect_at_result_boundaries(
    database,
    tmp_path,
    monkeypatch,
    tool_delivery_evidence,
    boundary,
):
    evidence_dir, evidence = tool_delivery_evidence
    budget = 450 if boundary == "numeric_leaf" else 1200
    data = (
        10**1000
        if boundary == "numeric_leaf"
        else [{"rows": [1, 2, 3]}]
        if boundary == "nested_item_limit"
        else {1: [{"body": "x" * 4000}]}
        if boundary == "json_key"
        else {
            "content": "KNOWN-CAPACITY-BODY " + "x" * 4000,
            "operation_id": "original-boundary-effect",
            "status": "succeeded",
        }
    )
    case = await setup_run(database, tmp_path, mode="silent")
    case.settings.agent_tool_result_max_characters = budget
    case.settings.tooling_result_token_budget = None
    case.settings.tooling_result_artifact_enabled = boundary != "disabled"
    case.settings.tooling_result_item_limit = 1 if boundary == "nested_item_limit" else None
    store_root = tmp_path / "boundary-artifacts"
    case.chat._tool_artifacts = ToolArtifactRepository(
        database,
        store_root,
        retention_seconds=600,
        **({"max_artifact_bytes": 1} if boundary == "capacity" else {}),
    )
    executions = []

    async def execute(name, arguments, runtime):
        executions.append({"name": name, "arguments": arguments})
        return ToolExecutionResult(
            ok=True, data=data, mutation_committed=boundary in {"capacity", "disabled"}
        )

    original_registry = case.chat._build_tool_registry

    def registry(runtime):
        actual = original_registry(runtime)
        actual.register(
            InProcessToolProvider(
                provider_id="boundary-fixture",
                source=CapabilityTrustSource.PLUGIN,
                definitions=lambda _: (
                    ChatTool(
                        "boundary_result",
                        "Return this scenario's original domain value",
                        {"type": "object", "properties": {}, "additionalProperties": False},
                    ),
                ),
                execute=execute,
                plugin_read_only=lambda _: boundary not in {"capacity", "disabled"},
            )
        )
        return actual

    monkeypatch.setattr(case.chat, "_build_tool_registry", registry)
    observed = []
    read_pages = []
    control = None

    def respond(request):
        nonlocal control
        count = len(case.provider.requests)
        if count == 1:
            return _response("task_control", {"action": "accept", "goal": boundary}, "accept")
        if count == 2:
            return _response("boundary_result", {}, "original-boundary-call")
        message = next(m for m in request.messages if m.tool_call_id == "original-boundary-call")
        observed.append(message.content)
        control = current_work_control.get()
        if boundary == "json_key":
            if count == 3:
                view = json.loads(message.content)
                followup = next(
                    item for item in view["read_more"] if item["path"] == ["1", 0, "body"]
                )
                return _response("read_tool_artifact", followup, "normalized-key-read")
            read_pages.append(
                json.loads(
                    next(
                        m.content
                        for m in request.messages
                        if m.tool_call_id == "normalized-key-read"
                    )
                )
            )
        return _response("task_control", {"action": "complete", "result": "checked"}, "finish")

    case.provider._responder = respond
    evidence.update(
        scenario="E01/E05",
        boundary=boundary,
        effective_budget=budget,
        original_data=data,
        executions=executions,
        model_results=observed,
        read_pages=read_pages,
    )
    try:
        case.clock.advance(2)
        run = await case.dispatch()
        evidence["run_status"] = run.status.value
        assert observed, run
        view = json.loads(observed[0])
        assert view["ok"] is True and view["truncated"] is True
        assert len(observed[0]) <= budget
        if boundary == "numeric_leaf":
            assert view.get("data") != data
            assert view["artifact_handle"]
        elif boundary == "nested_item_limit":
            # Main data contains only complete array items. A separately marked
            # readable child preview must not pretend that this item fit.
            assert view["data"] == []
            assert view.get("preview"), view
            assert any(part["path"][:1] == [0] for part in view["read_more"])
        elif boundary == "json_key":
            assert view["preview"]["path"] == ["1", 0]
            assert view["preview"]["value"]["body"]
            assert read_pages[0]["ok"] is True
            assert read_pages[0]["data"]["path"] == ["1", 0, "body"]
            assert read_pages[0]["data"]["value"]
            assert set(read_pages[0]["data"]["value"]) == {"x"}
        else:
            assert "KNOWN-CAPACITY-BODY" in observed[0]
            assert view["mutation_committed"] is True
            assert view["operation_id"] == "original-boundary-effect"
            assert not view.get("result_unavailable") and not view.get("artifact_handle")
            if boundary == "capacity":
                assert view.get("artifact_error")
            else:
                async with database.read_session() as reader:
                    assert (await reader.scalars(select(ToolArtifactModel))).all() == []
        assert len(executions) == 1
        evidence["original_receipt"] = await _original_receipt(
            database, control, "boundary_result", observed[0]
        )
        assert len(executions) == 1
    finally:
        await _save_originals(database, store_root, evidence_dir, evidence)


@pytest.mark.parametrize("lose_second_confirmation", [False, True])
async def test_main_send_preview_keeps_each_real_part_identity_and_status(
    database,
    tmp_path,
    monkeypatch,
    tool_delivery_evidence,
    lose_second_confirmation,
):
    evidence_dir, evidence = tool_delivery_evidence
    case = await setup_run(database, tmp_path, mode="silent")
    case.settings.agent_tool_result_max_characters = 1400
    case.settings.tooling_result_token_budget = None
    case.settings.reply_hard_max_messages = 2
    store_root = tmp_path / "send-artifacts"
    case.chat._tool_artifacts = ToolArtifactRepository(database, store_root, retention_seconds=600)
    original_api = case.env.bot.call_api
    attempts = []

    async def transport(action, **arguments):
        if action == "send_group_msg":
            attempts.append(action)
            if lose_second_confirmation and len(attempts) == 2:
                case.env.bot.calls.append((action, arguments))
                raise TimeoutError("offline second-part confirmation lost")
        return await original_api(action, **arguments)

    monkeypatch.setattr(case.env.bot, "call_api", transport)
    observed = []
    control = None

    def respond(request):
        nonlocal control
        count = len(case.provider.requests)
        if count == 1:
            return _response(
                "task_control", {"action": "accept", "goal": "original parts"}, "accept"
            )
        if count == 2:
            return _response(
                "send", {"text": "FIRST " + "a" * 1500 + "\nSECOND " + "b" * 1500}, "original-send"
            )
        message = next(m for m in request.messages if m.tool_call_id == "original-send")
        observed.append(message.content)
        control = current_work_control.get()
        return _response("task_control", {"action": "complete", "result": "checked"}, "finish")

    case.provider._responder = respond
    evidence.update(
        scenario="E05",
        boundary="send_parts",
        effective_budget=1400,
        lose_second_confirmation=lose_second_confirmation,
        model_results=observed,
        transport_attempts=attempts,
    )
    try:
        case.clock.advance(2)
        run = await case.dispatch()
        evidence["run_status"] = run.status.value
        assert observed, run
        view = json.loads(observed[0])
        assert view["truncated"] is True
        async with database.read_session() as reader:
            parts = list(
                (
                    await reader.scalars(
                        select(SocialOperationModel).where(
                            SocialOperationModel.action == "send_message"
                        )
                    )
                ).all()
            )
        expected = [
            {"operation_id": part.id, "status": part.status, "event_id": part.event_id}
            for part in parts
        ]
        evidence["actual_parts"] = expected
        assert len(attempts) == len(parts) == 2
        assert sum(part["status"] == "succeeded" for part in expected) == (
            1 if lose_second_confirmation else 2
        )
        if lose_second_confirmation:
            assert view["uncertain"] is True
        objects = list(_objects(view))
        for fact in expected:
            assert any(
                all(item.get(key) == value for key, value in fact.items()) for item in objects
            ), (fact, view)
        evidence["original_receipt"] = await _original_receipt(
            database, control, "send", observed[0]
        )
        assert len(attempts) == 2
    finally:
        await _save_originals(database, store_root, evidence_dir, evidence)
