"""#645: release while Claude waits on a native request (real Bot + real connector + SDK).

The production sequence of 2026-11-19 (delegation ``8331a5f7``): Claude asks for an
out-of-scope Bash while two more parallel Bash calls wait behind it; the parent
releases the delegation. Failure cases written first (taskbook
``Yuki-外部Agent交还时未答原生请求结算任务书-2026-11-20`` §4):
- F1: the pending request turns unowned on release and nobody answers it;
- F2: the next request of the same turn is unowned too and nobody answers it;
- F3: the parent is woken with a deny-only card for a session it gave back;
- F4/F5: the answer is not a deny recorded on the record as the release's;
- F8: the turn never reaches its boundary, so Yuki's SDK process never closes.
No model, no paid call: the CLI side is a deterministic stream-json peer. Run with
``uv run --with claude-agent-sdk==<connector pin> pytest <this file>``.
Artifact: ``test-results/claude-release-pending-request/result.json``.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("claude_agent_sdk")
from sqlalchemy import select
from tests.integration.test_claude_connector_cross_version_e2e import CliWire, _connector, until
from tests.integration.test_external_agent_bridge import accepted_work
from tests.integration.test_external_agent_bridge import bridge as bridge
from tests.integration.test_external_agent_delegation import _inputs, _row

from yuki.external_agents.db_models import AgentOperationModel
from yuki.external_agents.delegation import Delegations

ARTIFACT = Path(__file__).resolve().parents[2] / "test-results" / "claude-release-pending-request"


def can_use_tool(request_id: str, tool_use_id: str, command: str) -> dict[str, Any]:
    return {
        "type": "control_request",
        "request_id": request_id,
        "request": {
            "subtype": "can_use_tool",
            "tool_name": "Bash",
            "tool_use_id": tool_use_id,
            "input": {"command": command},
        },
    }


def answer(wire: CliWire, request_id: str) -> dict[str, Any] | None:
    for item in wire.writes:
        if item["type"] == "control_response" and item["response"].get("request_id") == request_id:
            return item["response"]["response"]  # type: ignore[no-any-return]
    return None


async def test_release_settles_a_pending_native_request_and_the_owner_closes(
    bridge, tmp_path, monkeypatch
):
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
            started = await control.tool_outcome(
                "start_subtask",
                {
                    "goal": "run the matrix checks",
                    "executor": {"agent": instance["id"]},
                    "scope": {"write_paths": [str(tmp_path)]},
                },
                "start-645",
            )
            assert started.ok, started
            identity = started.data["id"]

            async def written():
                return bool(wires) and bool(wires[0].queued)

            await until(written)
            wire = wires[0]
            native_session = wire.queued[0]["session_id"]
            await wire.take_up()
            # One assistant message, three parallel Bash calls; the CLI asks for them in turn.
            await wire.incoming.put(
                can_use_tool("req-a", "toolu-a", "python tools/check_matrix.py > /outside/a")
            )

            async def carded():
                return any(
                    "toolu-a" in row["payload_json"] or "permission" in row["payload_json"]
                    for row in await _inputs(bridge, control.current["id"])
                )

            await until(carded)
            before = len(await _inputs(bridge, control.current["id"]))
            released = await control.tool_outcome(
                "manage_task", {"action": "release", "id": identity}, "release-645"
            )
            assert released.ok, released

            async def denied_a():
                return answer(wire, "req-a") is not None

            await until(denied_a)  # F1
            await wire.incoming.put(
                can_use_tool("req-b", "toolu-b", "python tools/check_matrix.py > /outside/b")
            )

            async def denied_b():
                return answer(wire, "req-b") is not None

            await until(denied_b)  # F2
            assert answer(wire, "req-a")["behavior"] == "deny"  # F5
            assert answer(wire, "req-b")["behavior"] == "deny"
            assert wire.closed is False
            await wire.incoming.put(
                {
                    "type": "result",
                    "subtype": "success",
                    "duration_ms": 1,
                    "duration_api_ms": 1,
                    "is_error": False,
                    "num_turns": 1,
                    "session_id": native_session,
                    "result": "Both checks were declined; handing back.",
                }
            )

            async def closed():
                return wire.closed

            await until(closed)  # F8
            # F3: nothing new asks the parent to answer a session it gave back.
            later = (await _inputs(bridge, control.current["id"]))[before:]
            assert not any("deny_only" in row["payload_json"] for row in later)
            async with bridge.database.read_session() as reader:
                timeline = await Delegations(bridge.database).timeline(reader, identity, 0, 100)
                answers = list(
                    await reader.scalars(
                        select(AgentOperationModel).where(
                            AgentOperationModel.work_id == identity,
                            AgentOperationModel.action == "respond",
                        )
                    )
                )
            settled_by_release = [
                item for item in timeline["operations"] if item["action"] == "respond"
            ]
            # F4: two denies, both recorded on the record as the release's answers.
            assert len(settled_by_release) == 2 == len(answers)
            assert {item["answered_via"] for item in settled_by_release} == {"release"}
            assert {item["option"] for item in settled_by_release} == {"deny"}
            record = await _row(bridge, identity)
            assert (record["state"], record["reason"]) == ("cancelled", "released")
            ARTIFACT.mkdir(parents=True, exist_ok=True)
            (ARTIFACT / "result.json").write_text(
                json.dumps(
                    {
                        "record": {"state": record["state"], "reason": record["reason"]},
                        "cli_answers": {
                            "req-a": answer(wire, "req-a"),
                            "req-b": answer(wire, "req-b"),
                        },
                        "timeline_answers": settled_by_release,
                        "parent_inputs_after_release": [row["source_key"] for row in later],
                        "sdk_owner_closed": wire.closed,
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
