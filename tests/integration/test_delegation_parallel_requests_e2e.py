# ruff: noqa: E402
"""W10: real Bot + real Connector + Claude driver; two parallel requests, one by one.

The 2026-11-20 incident on the real wire: one assistant message carries two Bash
tool uses and the CLI sends two ``can_use_tool`` control requests at once. Failure
cases (taskbook ``Yuki-委派权限卡并行处理与等待死锁任务书-2026-11-20`` §4, W10), written
before the implementation:
- only one card reaches the parent, or the two cards collapse into one request;
- after the parent answers one, ``wait(run)`` registers (the three-hour deadlock)
  instead of returning ``run_waiting_for_you`` with the other request;
- the answer's ``remaining`` omits the other request or lists the one just answered;
- an answer reaches the wrong ``can_use_tool`` (or none), or one answer settles both;
- after both answers the wait is refused, or the turn's end does not wake it.
Run in the ``claude-cross-version`` CI job's paired configuration (this Bot with this
connector): ``uv run --with claude-agent-sdk==0.2.172 pytest <this file>
-o pythonpath="src packages/yuki-agent-connector/src"``. No model, no paid call.
Artifact: ``test-results/delegation-parallel-requests/result.json``.
"""

import json
from pathlib import Path

import pytest

claude_agent_sdk = pytest.importorskip("claude_agent_sdk")
import asyncio

from sqlalchemy import select
from tests.integration.test_claude_connector_cross_version_e2e import CliWire, _connector, until
from tests.integration.test_external_agent_bridge import accepted_work
from tests.integration.test_external_agent_bridge import bridge as bridge
from yuki_agent_connector import PROTOCOL_VERSION

from yuki.external_agents import service as service_module
from yuki.external_agents.db_models import AgentOperationModel
from yuki.runtime.work_schema_v1 import inputs, work
from yuki.runtime.work_wait import WorkWaitRepository
from yuki.runtime.work_wait_schema import waits

ARTIFACT = Path(__file__).resolve().parents[2] / "test-results" / "delegation-parallel-requests"

pytestmark = pytest.mark.skipif(
    service_module.CONNECTOR_PROTOCOL != PROTOCOL_VERSION,
    reason="Q4 hard switch: different protocols never pair",
)


def _answers(wire: CliWire) -> dict[str, dict]:
    return {
        item["response"]["request_id"]: item["response"]["response"]
        for item in wire.writes
        if item["type"] == "control_response"
        and item["response"].get("request_id", "").startswith("can-use-")
    }


