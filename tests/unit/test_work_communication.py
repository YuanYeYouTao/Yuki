"""Communication associations use original inputs and durable transport evidence."""

import json
from dataclasses import replace

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from tests.support.social_identity_cases import social_env

from qq_ai_bot.capabilities.results import normalize_legacy_result
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatMessage, ToolCall, ToolFunction
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.effect_outcomes import current_result_capture
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import inputs, journal
from qq_ai_bot.runtime.work_session import WorkSession
from qq_ai_bot.runtime.work_wait import WorkWaitRepository
from qq_ai_bot.services.turn_transcript import TurnTranscript


async def control_env(database, tmp_path, *, reporting=None, output_kind="answer"):
    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)
    async with database.sessions() as reader:
        trigger = await reader.scalar(select(ChatEventModel.id))
    source = {
        "origin": "user_message",
        "actor_user_id": "10001",
        "actor_person_id": env.person,
        "trigger_event_id": trigger,
        "conversation_id": lease.conversation_id,
        "generation": 1,
        "bot_user_id": "80001",
    }

    async def validate():
        assert await repo.valid(lease)

    control = WorkControl(repo, lease, "communication", source, validate)
    result = json.loads(
        await control.execute(
            "task_control",
            {
                "action": "accept",
                "goal": "investigate the original question",
                "output_kind": output_kind,
                **({"reporting": reporting} if reporting else {}),
            },
            "accept",
        )
    )
    assert result["ok"]
    return env, control


async def append_input(env, control, identity, *, signal=False):
    await env.service.writer.append(
        scope=ConversationScope.group("80001", "20001"),
        platform_message_id=identity,
        sender_user_id="10001",
        direction="inbound",
        content="How is the task going?",
    )
    async with env.db.sessions() as reader:
        event = await reader.scalar(
            select(ChatEventModel.id).where(ChatEventModel.platform_message_id == identity)
        )
    input_id = await control.repository.enqueue(
        control.lease.conversation_id,
        1,
        identity,
        kind="message",
        event_id=event,
        work_id=control.current["id"],
        ready=False,
    )
    await control.repository.prepare_input(
        input_id,
        {
            "text": "How is the task going?",
            **({"signal": True} if signal else {}),
        },
    )
    return input_id, event


@pytest.mark.asyncio
async def test_metadata_update_preserves_goal_wait_and_checkpoint_communication(database, tmp_path):
    _, control = await control_env(database, tmp_path, reporting="quiet")
    identity = control.current["id"]
    assert (
        json.loads((await control.repository.get(identity))["checkpoint_json"])["communication"][
            "reporting"
        ]
        == "quiet"
    )
    await control.patch_communication(start_feedback_given=True, input_feedback_through_id=4)
    waits = WorkWaitRepository(control.repository)
    await waits.register(
        control.lease,
        work_id=identity,
        source=control.source,
        call_key="wait",
        mode="any",
        conditions=[{"kind": "time_due", "after_seconds": 3600}],
        deadline_at=None,
    )
    before = await waits.describe(identity)
    control.current = await control.repository.transition(
        control.lease, identity, control.current["revision"], "waiting_external"
    )
    control.ending = "waiting_external"
    result = json.loads(
        await control.execute(
            "task_control",
            {
                "action": "update",
                "reporting": "interactive",
            },
            "metadata",
        )
    )
    assert result["ok"] and control.reporting == "interactive"
    assert control.current["goal"] == "investigate the original question"
    assert control.current["state"] == control.ending == "waiting_external"
    assert await waits.describe(identity) == before
    await control.repository.checkpoint(control.lease, identity, {"reason": "blocked"})
    control.current = await control.repository.get(identity)
    assert control.communication == {
        "reporting": "interactive",
        "start_feedback_given": True,
        "input_feedback_through_id": 4,
    }
    assert not json.loads(await control.execute("task_control", {"action": "update"}, "empty"))[
        "ok"
    ]


