"""Lifecycle control calls are validated by the frozen contract schema, end to end.

Failure cases listed before the implementation
(docs/operations/control-call-schema-20261108.md, section 1):
* every one of the five controls with malformed arguments, driven through the
  real Main loop, returns the business-tool refusal
  ``{ok:false, error:"tool_input_validation_failed", message, executed:false}``
  with one readable sentence naming the parameter, and changes nothing;
* the newly declared ``plugin_event`` length and non-JSON arguments are refused
  by the same schema;
* a batch mixing invalid and valid controls still executes the valid ones in
  their original order; refusals charge no business tool budget;
* recovery: a control call that already has a receipt returns that original
  receipt byte for byte without being validated again, while an undispatched
  pending control call is validated against the current schema;
* the Code Mode child entry (``invocation=None``) uses the same check.

Each scenario writes normalized evidence under ``test-results/control-call-schema/``.
"""

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import select, update
from tests.support.work_runner_helpers import case, run, tool

from yuki.domain.messages import ChatMessage, ChatResponse, ToolCall, ToolFunction
from yuki.runtime.subagent_schema import children
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_schema_v1 import effects
from yuki.runtime.work_wait import WorkWaitRepository
from yuki.services.main_agent_backend import _refused_result
from yuki.services.turn_transcript import TurnTranscript

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HEX = re.compile(r"\b[0-9a-f]{64}\b")

INVALID = (
    ("get_tasks", {"limit": 0}, "bad-get"),
    ("manage_task", {"action": "complete"}, "bad-manage"),
    ("wait", {"for": [{"message": False}]}, "bad-wait"),
    (
        "wait",
        {"for": [{"plugin_event": {"plugin_id": "p", "event_type": "e" * 129}}]},
        "bad-plugin",
    ),
    ("start_subtask", {"context": "no goal"}, "bad-start"),
    ("message_task", {"text": "hi", "ask": "yes"}, "bad-message"),
)


