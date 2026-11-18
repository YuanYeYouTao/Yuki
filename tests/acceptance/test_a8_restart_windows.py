"""A8 (taskbook §10.3): phase-1 S04–S07 crash windows applied to delegation records.

One real Claude Code (haiku) executor on one connector pairing; the Bot (container,
SQLite, HTTP/WSS) is stopped and started again in-process at each window:
- W1 registered, never written (the write is suppressed, then the Bot stops);
- W2 the connector began the command, native result not yet in (Bot stops mid-turn);
- W3 the connector journaled the receipt, the Bot never committed it (no ACK);
- W4 the Bot committed the terminal event, its ACK was lost.
Recorded per window: native command count (connector journal), original operation /
run / execution IDs, parent budget counters, record state sequence and parent inputs.
Artifact: ``test-results/external-agent-phase2-acceptance/A8.json``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.acceptance.harness import (
    Facts,
    Stack,
    StateRecorder,
    by_local,
    call,
    claude_instance,
    journal_commands,
    project,
    run_root,
    started,
    until,
    write_artifact,
)
from yuki.runtime.work_repository import TERMINAL

pytestmark = pytest.mark.real_agent

GOAL = (
    "Run exactly the shell command `ping -c 12 127.0.0.1` on its own (not chained, not in the "
    "background), then reply with just the word done."
)


async def test_a8_restart_windows_keep_ids_counts_and_inputs(tmp_path):
    facts = Facts()
    stack = Stack(run_root("test_a8_restart_windows"), facts)
    await stack.start()
    recorder = StateRecorder(stack)
    recorder.start()
    artifact: dict[str, Any] = {"scenario": "A8", "status": "incomplete", "windows": {}}
    cwd = project("a8")
    windows: dict[str, dict[str, Any]] = {}
    try:
        device = await stack.pair_device("Mac A")
        await stack.start_connector("mac-a", device, [claude_instance("claude-1", cwd)])
        agent = by_local(await stack.online_instances(device["device_id"], 1))["claude-1"]
        journal = stack.root / "connectors" / "mac-a" / "journal.sqlite3"

        async def decide(turn):
            tag = next((name for name in windows if f"A8-{name}" in turn.trigger_text), None)
            if tag is None:
                return None
            window = windows[tag]
            if turn.work_id is None:
                return call("manage_task", {"action": "start", "goal": f"A8 {tag}"})
            window["parent"] = turn.work_id
            tool = turn.last_tool()
            if started(tool) and window.get("record") is None:
                window["record"] = tool["id"]
                recorder.watch(tool["id"], turn.work_id)
                return call("wait", {"for": [{"run": tool["id"]}]})
            if window.get("record") is None:
                return call(
                    "start_subtask",
                    {
                        "goal": GOAL,
                        "executor": {"agent": agent},
                        "cwd": str(cwd),
                        "scope": {
                            "write_paths": [str(cwd)],
                            "network": False,
                            "commands": ["ping"],
                        },
                    },
                )
            if (await stack.work_row(window["record"]))["state"] not in TERMINAL:
                return call("wait", {"for": [{"run": window["record"]}]})
            if tool is None or tool.get("status") != "completed":
                return call("manage_task", {"action": "finish", "result": "settled"})
            return None

        stack.decide = decide

        async def snapshot(tag: str) -> dict[str, Any]:
            window = windows[tag]
            parent = await stack.work_row(window["parent"])
            record = window.get("record")
            ops = await stack.operations(record) if record else []
            prompt = next((op for op in ops if op["action"] == "prompt"), None)
            return {
                "parent_state": parent["state"],
                "parent_budget": {
                    k: parent[k] for k in ("model_requests", "tool_calls", "sent_messages")
                },
                "record_state": (await stack.work_row(record))["state"] if record else None,
                "operation": prompt,
                "native_prompt_commands": len(
                    [
                        row
                        for row in journal_commands(journal)
                        if prompt and row["operation_id"] == prompt["operation_id"]
                    ]
                ),
                "parent_inputs": await stack.inputs_of(window["parent"]),
                "completed_events": len(
                    [e for e in await stack.events(record) if e["kind"] == "completed"]
                )
                if record
                else 0,
            }

        async def begin(tag: str) -> None:
            windows[tag] = {"record": None}
            facts.mark("window_start", window=tag)
            # One group per window: a later request must not join an earlier window's Work.
            await stack.group(42, f"!ai A8-{tag} 让 Claude 跑一下", chat=-10100 - len(windows))

            async def registered():
                record = windows[tag].get("record")
                return record and await stack.operations(record)

            await until(registered, 60)

        async def settled(tag: str, limit: float = 240) -> None:
            async def done():
                parent = windows[tag].get("parent")
                return parent and (await stack.work_row(parent))["state"] in TERMINAL

            await until(done, limit, 1)

        # ---- W1: registered, never written --------------------------------------------
        service = stack.runtime.external_agents
        suppressed: list[str] = []
        original_dispatch = service._dispatch

        async def no_write(identity, connection, command):
            suppressed.append(identity)  # the process "ends" before the write

        service._dispatch = no_write
        await begin("W1")
        before = await snapshot("W1")
        await stack.restart()
        await stack.online_instances(device["device_id"], 1)
        await asyncio.sleep(20)
        after = await snapshot("W1")
        windows["W1"]["facts"] = {
            "suppressed_writes": len(suppressed),
            "before": before,
            "after_restart": after,
            "state_sequence": recorder.of(windows["W1"]["record"]),
        }
        del original_dispatch

        # ---- W2: connector began, Bot stops before the native result -------------------
        await begin("W2")

        async def native_began():
            rows = journal_commands(journal)
            ops = await stack.operations(windows["W2"]["record"])
            return ops and any(r["operation_id"] == ops[0]["operation_id"] for r in rows)

        await until(native_began, 60, 0.5)
        await asyncio.sleep(3)
        before = await snapshot("W2")
        await stack.restart()
        await settled("W2")
        windows["W2"]["facts"] = {
            "before": before,
            "after": await snapshot("W2"),
            "state_sequence": recorder.of(windows["W2"]["record"]),
        }

        # ---- W3: receipt journaled, never committed (no ACK) ---------------------------
        held = asyncio.Event()

        def hold_receipt(service: Any, tag: str) -> None:
            original = service.receive_events

            async def receive(device_id, events):
                record = windows[tag].get("record")
                ops = await stack.operations(record) if record else []
                ids = {op["operation_id"] for op in ops}
                if any(e.get("kind") == "receipt" and e.get("operation_id") in ids for e in events):
                    held.set()
                    await asyncio.Event().wait()  # the Bot dies before committing
                return await original(device_id, events)

            service.receive_events = receive

        hold_receipt(stack.runtime.external_agents, "W3")
        await begin("W3")
        await asyncio.wait_for(held.wait(), 120)
        before = await snapshot("W3")
        await stack.restart()
        await settled("W3")
        windows["W3"]["facts"] = {
            "before": before,
            "after": await snapshot("W3"),
            "state_sequence": recorder.of(windows["W3"]["record"]),
        }

        # ---- W4: terminal event committed, ACK lost --------------------------------------
        committed = asyncio.Event()
        service = stack.runtime.external_agents
        original_receive = service.receive_events

        async def lose_ack(device_id, events):
            result = await original_receive(device_id, events)
            record = windows["W4"].get("record")
            ops = await stack.operations(record) if record else []
            ids = {op["operation_id"] for op in ops}
            if any(e.get("kind") == "completed" and e.get("operation_id") in ids for e in events):
                committed.set()
                await asyncio.Event().wait()  # committed; the ACK is never sent
            return result

        service.receive_events = lose_ack
        await begin("W4")
        await asyncio.wait_for(committed.wait(), 240)
        await asyncio.sleep(2)
        before = await snapshot("W4")
        await stack.restart()
        await settled("W4")
        await asyncio.sleep(5)
        windows["W4"]["facts"] = {
            "before": before,
            "after": await snapshot("W4"),
            "state_sequence": recorder.of(windows["W4"]["record"]),
        }

        for tag, window in windows.items():
            artifact["windows"][tag] = {
                "parent_work_id": window["parent"],
                "record_work_id": window["record"],
                **window["facts"],
            }
        artifact.update(status="completed", marks=facts.marks)
        w1 = windows["W1"]["facts"]
        assert w1["after_restart"]["native_prompt_commands"] == 0
        assert (
            w1["after_restart"]["operation"]["operation_id"]
            == w1["before"]["operation"]["operation_id"]
        )
        for tag in ("W2", "W3", "W4"):
            f = windows[tag]["facts"]
            assert f["after"]["native_prompt_commands"] == 1, (tag, f)
            assert (
                f["after"]["operation"]["operation_id"] == f["before"]["operation"]["operation_id"]
            )
            assert f["after"]["operation"]["run_id"] == f["before"]["operation"]["run_id"]
            assert f["after"]["completed_events"] == 1, (tag, f)
            results = [
                i
                for i in f["after"]["parent_inputs"]
                if i["source_key"].startswith("worker-result:")
            ]
            assert len(results) == 1, (tag, f["after"]["parent_inputs"])
    except BaseException as error:
        artifact.update(
            status="failed",
            error=repr(error)[:500],
            marks=facts.marks,
            partial={k: v.get("facts") for k, v in windows.items()},
        )
        raise
    finally:
        artifact["model_requests"] = facts.model
        write_artifact("A8", artifact)
        await recorder.stop()
        for name in list(stack.connectors):
            await stack.stop_connector(name)
        await stack.stop()