@pytest.mark.asyncio
async def test_report_uses_original_trigger_and_displayed_inputs_without_fabrication(
    database, tmp_path
):
    env, control = await control_env(database, tmp_path)
    original = control.source["trigger_event_id"]
    input_id, event_id = await append_input(env, control, "steer")
    # The current foreground trigger is not the Work's original trigger.
    control.source = {**control.source, "trigger_event_id": event_id}
    assert await control.validate_work_report(
        {
            "work_report": {"kind": "start", "reply_to_event_ids": [original]},
        }
    ) == {"kind": "start", "reply_to_event_ids": [original]}
    with pytest.raises(ValueError, match="work_report_event_not_admitted"):
        await control.validate_work_report(
            {
                "work_report": {"kind": "reply", "reply_to_event_ids": [event_id]},
            }
        )
    await control.repository.stage(control.lease, [input_id], "shown")
    assert await control.validate_work_report(
        {
            "work_report": {"kind": "reply", "reply_to_event_ids": [event_id]},
        }
    ) == {"kind": "reply", "reply_to_event_ids": [event_id]}
    await control.repository.consume(control.lease, "shown")
    assert await control.communication_inputs() == [{"id": input_id, "event_id": event_id}]
    _, signal_id = await append_input(env, control, "signal", signal=True)
    pending = await control.pending()
    await control.repository.stage(control.lease, [item["id"] for item in pending], "signals")
    await control.repository.consume(control.lease, "signals")
    assert signal_id not in [item["event_id"] for item in await control.communication_inputs()]
    async with database.sessions() as reader:
        assert not await reader.scalar(select(inputs.c.id).where(inputs.c.event_id == original))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments,error",
    [
        (
            {"work_report": {"kind": "reply", "reply_to_event_ids": list(range(1, 10))}},
            "work_report_event_ids_invalid",
        ),
        (
            {"work_report": {"kind": "reply", "reply_to_event_ids": [True]}},
            "work_report_event_ids_invalid",
        ),
        (
            {"work_report": {"kind": "reply", "reply_to_event_ids": ["inbound"]}},
            "work_report_event_ids_invalid",
        ),
        (
            {"work_report": {"kind": "reply", "reply_to_event_ids": [999999]}},
            "work_report_event_not_admitted",
        ),
        (
            {"work_report": {"kind": "start"}, "target": {"kind": "person", "target_id": "wrong"}},
            "work_report_target_not_current",
        ),
        ({"work_report": {"kind": "start", "work_id": "another"}}, "work_report_invalid"),
        ({"work_report": None}, "work_report_invalid"),
    ],
)
async def test_invalid_association_never_dispatches(database, tmp_path, arguments, error):
    _, control = await control_env(database, tmp_path)
    control.session = WorkSession(control, "contract")
    await control.session.restore(TurnTranscript((ChatMessage("user", "investigate"),)))
    invoked = False

    async def invoke():
        nonlocal invoked
        invoked = True
        return "{}"

    call = ToolCall(
        "invalid", ToolFunction("send_message", json.dumps({"text": "reply", **arguments}))
    )
    receipt = json.loads(await control.session.execute(call, invoke, allow_pending=True))
    assert receipt == {"ok": False, "executed": False, "error": error}
    assert not invoked
    assert not await control.effect_evidence()
    assert (await control.repository.get(control.current["id"]))["tool_calls"] == 0


@pytest.mark.asyncio
async def test_transport_report_survives_long_history_and_does_not_complete_early(
    database, tmp_path
):
    env, control = await control_env(database, tmp_path, reporting="interactive")
    session = control.session = WorkSession(control, "contract")
    await session.restore(TurnTranscript((ChatMessage("user", "investigate"),)))
    await session.save("paired")

    async def send(kind, identity):
        args = {"text": f"{kind} explanation", "work_report": {"kind": kind}}
        call = ToolCall(identity, ToolFunction("send_message", json.dumps(args)))

        async def invoke():
            result = await env.service.execute(
                "send_message",
                args,
                replace(
                    env.context,
                    turn_id=control.current["id"],
                    call_id=identity,
                ),
            )
            return json.dumps({"ok": True, "data": result})

        return await session.execute(call, invoke, allow_pending=True)

    await send("start", "start")
    for index in range(70):
        key = f"later:{index:03d}"
        await control.repository.prepare_effect(control.lease, control.current["id"], key, "tool")
        await control.repository.record_effect(
            key,
            "accepted",
            {
                "outcome": {"tool": "read", "ok": True, "side_effecting": False},
                "result": "{}",
            },
        )
    await control.refresh_effects()
    assert len(control.known_effects) == 64
    reports = await control.communication_reports(kind="start", delivered_only=True)
    assert len(reports) == 1 and reports[0]["delivered_message"]
    assert all("work_report" not in args for _, args in env.bot.calls)
    complete = json.loads(await control.execute("task_control", {"action": "complete"}, "early"))
    assert complete["error"] == "work_completion_requires_final_delivery_receipt"
    await send("final", "final")
    assert json.loads(await control.execute("task_control", {"action": "complete"}, "complete"))[
        "ok"
    ]
    restored = WorkControl(
        control.repository,
        control.lease,
        control.source_key,
        control.source,
        control.validate,
        current=await control.repository.get(control.current["id"]),
    )
    assert len(await restored.communication_reports(kind="start", delivered_only=True)) == 1