def _stable(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    seen: dict[str, str] = {}
    text = _UUID.sub(lambda m: seen.setdefault(m.group(0), f"<uuid:{len(seen) + 1}>"), text)
    return _HEX.sub("<sha256>", text) + "\n"


def _write(name, value):
    directory = Path(__file__).resolve().parents[2] / "test-results" / "control-call-schema"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(_stable(value), encoding="utf-8")


def _pairs(control):
    return {
        message.tool_call_id: json.loads(message.content)
        for message in control.session.transcript.request().messages
        if message.role == "tool"
    }


async def _receipts(database, work_id):
    async with database.read_session() as reader:
        rows = (
            (await reader.execute(select(effects).where(effects.c.work_id == work_id)))
            .mappings()
            .all()
        )
    return {row["effect_key"].rsplit(":", 1)[-1]: row for row in rows}


def _business_refusal_keys():
    return set(json.loads(_refused_result("read_fixture", "tool_input_validation_failed")))


async def test_invalid_arguments_for_each_control_are_refused_by_the_frozen_schema(
    database, tmp_path
):
    calls = (
        *(tool(name, args, identity) for name, args, identity in INVALID),
        ToolCall("bad-json", ToolFunction("manage_task", "not json")),
        tool("manage_task", {"action": "update", "goal": "新的目标"}, "update"),
        tool("manage_task", {"action": "finish", "result": "完成"}, "finish"),
    )
    probe = await case(database, tmp_path, [ChatResponse("", 0, tool_calls=calls)])
    work_id = probe.control.current["id"]
    before = await probe.repository.get(work_id)

    result = await run(probe)

    assert result.work_state == "completed" and result.text == "完成"
    assert len(probe.provider.requests) == 1 and probe.observed == []
    pairs = _pairs(probe.control)
    assert list(pairs) == [call.id for call in calls]
    refused = {identity: pairs[identity] for _name, _args, identity in INVALID}
    refused["bad-json"] = pairs["bad-json"]
    for identity, payload in refused.items():
        assert set(payload) == _business_refusal_keys() == {"ok", "error", "message", "executed"}
        assert payload["ok"] is False and payload["executed"] is False, identity
        assert payload["error"] == "tool_input_validation_failed", identity
    assert refused["bad-get"]["message"] == "参数 limit：不能小于 1"
    assert refused["bad-manage"]["message"].startswith("参数 action：只能取 ")
    assert refused["bad-wait"]["message"].startswith("参数 for[0]：")
    assert refused["bad-plugin"]["message"].startswith("参数 for[0]：")
    assert refused["bad-start"]["message"] == "缺少必填参数 goal"
    assert refused["bad-message"]["message"] == "参数 ask：类型应为 boolean"
    assert refused["bad-json"]["message"] == "参数必须是 JSON 对象。"
    # Valid controls in the same batch still ran in their original order.
    assert pairs["update"]["status"] == "running" and pairs["finish"]["ending"] == "completed"

    after = await probe.repository.get(work_id)
    assert after["goal"] == "新的目标"
    assert after["tool_calls"] == before["tool_calls"]  # No business budget is charged.
    assert await WorkWaitRepository(probe.repository).describe(work_id) is None
    async with database.read_session() as reader:
        assert await reader.scalar(select(children.c.work_id)) is None
    receipts = await _receipts(database, work_id)
    for identity in refused:
        receipt = json.loads(receipts[identity]["receipt_json"])
        assert json.loads(receipt["result"]) == refused[identity]
        assert receipt["outcome"]["executed"] is False
        assert receipt["outcome"]["mutation_committed"] is False
    _write(
        "invalid-controls",
        {
            "refusals": refused,
            "valid": {"update": pairs["update"], "finish": pairs["finish"]},
            "tool_calls_before_after": [before["tool_calls"], after["tool_calls"]],
            "model_requests": len(probe.provider.requests),
            "receipt_states": {identity: receipts[identity]["state"] for identity in refused},
        },
    )


class PaidResponseCrash(BaseException):
    pass


async def test_recovery_returns_original_receipt_and_validates_only_undispatched_calls(
    database, tmp_path, monkeypatch
):
    calls = (
        tool("wait", {"for": []}, "receipted"),
        tool("manage_task", {"action": "resume", "id": 7}, "undispatched"),
        tool("manage_task", {"action": "finish", "result": "恢复后完成"}, "finish"),
    )
    probe = await case(database, tmp_path, [ChatResponse("", 0, tool_calls=calls)])
    control = probe.control
    execute_batch = probe.runner._execute_tool_batch
    armed = True
    dispatched = []
    paired = {}

    async def interrupted_batch(pending, *args, **kwargs):
        nonlocal armed
        if armed and pending[0].id == "undispatched":
            armed = False
            raise PaidResponseCrash()
        if not armed:
            dispatched.extend(call.id for call in pending)
        return await execute_batch(pending, *args, **kwargs)

    append_result = TurnTranscript.append_result

    def record_pairing(transcript, call_id, result):
        # What the recovered chain pairs for each original call.
        paired[call_id] = result
        append_result(transcript, call_id, result)

    monkeypatch.setattr(probe.runner, "_execute_tool_batch", interrupted_batch)
    with pytest.raises(PaidResponseCrash):
        await run(probe)
    receipted_key = control.session.call_key("receipted")
    # A receipt written before this change keeps its original domain text.
    original = json.dumps(
        {"ok": False, "error": "wait_for_invalid", "message": "for 是非空数组。"},
        ensure_ascii=False,
    )
    async with database.immediate_session() as writer:
        stored = json.loads(
            await writer.scalar(
                select(effects.c.receipt_json).where(effects.c.effect_key == receipted_key)
            )
        )
        stored["result"] = original
        await writer.execute(
            update(effects)
            .where(effects.c.effect_key == receipted_key)
            .values(receipt_json=json.dumps(stored, ensure_ascii=False))
        )
    requests = len(probe.provider.requests)

    contract = probe.runner.main_contract
    view = contract.schema_view
    validated = []

    class RecordingView:
        def validate(self, name, arguments):
            validated.append(name)
            return view.validate(name, arguments)

    contract.schema_view = RecordingView()
    monkeypatch.setattr(TurnTranscript, "append_result", record_pairing)
    recovered = WorkControl(
        probe.repository, control.lease, control.source_key, control.source, control.validate
    )
    recovered.current = await probe.repository.get(control.current["id"])
    try:
        result = await probe.runner.run(
            (ChatMessage("user", "resume original response"),),
            replace(probe.runtime, work_control=recovered),
            probe.backend,
        )
    finally:
        contract.schema_view = view

    assert result.work_state == "completed" and result.text == "恢复后完成"
    assert len(probe.provider.requests) == requests == 1
    pairs = {identity: json.loads(text) for identity, text in paired.items()}
    assert set(pairs) == {"receipted", "undispatched", "finish"}
    assert paired["receipted"] == original
    assert "receipted" not in dispatched
    assert validated == ["manage_task", "manage_task"]  # Never the receipted wait.
    assert pairs["undispatched"]["error"] == "tool_input_validation_failed"
    assert pairs["undispatched"]["message"] == "参数 id：类型应为 string"
    receipts = await _receipts(database, recovered.current["id"])
    assert json.loads(receipts["receipted"]["receipt_json"])["result"] == original
    _write(
        "recovery-replay",
        {
            "receipted": pairs["receipted"],
            "undispatched": pairs["undispatched"],
            "finish": pairs["finish"],
            "validated_on_recovery": validated,
            "dispatched_on_recovery": dispatched,
            "model_requests": len(probe.provider.requests),
        },
    )


async def test_code_mode_child_entry_uses_the_same_schema_check(database, tmp_path):
    """``CodeHost.execute_control`` passes children to this entry without an invocation."""
    probe = await case(database, tmp_path, [])
    declared = frozenset(item.name for item in probe.runtime.fixed_tools)
    before = await probe.repository.get(probe.control.current["id"])
    results = {}
    for name, args, identity in INVALID:
        text, executed = await probe.runner._execute_control_call(
            tool(name, args, identity), probe.backend, probe.runtime, declared, f"child:{identity}"
        )
        results[identity] = json.loads(text)
        assert executed is True
        assert results[identity]["error"] == "tool_input_validation_failed"
    assert await probe.repository.get(probe.control.current["id"]) == before
    _write("code-mode-child-entry", results)
