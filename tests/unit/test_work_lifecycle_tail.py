"""Lifecycle decisions preserve the paid response and each original receipt."""

import json
from dataclasses import replace

import pytest
from sqlalchemy import select
from tests.support.work_runner_helpers import case, run, tool

from yuki.capabilities.results import normalize_legacy_result
from yuki.domain.messages import ChatMessage, ChatResponse
from yuki.runtime.effect_outcomes import current_result_capture
from yuki.runtime.work_budget_schema import budgets
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_schema_v1 import effects


def pairs(control):
    return {
        message.tool_call_id: json.loads(message.content)
        for message in control.session.transcript.request().messages
        if message.role == "tool"
    }


@pytest.mark.parametrize(
    "ending,arguments",
    [
        ("completed", {"action": "complete", "result": "original internal result"}),
        ("waiting_user", {"action": "need_input"}),
        (
            "waiting_external",
            {
                "action": "wait",
                "run_id": None,
                "conditions": [{"kind": "time_due", "after_seconds": 120}],
            },
        ),
    ],
)
async def test_normal_decision_preserves_all_authorized_original_calls(
    database, tmp_path, ending, arguments
):
    probe = await case(database, tmp_path, [])
    calls = (
        tool("task_control", arguments, "decision"),
        tool("read_fixture", {}, "read"),
        tool("write_fixture", {}, "write"),
        tool("send_message", {"text": "prepared report"}, "send"),
        tool("task_control", {"action": "get", "work_id": probe.control.current["id"]}, "get"),
        tool("subagent_control", {"action": "list"}, "children"),
    )
    probe.provider._responder = lambda _: ChatResponse("", 0, tool_calls=calls)
    result = await run(probe)
    assert result.work_state == ending
    assert len(probe.provider.requests) == 1
    assert probe.observed == ["read_fixture", "write_fixture", "send_message"]
    receipts = pairs(probe.control)
    assert list(receipts) == [call.id for call in calls]
    assert all(receipt["ok"] for receipt in receipts.values())
    await probe.control.settle(pending_inputs=False)
    persisted = await probe.repository.get(probe.control.current["id"])
    assert persisted["state"] == ending
    assert persisted["tool_calls"] == 3


@pytest.mark.parametrize("action", ["cancel", "fail"])
async def test_true_current_revocation_stops_remaining_calls(database, tmp_path, action):
    calls = (
        tool("task_control", {"action": "complete"}, "ordinary-decision"),
        tool("task_control", {"action": action}, "revoke-current"),
        tool("send_message", {"text": "revoked source"}, "not-sent"),
    )
    probe = await case(database, tmp_path, [ChatResponse("", 0, tool_calls=calls)])
    result = await run(probe)
    assert result.work_state == {"cancel": "cancelled", "fail": "failed"}[action]
    assert probe.observed == []
    assert pairs(probe.control)["not-sent"]["executed"] is False
    await probe.control.settle(pending_inputs=False)
    assert probe.control.current["state"] == result.work_state


class PaidResponseCrash(BaseException):
    pass


async def test_complete_then_send_keeps_real_social_receipt_without_recovery_resend(
    database, tmp_path
):
    probe = await case(
        database,
        tmp_path,
        [
            ChatResponse(
                "",
                0,
                tool_calls=(
                    tool("task_control", {"action": "complete", "result": "report sent"}, "done"),
                    tool("send_message", {"text": "actual original report"}, "report"),
                ),
            )
        ],
    )

    async def execute(invocation):
        receipt = await probe.env.service.execute(
            invocation.call.function.name,
            json.loads(invocation.call.function.arguments),
            replace(
                probe.env.context,
                turn_id=probe.control.current["id"],
                call_id=invocation.identity.operation_id,
            ),
        )
        result = json.dumps({"ok": receipt["status"] == "succeeded", "data": receipt})
        current_result_capture.get().outcome = normalize_legacy_result(
            result, provider_id="core", tool_name=invocation.call.function.name
        )
        return result

    probe.backend.execute_call = execute
    result = await run(probe)
    assert result.work_state == "completed" and result.text == "report sent"
    fact = next(
        fact for fact in await probe.control.effect_evidence() if fact["tool"] == "send_message"
    )
    assert fact["executed"] is True and fact["uncertain"] is False
    recovered = WorkControl(
        probe.repository,
        probe.control.lease,
        probe.control.source_key,
        probe.control.source,
        probe.control.validate,
        current=await probe.repository.get(probe.control.current["id"]),
    )
    await probe.runner.run(
        (ChatMessage("user", "resume the original response"),),
        replace(probe.runtime, work_control=recovered),
        probe.backend,
    )
    assert len(probe.provider.requests) == 1
    assert sum(action == "send_group_msg" for action, _ in probe.env.bot.calls) == 1
    await recovered.settle(pending_inputs=False)
    assert recovered.current["state"] == "completed"