@pytest.mark.asyncio
async def test_interrupted_send_keeps_association_and_recovery_never_replays(database, tmp_path):
    _, control = await control_env(database, tmp_path)
    session = control.session = WorkSession(control, "contract")
    await session.restore(TurnTranscript((ChatMessage("user", "investigate"),)))
    original = control.source["trigger_event_id"]
    args = {"text": "reply", "work_report": {"kind": "reply", "reply_to_event_ids": [original]}}
    call = ToolCall("send", ToolFunction("send_message", json.dumps(args)))
    attempts = 0

    async def interrupted():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("interrupted after possible transport")

    with pytest.raises(RuntimeError):
        await session.execute(call, interrupted, allow_pending=True)
    evidence = await control.communication_reports(event_ids=(original,))
    assert len(evidence) == 1 and evidence[0]["uncertain"]
    assert not await control.communication_reports(event_ids=(original,), delivered_only=True)
    receipt = json.loads(await session.execute(call, interrupted, allow_pending=True))
    assert receipt["uncertain"] and attempts == 1


@pytest.mark.asyncio
async def test_failed_start_cannot_be_retried_or_cleared_by_quiet(database, tmp_path):
    _, control = await control_env(database, tmp_path, reporting="interactive")
    session = control.session = WorkSession(control, "contract")
    await session.restore(TurnTranscript((ChatMessage("user", "investigate"),)))
    args = {"text": "starting", "work_report": {"kind": "start"}}
    call = ToolCall("start", ToolFunction("send_message", json.dumps(args)))
    attempts = 0

    async def failed():
        nonlocal attempts
        attempts += 1
        return json.dumps({"ok": False, "data": {"status": "failed"}})

    original = await session.execute(call, failed, allow_pending=True)
    assert await session.execute(call, failed, allow_pending=True) == original
    retry = json.loads(
        await session.execute(
            ToolCall("retry", call.function),
            failed,
            allow_pending=True,
        )
    )
    assert retry["error"] == "work_start_delivery_unconfirmed" and attempts == 1
    quiet = json.loads(
        await control.execute(
            "task_control",
            {
                "action": "update",
                "reporting": "quiet",
            },
            "quiet",
        )
    )
    assert quiet["error"] == "work_start_delivery_unconfirmed"
    assert control.reporting == "interactive"


@pytest.mark.asyncio
async def test_missing_journal_with_previous_tools_is_recovery_source(database, tmp_path):
    _, control = await control_env(database, tmp_path)
    await control.repository.checkpoint(control.lease, control.current["id"], None, tools=1)
    control.current = await control.repository.get(control.current["id"])
    session = control.session = WorkSession(control, "contract")
    await session.restore(TurnTranscript((ChatMessage("user", "new wakeup"),)))
    assert session.uses_recovery_transcript


@pytest.mark.asyncio
async def test_interactive_cannot_downgrade_before_any_start(database, tmp_path):
    _, control = await control_env(database, tmp_path, reporting="interactive")
    receipt = json.loads(
        await control.execute(
            "task_control",
            {
                "action": "update",
                "reporting": "quiet",
            },
            "quiet",
        )
    )
    assert receipt["error"] == "work_reporting_cannot_quiet_interactive"
    assert control.reporting == "interactive"


