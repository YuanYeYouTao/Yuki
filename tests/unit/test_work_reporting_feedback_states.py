"""Reporting opportunities use paired results and bounded delivery witnesses."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import event, insert, update
from tests.support.work_session import WorkSession
from tests.unit.test_work_communication import append_input, control_env
from tests.unit.test_work_reporting_runner import START, case, response, run, tool

from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.runtime.subagent_repository import SubagentRepository
from qq_ai_bot.runtime.subagent_schema import children
from qq_ai_bot.runtime.work_schema_v1 import effects, work
from qq_ai_bot.services.turn_transcript import TurnTranscript
from qq_ai_bot.services.work_reporting import append_input_feedback, stage_feedback_opportunity


async def report(control, key, event_id, status, *, actual_target=None):
    target = await control.communication_target()
    await control.repository.prepare_effect(control.lease, control.current["id"], key, "tool")
    await control.repository.record_effect(
        key,
        "unknown" if status == "unknown" else "accepted",
        {
            "result": "large-saved-body-" + "x" * 50000,
            "outcome": {
                "tool": "send_message",
                "work_report": {"kind": "reply", "reply_to_event_ids": [event_id]},
                "report_target": target,
                "delivery_target": actual_target or (target if status == "succeeded" else None),
                "delivered_message": status == "succeeded",
                "status": status,
                "uncertain": status == "unknown",
                "ok": status == "succeeded",
            },
        },
    )


async def test_tool_only_stage_is_nonblocking_and_does_not_add_model_requests(database, tmp_path):
    test_case = await case(
        database,
        tmp_path,
        [
            response(tool("send_message", START)),
            response(tool("read_fixture")),
            response(tool("write_fixture")),
            response(tool("task_control", {"action": "complete"})),
        ],
    )
    result = await run(test_case)
    assert result.work_state == "completed"
    assert len(test_case.provider.requests) == 4
    assert test_case.observed == ["send_message", "read_fixture", "write_fixture"]
    assert any(
        "上一段工具结果仍是内部资料" in (message.content or "")
        for message in test_case.provider.requests[2].messages
    )


@pytest.mark.parametrize("action,changed", [("result", False), ("result", True), ("status", False)])
async def test_actual_child_result_can_offer_stage_but_status_poll_does_not(
    database, tmp_path, action, changed
):
    opportunity = action == "result"
    test_case = await case(database, tmp_path, [])
    child_id = await SubagentRepository(test_case.repository).start(
        test_case.control.lease,
        test_case.control.current["id"],
        "existing-child",
        {"goal": "inspect evidence", "output_kind": "answer"},
    )
    async with database.immediate_session() as writer:
        await writer.execute(update(work).where(work.c.id == child_id).values(state="completed"))
        await writer.execute(
            update(children)
            .where(children.c.work_id == child_id)
            .values(result_json=json.dumps({"answer": "specific evidence"}))
        )
    scripted = iter(
        [
            response(tool("send_message", START)),
            response(tool("subagent_control", {"action": action, "child_id": child_id})),
            response(tool("subagent_control", {"action": action, "child_id": child_id}, "repeat")),
            response(tool("write_fixture")),
            response(tool("task_control", {"action": "complete"})),
        ]
    )
    test_case.provider._responder = lambda _: next(scripted)
    original_complete = test_case.provider.complete

    async def complete(request):
        if changed and len(test_case.provider.requests) == 2:
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(children)
                    .where(children.c.work_id == child_id)
                    .values(result_json=json.dumps({"answer": "additional verified evidence"}))
                )
        return await original_complete(request)

    test_case.provider.complete = complete
    result = await run(test_case)
    assert result.work_state == "completed" and len(test_case.provider.requests) == 5
    assert (
        any(
            "上一段工具结果仍是内部资料" in (message.content or "")
            for message in test_case.provider.requests[2].messages
        )
        is opportunity
    )
    assert sum(
        "上一段工具结果仍是内部资料" in (message.content or "")
        for message in test_case.provider.requests[3].messages
    ) == int(opportunity) + int(changed)


@pytest.mark.parametrize(
    "statuses",
    [
        ("failed",),
        ("unknown",),
        ("failed", "succeeded"),
        ("unknown", "succeeded"),
        ("failed", "unknown"),
    ],
)
async def test_input_opportunity_distinguishes_failure_unknown_and_later_delivery(
    database, tmp_path, statuses
):
    env, control = await control_env(database, tmp_path, reporting="quiet")
    input_id, event_id = await append_input(env, control, "question")
    await control.repository.stage(control.lease, [input_id], "shown")
    await control.repository.consume(control.lease, "shown")
    for number, status in enumerate(statuses):
        await report(control, f"reply:{number}", event_id, status)
    session = control.session = WorkSession(control, "reporting")
    transcript = await session.restore(TurnTranscript((ChatMessage("user", "original task"),)))
    before = len(transcript.request().messages)
    watermark = await append_input_feedback(control, transcript, 0)
    new_messages = transcript.request().messages[before:]
    rendered = "\n".join(message.content or "" for message in new_messages)
    assert watermark == input_id
    if "succeeded" in statuses:
        assert not new_messages
        witnesses = await control.communication_reports(event_ids=(event_id,))
        assert [item["effect_key"] for item in witnesses] == ["reply:1"]
    elif "unknown" in statuses:
        assert "送达未确认" in rendered and "不能盲目重发" in rendered
        assert "原 Work 新输入的答复机会" not in rendered
    else:
        assert "原 Work 新输入的答复机会" in rendered and "确定失败回执" in rendered
    # Producing a reminder is read-only; the existing dispatched journal owns
    # its cursor. A later activation never turns it into a delivered reply.
    assert control.communication["input_feedback_through_id"] == 0
    await session.save("dispatched", communication_updates={"input_feedback_through_id": watermark})
    assert control.communication["input_feedback_through_id"] == input_id
    before = len(transcript.request().messages)
    assert await append_input_feedback(control, transcript, 0) == input_id
    assert len(transcript.request().messages) == before


async def test_claimed_report_target_cannot_replace_actual_delivery_target(database, tmp_path):
    env, control = await control_env(database, tmp_path)
    input_id, event_id = await append_input(env, control, "question")
    await control.repository.stage(control.lease, [input_id], "shown")
    await report(
        control, "reply:0", event_id, "succeeded", actual_target={"kind": "person", "id": "other"}
    )
    assert not await control.communication_reports(event_ids=(event_id,))
    assert not await control.communication_reports(event_ids=(event_id,), delivered_only=True)
    await report(control, "reply:1", event_id, "succeeded")
    assert [
        row["effect_key"] for row in await control.communication_reports(event_ids=(event_id,))
    ] == ["reply:1"]


async def test_delivery_target_extra_fields_do_not_hide_confirmed_reply(database, tmp_path):
    env, control = await control_env(database, tmp_path)
    input_id, event_id = await append_input(env, control, "question")
    await control.repository.stage(control.lease, [input_id], "shown")
    target = await control.communication_target()
    await report(
        control, "reply:0", event_id, "succeeded", actual_target={**target, "receipt": "extra"}
    )
    rows = await control.communication_reports(event_ids=(event_id,), delivered_only=True)
    assert len(rows) == 1 and rows[0]["delivery_target"]["receipt"] == "extra"
    transcript = TurnTranscript((ChatMessage("user", "original task"),))
    assert await append_input_feedback(control, transcript, 0) == input_id
    assert len(transcript.request().messages) == 1


async def test_large_current_batch_checks_later_bind_page_without_a_new_tool_limit(
    database, tmp_path
):
    _, control = await control_env(database, tmp_path)
    await report(control, "last-report", control.source["trigger_event_id"], "succeeded")
    keys = (*(f"no-report:{number}" for number in range(128)), "last-report")
    rows = await control.communication_reports(effect_keys=keys, delivered_only=True)
    assert [row["effect_key"] for row in rows] == ["last-report"]


async def test_exact_current_batch_report_query_projects_metadata_and_uses_index(
    database, tmp_path
):
    _, control = await control_env(database, tmp_path, reporting="interactive")
    await report(control, "known-report", control.source["trigger_event_id"], "succeeded")
    target = await control.communication_target()
    async with database.immediate_session() as writer:
        await writer.execute(
            insert(effects),
            [
                {
                    "effect_key": f"unrelated:{number:04d}",
                    "work_id": control.current["id"],
                    "kind": "tool",
                    "state": "accepted",
                    "receipt_json": json.dumps({"result": "large-saved-body-" + "x" * 50000}),
                    "created": 0,
                    "updated": 0,
                }
                for number in range(64)
            ],
        )
    captured = []

    def capture(_connection, _cursor, sql, parameters, *_args):
        captured.append((sql, parameters))

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        rows = await control.communication_reports(
            effect_keys=("known-report",), delivered_only=True
        )
        replies = await control.communication_reports(
            event_ids=(control.source["trigger_event_id"],)
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert len(rows) == 1 and rows[0]["delivery_target"] == target
    assert len(replies) == 1 and replies[0]["effect_key"] == "known-report"
    assert "large-saved-body" not in json.dumps(rows)
    assert len(captured) == 6  # each query: target, lease, one witness
    assert all(sql.lstrip().upper().startswith("SELECT") for sql, _ in captured)
    queries = [(sql, p) for sql, p in captured if "FROM runtime_work_effects" in sql]
    assert len(queries) == 2 and "effect_key IN" in queries[0][0]
    assert "json_each" in queries[1][0]
    assert all("LIMIT" in sql for sql, _ in queries)

    def explain():
        with sqlite3.connect(database.url.removeprefix("sqlite+aiosqlite:///")) as connection:
            return [
                (
                    [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql, parameters)],
                    connection.execute(sql, parameters).fetchall(),
                )
                for sql, parameters in queries
            ]

    for plan, actual in await asyncio.to_thread(explain):
        assert len(actual) == 1
        assert any("SEARCH runtime_work_effects USING INDEX" in line for line in plan), plan
        assert not any("SCAN runtime_work_effects" in line for line in plan), plan
        assert not any("TEMP B-TREE" in line for line in plan), plan
        assert not any(isinstance(item, str) and "large-saved-body" in item for item in actual[0])


@pytest.mark.parametrize("later_status", ["succeeded", "unknown"])
@pytest.mark.parametrize("early_witness", [False, True])
async def test_input_witness_does_not_sort_same_work_history(
    database, tmp_path, later_status, early_witness
):
    _, control = await control_env(database, tmp_path)
    event_id = control.source["trigger_event_id"]
    prefix = "0" if early_witness else "z"
    await report(control, f"{prefix}-failed", event_id, "failed")
    await report(control, f"{prefix}-later", event_id, later_status)

    async def inspect():
        captured = []

        def capture(_connection, _cursor, sql, parameters, *_args):
            if "FROM runtime_work_effects" in sql:
                captured.append((sql, parameters))

        event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
        try:
            rows = await control.communication_reports(event_ids=(event_id,))
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
        assert [row["effect_key"] for row in rows] == [f"{prefix}-later"]

        def measure():
            samples = []
            with sqlite3.connect(database.url.removeprefix("sqlite+aiosqlite:///")) as connection:
                for sql, parameters in captured:
                    assert "ORDER BY" not in sql and "LIMIT" in sql
                    plan = [
                        row[3]
                        for row in connection.execute("EXPLAIN QUERY PLAN " + sql, parameters)
                    ]
                    assert not any("TEMP B-TREE" in line for line in plan), plan
                    assert any("SEARCH runtime_work_effects USING INDEX" in line for line in plan)
                    steps = 0

                    def tick():
                        nonlocal steps
                        steps += 1
                        return 0

                    connection.set_progress_handler(tick, 1)
                    connection.execute(sql, parameters).fetchall()
                    connection.set_progress_handler(None, 0)
                    samples.append(steps)
            return samples

        return await asyncio.to_thread(measure)

    baseline = await inspect()
    async with database.immediate_session() as writer:
        await writer.execute(
            insert(effects),
            [
                {
                    "effect_key": f"a-unrelated:{number:04d}",
                    "work_id": control.current["id"],
                    "kind": "tool",
                    "state": "accepted",
                    "receipt_json": json.dumps({"result": "x" * 5000}),
                    "created": 0,
                    "updated": 0,
                }
                for number in range(512)
            ],
        )
    enlarged = await inspect()
    assert len(enlarged) == (2 if later_status == "succeeded" else 3)
    # Existing indexes can stop at an early match, but a late/absent match
    # still examines the scoped Work range. Neither case sorts receipts or
    # transfers their saved bodies; VM growth is bounded by one range per query.
    assert all(
        0 <= after - before < 512 * 40 for before, after in zip(baseline, enlarged, strict=True)
    )
    if early_witness:
        assert enlarged[0] == baseline[0] and enlarged[-1] == baseline[-1]
        if later_status == "succeeded":
            assert enlarged == baseline
    else:
        assert enlarged[0] > baseline[0]
    if later_status == "unknown":
        assert enlarged[1] > baseline[1]  # absent success must be checked


async def test_reporting_reads_complete_while_another_sqlite_writer_is_held(database, tmp_path):
    env, control = await control_env(database, tmp_path, reporting="interactive")
    input_id, event_id = await append_input(env, control, "question")
    await control.repository.stage(control.lease, [input_id], "shown")
    await report(control, "reply:0", event_id, "failed")
    control.session = SimpleNamespace(transcript=SimpleNamespace(chain_id="current-chain"))
    observation = {
        "sequence": 1,
        "content": "",
        "results": [{"name": "terminal_read", "call_id": "read", "executed": True, "output": "{}"}],
    }
    transcript = TurnTranscript((ChatMessage("user", "original work"),))
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work).where(work.c.id == control.current["id"]).values(reason="held")
        )
        stage = await asyncio.wait_for(stage_feedback_opportunity(control, observation), 2)
        assert stage is not None
        watermark = await asyncio.wait_for(append_input_feedback(control, transcript, 0), 2)
        assert watermark == input_id
    assert control.communication["input_feedback_through_id"] == 0
    assert "确定失败回执" in transcript.request().messages[-1].content