@pytest.mark.parametrize("boundary", ["before_t1", "after_t1", "after_t2", "after_t3"])
async def test_response_recovery_continues_only_original_undispatched_suffix(
    database, tmp_path, monkeypatch, boundary
):
    calls = (
        tool("task_control", {"action": "complete", "result": "retained result"}, "complete"),
        tool("write_fixture", {"step": 1}, "first-tail"),
        tool("send_message", {"text": "original tail report"}, "last-tail"),
    )
    probe = await case(database, tmp_path, [ChatResponse("", 0, tool_calls=calls)])
    control = probe.control
    armed = True

    def is_first_tail(key):
        return key == control.session.call_key("first-tail")

    if boundary == "before_t1":
        execute_batch = probe.runner._execute_tool_batch

        async def interrupted_batch(pending, *args, **kwargs):
            nonlocal armed
            if armed and pending[0].id == "first-tail":
                armed = False
                raise PaidResponseCrash()
            return await execute_batch(pending, *args, **kwargs)

        monkeypatch.setattr(probe.runner, "_execute_tool_batch", interrupted_batch)
    elif boundary == "after_t1":
        admit = probe.repository.admit_dispatch

        async def interrupted_admission(lease, work_id, key, **kwargs):
            nonlocal armed
            if armed and is_first_tail(key):
                armed = False
                raise PaidResponseCrash()
            return await admit(lease, work_id, key, **kwargs)

        monkeypatch.setattr(probe.repository, "admit_dispatch", interrupted_admission)
    elif boundary == "after_t2":
        execute = probe.backend.execute_call

        async def interrupted_domain(invocation):
            nonlocal armed
            if armed and invocation.call.id == "first-tail":
                armed = False
                raise PaidResponseCrash()
            return await execute(invocation)

        monkeypatch.setattr(probe.backend, "execute_call", interrupted_domain)
    else:
        record = control.repository.record_effect

        async def interrupted_receipt(key, *args, **kwargs):
            nonlocal armed
            await record(key, *args, **kwargs)
            if armed and is_first_tail(key):
                armed = False
                raise PaidResponseCrash()

        monkeypatch.setattr(control.repository, "record_effect", interrupted_receipt)

    with pytest.raises(PaidResponseCrash):
        await run(probe)
    assert control.accepted_ending() == "completed"
    original_key = control.session.call_key("first-tail")
    last_key = control.session.call_key("last-tail")
    original_chain = control.session.transcript.chain_id
    original_requests = len(probe.provider.requests)

    recovered = WorkControl(
        probe.repository, control.lease, control.source_key, control.source, control.validate
    )
    recovered.current = await probe.repository.get(control.current["id"])
    result = await probe.runner.run(
        (ChatMessage("user", "resume original response"),),
        replace(probe.runtime, work_control=recovered),
        probe.backend,
    )
    assert result.work_state == "completed" and result.text == "retained result"
    assert len(probe.provider.requests) == original_requests == 1
    assert probe.observed.count("send_message") == 1
    assert probe.observed.count("write_fixture") == (boundary != "after_t2")
    first_result = json.loads(await recovered.session.journal.effect_result(original_key))
    assert first_result.get("uncertain", False) == (boundary == "after_t2")
    assert json.loads(await recovered.session.journal.effect_result(last_key))["ok"]
    async with database.sessions() as reader:
        first = (
            (await reader.execute(select(effects).where(effects.c.effect_key == original_key)))
            .mappings()
            .one()
        )
        usage = await reader.scalar(
            select(budgets.c.tools).where(budgets.c.root_id == recovered.current["id"])
        )
    invocation = json.loads(first["receipt_json"])["invocation"]
    assert invocation["chain_id"] == original_chain
    assert invocation["provider_call_id"] == "first-tail"
    assert usage == 2
    await recovered.settle(pending_inputs=False)
    assert recovered.current["state"] == "completed"