@pytest.mark.asyncio
async def test_typed_send_survives_presentation_failure_without_replay(database, tmp_path):
    env, control = await control_env(database, tmp_path, reporting="interactive")
    session = control.session = WorkSession(control, "contract")
    await session.restore(TurnTranscript((ChatMessage("user", "investigate"),)))
    args = {"text": "starting", "work_report": {"kind": "start"}}
    call = ToolCall("send", ToolFunction("send_message", json.dumps(args)))
    attempts = 0

    async def interrupted_publication():
        nonlocal attempts
        attempts += 1
        receipt = await env.service.execute(
            "send_message",
            args,
            replace(
                env.context,
                turn_id=control.current["id"],
                call_id="publication",
            ),
        )
        current_result_capture.get().outcome = normalize_legacy_result(
            {"ok": True, "data": receipt},
            provider_id="core",
            tool_name="send_message",
        )
        raise RuntimeError("presentation persistence failed")

    with pytest.raises(RuntimeError, match="presentation persistence failed"):
        await session.execute(call, interrupted_publication, allow_pending=True)
    assert len(await control.communication_reports(kind="start", delivered_only=True)) == 1
    recovered = json.loads(await session.execute(call, interrupted_publication, allow_pending=True))
    assert recovered["delivered_message"] and recovered["replay_forbidden"]
    assert attempts == 1 and sum(name == "send_group_msg" for name, _ in env.bot.calls) == 1


@pytest.mark.asyncio
async def test_journal_and_feedback_marker_commit_or_rollback_together(database, tmp_path):
    _, control = await control_env(database, tmp_path, reporting="interactive")
    session = control.session = WorkSession(control, "contract")
    await session.restore(TurnTranscript((ChatMessage("user", "investigate"),)))
    await session.save("paired")
    async with database.immediate_session() as writer:
        await writer.execute(
            text(
                "CREATE TRIGGER fail_communication BEFORE UPDATE OF checkpoint_json "
                "ON runtime_work WHEN json_extract(NEW.checkpoint_json, "
                "'$.communication.input_feedback_through_id') = 7 "
                "BEGIN SELECT RAISE(ABORT, 'communication_test_failure'); END"
            )
        )
    with pytest.raises(IntegrityError, match="communication_test_failure"):
        await session.save("dispatched", communication_updates={"input_feedback_through_id": 7})
    async with database.sessions() as reader:
        assert (
            await reader.scalar(
                select(journal.c.phase).where(journal.c.work_id == control.current["id"])
            )
            == "paired"
        )
    reopened_database = Database(database.url)
    try:
        original = await WorkRepository(reopened_database).get(control.current["id"])
    finally:
        await reopened_database.close()
    assert (
        json.loads(original["checkpoint_json"])["communication"]["input_feedback_through_id"] == 0
    )
    async with database.immediate_session() as writer:
        await writer.execute(text("DROP TRIGGER fail_communication"))
    await session.save("dispatched", communication_updates={"input_feedback_through_id": 7})
    reopened_database = Database(database.url)
    try:
        refreshed = await WorkRepository(reopened_database).get(control.current["id"])
    finally:
        await reopened_database.close()
    assert (
        json.loads(refreshed["checkpoint_json"])["communication"]["input_feedback_through_id"] == 7
    )
    async with database.sessions() as reader:
        assert (
            await reader.scalar(
                select(journal.c.phase).where(journal.c.work_id == control.current["id"])
            )
            == "dispatched"
        )
    assert control.communication["input_feedback_through_id"] == 7


@pytest.mark.asyncio
async def test_another_work_input_is_rejected_before_any_dispatch(database, tmp_path):
    env, control = await control_env(database, tmp_path)
    input_id, event_id = await append_input(env, control, "other-work-event")
    other = await control.repository.accept(
        control.lease,
        source_key="another-work",
        source=control.source,
        goal="separate task",
        output_kind="answer",
    )
    async with database.immediate_session() as writer:
        await writer.execute(
            update(inputs)
            .where(inputs.c.id == input_id)
            .values(
                work_id=other["id"],
                state="consumed",
            )
        )
    session = control.session = WorkSession(control, "contract")
    await session.restore(TurnTranscript((ChatMessage("user", "original"),)))
    calls = 0

    async def invoke():
        nonlocal calls
        calls += 1
        return "{}"

    arguments = {
        "text": "reply",
        "work_report": {
            "kind": "reply",
            "reply_to_event_ids": [event_id],
        },
    }
    receipt = json.loads(
        await session.execute(
            ToolCall("cross-work", ToolFunction("send_message", json.dumps(arguments))),
            invoke,
            allow_pending=True,
        )
    )
    assert receipt["error"] == "work_report_event_not_admitted"
    assert calls == 0 and not await control.effect_evidence()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["start", "final"])