async def test_two_parallel_requests_are_answered_one_by_one(bridge, tmp_path, monkeypatch):
    wires: list[CliWire] = []
    connector = _connector(bridge, tmp_path, monkeypatch, wires)
    running = asyncio.create_task(connector.run())
    try:

        async def online():
            if running.done():
                await running
            catalog = await bridge.service.catalog(bridge.env.person)
            return any(item["online"] for item in catalog["instances"])

        await until(online)
        catalog = await bridge.service.catalog(bridge.env.person)
        instance = next(item for item in catalog["instances"] if item["online"])
        async with accepted_work(bridge) as control:
            parent = control.current["id"]
            started = await control.tool_outcome(
                "start_subtask",
                {"goal": "run both checks", "executor": {"agent": instance["id"]}},
                "claude-start",
            )
            assert started.ok, started
            record = started.data["id"]

            async def written():
                return bool(wires) and bool(wires[0].queued)

            await until(written)
            wire = wires[0]
            await wire.take_up()

            async def prompt():
                async with bridge.database.read_session() as reader:
                    row = await reader.scalar(
                        select(AgentOperationModel).where(
                            AgentOperationModel.work_id == record,
                            AgentOperationModel.action == "prompt",
                        )
                    )
                return row if row is not None and row.result_json else None

            await until(prompt)
            native_session = json.loads((await prompt()).result_json)["session_id"]
            # One assistant message, two tool uses, two can_use_tool at once.
            commands = {"toolu_first": "pytest -q tests/a", "toolu_second": "pytest -q tests/b"}
            await wire.incoming.put(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "model": "native",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": tool_use,
                                "name": "Bash",
                                "input": {"command": command},
                            }
                            for tool_use, command in commands.items()
                        ],
                    },
                    "session_id": native_session,
                }
            )
            for index, (tool_use, command) in enumerate(commands.items()):
                await wire.incoming.put(
                    {
                        "type": "control_request",
                        "request_id": f"can-use-{index}",
                        "request": {
                            "subtype": "can_use_tool",
                            "tool_name": "Bash",
                            "tool_use_id": tool_use,
                            "input": {"command": command},
                        },
                    }
                )

            async def cards():
                async with bridge.database.read_session() as reader:
                    rows = list(
                        (
                            await reader.execute(
                                select(inputs.c.source_key, inputs.c.payload_json)
                                .where(inputs.c.work_id == parent)
                                .order_by(inputs.c.id)
                            )
                        ).all()
                    )
                found = {
                    key.rsplit(":", 1)[-1]: json.loads(json.loads(payload)["text"])
                    for key, payload in rows
                    if ":request:" in key
                }
                return found if len(found) == 2 else None

            await until(cards)
            delivered = await cards()
            assert set(delivered) == set(commands)
            first = delivered["toolu_first"]["permission_id"]
            second = delivered["toolu_second"]["permission_id"]
            assert first != second
            # The incident: only the second is answered, then the parent waits on the run.
            answered_second = await control.tool_outcome(
                "message_task",
                {"id": record, "reply_to": second, "answer": "allow", "text": "b is safe"},
                "answer-second",
            )
            assert answered_second.ok, answered_second
            assert [item["permission_id"] for item in answered_second.data["remaining"]] == [first]

            async def second_answered():
                return "can-use-1" in _answers(wire)

            await until(second_answered)
            assert "can-use-0" not in _answers(wire)
            refused = await control.tool_outcome("wait", {"for": [{"run": record}]}, "wait-1")
            assert not refused.ok and refused.error_code == "run_waiting_for_you"
            assert f"permission_id={first}（" in refused.public_message
            assert f"permission_id={second}（" not in refused.public_message
            async with bridge.database.read_session() as reader:
                active = await reader.scalar(
                    select(waits.c.id).where(waits.c.work_id == parent, waits.c.status == "active")
                )
            assert active is None
            answered_first = await control.tool_outcome(
                "message_task",
                {"id": record, "reply_to": first, "answer": "allow", "text": "a is safe"},
                "answer-first",
            )
            assert answered_first.ok, answered_first
            assert answered_first.data["remaining"] == []

            async def both_answered():
                return {"can-use-0", "can-use-1"} <= set(_answers(wire))

            await until(both_answered)

            async def record_running():
                async with bridge.database.read_session() as reader:
                    state = await reader.scalar(select(work.c.state).where(work.c.id == record))
                return state == "waiting_external"

            await until(record_running)
            waited = await control.tool_outcome("wait", {"for": [{"run": record}]}, "wait-2")
            assert waited.ok, waited
            await wire.incoming.put(
                {
                    "type": "result",
                    "subtype": "success",
                    "duration_ms": 1,
                    "duration_api_ms": 1,
                    "is_error": False,
                    "num_turns": 1,
                    "session_id": native_session,
                    "result": "both checks passed",
                }
            )

            async def completed():
                async with bridge.database.read_session() as reader:
                    state = await reader.scalar(select(work.c.state).where(work.c.id == record))
                return state == "completed"

            await until(completed)
            woken = await WorkWaitRepository(control.repository).deliver_due()
            assert woken == 1
        answers = _answers(wire)
        assert [answers[key]["behavior"] for key in ("can-use-0", "can-use-1")] == [
            "allow",
            "allow",
        ]
        ARTIFACT.mkdir(parents=True, exist_ok=True)
        (ARTIFACT / "result.json").write_text(
            json.dumps(
                {
                    "connector_protocol": PROTOCOL_VERSION,
                    "cards": {
                        tool_use: card["permission_id"] for tool_use, card in delivered.items()
                    },
                    "first_answer": {
                        "permission_id": second,
                        "remaining": answered_second.data["remaining"],
                    },
                    "wait_with_one_unanswered": {
                        "error_code": refused.error_code,
                        "message": refused.public_message,
                        "active_wait": None,
                    },
                    "second_answer": {
                        "permission_id": first,
                        "remaining": answered_first.data["remaining"],
                    },
                    "wait_after_both": waited.data,
                    "cli_answers": answers,
                    "record_state": "completed",
                    "waits_delivered_on_completion": woken,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    finally:
        running.cancel()
        if connector.stream is not None:
            await connector.stream.close()
        await asyncio.gather(running, return_exceptions=True)
        connector.journal.close()