async def test_reports_do_not_self_certify_a_state_change(database, tmp_path, kind):
    env, control = await control_env(
        database,
        tmp_path,
        reporting="interactive",
        output_kind="state_change",
    )
    session = control.session = WorkSession(control, "contract")
    await session.restore(TurnTranscript((ChatMessage("user", "write state file"),)))
    arguments = {"text": "status", "work_report": {"kind": kind}}

    async def report():
        receipt = await env.service.execute(
            "send_message",
            arguments,
            replace(
                env.context,
                turn_id=control.current["id"],
                call_id=kind,
            ),
        )
        return json.dumps({"ok": True, "data": receipt})

    await session.execute(
        ToolCall("report", ToolFunction("send_message", json.dumps(arguments))),
        report,
        allow_pending=True,
    )
    incomplete = json.loads(await control.execute("task_control", {"action": "complete"}, "early"))
    assert incomplete["error"] == "work_completion_requires_execution_evidence"

    async def write():
        return json.dumps({"ok": True, "data": env.store.write("state.txt", b"changed")})

    await session.execute(ToolCall("write", ToolFunction("workspace_write", "{}")), write)
    assert json.loads(await control.execute("task_control", {"action": "complete"}, "complete"))[
        "ok"
    ]


@pytest.mark.asyncio
async def test_legacy_consumed_watermark_is_scoped_and_not_a_reply_receipt(database, tmp_path):
    env, control = await control_env(database, tmp_path)
    old_input, old_event = await append_input(env, control, "old")
    await control.repository.stage(control.lease, [old_input], "old")
    await control.repository.consume(control.lease, "old")
    new_input, _ = await append_input(env, control, "new")
    assert new_input > await control.communication_consumed_watermark() == old_input
    assert not await control.communication_reports(event_ids=(old_event,))


@pytest.mark.asyncio
async def test_reply_witness_is_complete_for_each_input_beyond_display_page(database, tmp_path):
    env, control = await control_env(database, tmp_path)
    first_id, first_event = await append_input(env, control, "first")
    last_id, last_event = await append_input(env, control, "last")
    await control.repository.stage(control.lease, [first_id, last_id], "shown")
    target = await control.communication_target()
    for index in range(257):
        key = f"reply:{index:03d}"
        event = first_event if index < 256 else last_event
        await control.repository.prepare_effect(control.lease, control.current["id"], key, "tool")
        await control.repository.record_effect(
            key,
            "accepted",
            {
                "outcome": {
                    "tool": "send_message",
                    "work_report": {
                        "kind": "reply",
                        "reply_to_event_ids": [event],
                    },
                    "report_target": target,
                    "delivery_target": target,
                    "delivered_message": True,
                    "side_effecting": True,
                    "ok": True,
                }
            },
        )
    witnesses = await control.communication_reports(event_ids=(first_event, last_event))
    assert {
        event for witness in witnesses for event in witness["work_report"]["reply_to_event_ids"]
    } == {first_event, last_event}
    assert len(witnesses) == 2


@pytest.mark.asyncio
async def test_checkpoint_feedback_survives_contract_new_chain(database, tmp_path):
    _, control = await control_env(database, tmp_path, reporting="interactive")
    session = control.session = WorkSession(control, "old")
    initial = TurnTranscript((ChatMessage("user", "original task"),))
    await session.restore(initial, compaction_brief=ChatMessage("user", "original task"))
    assert not session.uses_recovery_transcript
    await control.patch_communication(start_feedback_given=True, final_feedback_given=True)
    await session.save("paired")
    control.current = await control.repository.get(control.current["id"])
    changed = control.session = WorkSession(control, "new")
    await changed.restore(TurnTranscript((ChatMessage("user", "new wakeup"),)))
    assert changed.uses_recovery_transcript
    assert control.reporting == "interactive"
    assert control.communication["start_feedback_given"]
    assert control.communication["final_feedback_given"]
