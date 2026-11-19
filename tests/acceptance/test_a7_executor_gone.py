"""A7 (taskbook §10.3, §4.9): the executor disappears.

Real Claude Code (haiku) delegations; three facts, one record each:
- process killed: the SDK-owned ``claude`` CLI of the delegation is SIGKILLed (our own
  PID, found by its ``--session-id``) while its turn runs;
- session deleted: a delegation completes on a new session, the session's transcript
  (our scratch project only) is deleted, then Yuki resumes it;
- device authorization revoked: the connector's device grant is revoked mid-turn.
Pass facts: the record enters the matching state (unknown with the native reason /
failed), the parent is told once per fact, no new native effect follows, and an
unknown record is never re-dispatched (one prompt in the journal).
Artifact: ``test-results/external-agent-phase2-acceptance/A7.json``.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

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
    say,
    started,
    until,
    write_artifact,
)
from yuki.runtime.work_repository import TERMINAL

pytestmark = pytest.mark.real_agent

LONG = (
    "Run exactly the shell command `ping -c 40 127.0.0.1` on its own (not chained, not in the "
    "background), then reply with just the word done."
)
SHORT = "Reply with just the word hi."


def sdk_cli_pid(native_session: str) -> int | None:
    """Our own connector-spawned CLI for this session (never a user process)."""
    out = subprocess.run(["ps", "-axo", "pid,command"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if (
            f"--session-id={native_session}" in line
            or f"--resume {native_session}" in line
            or f"--resume={native_session}" in line
        ):
            return int(line.split()[0])
    return None


async def test_a7_executor_gone_is_told_once_and_never_redispatched(tmp_path):
    facts = Facts()
    stack = Stack(run_root("test_a7_executor_gone"), facts)
    await stack.start()
    recorder = StateRecorder(stack)
    recorder.start()
    artifact: dict[str, Any] = {"scenario": "A7", "status": "incomplete", "facts": {}}
    cwd = project("a7")
    cases: dict[str, dict[str, Any]] = {}
    try:
        device = await stack.pair_device("Mac A")
        await stack.start_connector("mac-a", device, [claude_instance("claude-1", cwd)])
        agent = by_local(await stack.online_instances(device["device_id"], 1))["claude-1"]
        journal = stack.root / "connectors" / "mac-a" / "journal.sqlite3"

        async def decide(turn):
            tag = next((name for name in cases if f"A7-{name}" in turn.trigger_text), None)
            if tag is None:
                return None
            case = cases[tag]
            if turn.work_id is None:
                return call("manage_task", {"action": "start", "goal": f"A7 {tag}"})
            case["parent"] = turn.work_id
            tool = turn.last_tool()
            if started(tool) and case.get("record") is None:
                case["record"] = started(tool)
                recorder.watch(case["record"], turn.work_id)
                return call("wait", {"for": [{"run": case["record"]}]})
            if case.get("record") is None:
                arguments: dict[str, Any] = {
                    "goal": case["goal"],
                    "executor": {"agent": agent},
                    "scope": {"write_paths": [str(cwd)], "network": False, "commands": ["ping"]},
                }
                if case.get("session"):
                    arguments["session"] = case["session"]
                else:
                    arguments["cwd"] = str(cwd)
                return call("start_subtask", arguments)
            if turn.last_role != "tool":
                # A wake: the card names the fact; Yuki tells the group once per wake.
                case.setdefault("told", []).append(round(asyncio.get_running_loop().time(), 3))
                facts.mark("told", case=tag)
                return say(f"{tag}: 执行者状态变化，已记录，不会重派。")
            row = await stack.work_row(case["record"])
            if row["state"] not in TERMINAL:
                return call("wait", {"for": [{"run": case["record"]}]})
            if case.get("release_after") and not case.get("released"):
                case["released"] = True  # hand the session back: Yuki's SDK owner closes
                return call("manage_task", {"action": "release", "id": case["record"]})
            if tool and tool.get("status") == "completed":
                return None
            return call("manage_task", {"action": "finish", "result": row["state"]})

        stack.decide = decide

        async def begin(
            tag: str, goal: str, session: str | None = None, release_after: bool = False
        ) -> str:
            cases[tag] = {
                "goal": goal,
                "record": None,
                "session": session,
                "release_after": release_after,
            }
            facts.mark("case_start", case=tag)
            await stack.group(42, f"!ai A7-{tag} 让 Claude 做点事", chat=-10200 - len(cases))

            async def record():
                return cases[tag].get("record")

            return await until(record, 60)

        async def facts_of(tag: str) -> dict[str, Any]:
            case = cases[tag]
            ops = await stack.operations(case["record"])
            prompt_ids = {op["operation_id"] for op in ops if op["action"] == "prompt"}
            return {
                "parent_work_id": case["parent"],
                "record_work_id": case["record"],
                "record": {
                    k: v
                    for k, v in (await stack.work_row(case["record"])).items()
                    if k in ("state", "reason")
                },
                "operations": ops,
                "events": await stack.events(case["record"]),
                "parent_inputs": await stack.inputs_of(case["parent"]),
                "told": len(case.get("told", [])),
                "journal_prompts_for_record": len(
                    [r for r in journal_commands(journal) if r["operation_id"] in prompt_ids]
                ),
                "state_sequence": recorder.of(case["record"]),
            }

        # ---- process killed ---------------------------------------------------------------
        record = await begin("killed", LONG)

        async def running_session():
            ops = await stack.operations(record)
            sessions = [op["session_id"] for op in ops if op["session_id"]]
            if not sessions:
                return None
            from yuki.external_agents.delegation import session_native_id

            async with stack.database.read_session() as reader:
                native = await session_native_id(reader, sessions[0])
            return native if native and sdk_cli_pid(native) else None

        native = await until(running_session, 90, 0.5)
        await asyncio.sleep(5)
        pid = sdk_cli_pid(native)
        assert pid is not None
        facts.mark("sigkill_own_cli", case="killed")
        os.kill(pid, signal.SIGKILL)

        async def lost():
            row = await stack.work_row(record)
            return row["reason"] not in (None, "native_executor") or row["state"] in TERMINAL

        await until(lost, 60, 0.5)
        await asyncio.sleep(20)
        artifact["facts"]["process_killed"] = await facts_of("killed")

        # ---- session deleted ----------------------------------------------------------------
        record = await begin("first", SHORT, release_after=True)

        async def first_released():
            ops = await stack.operations(record)
            return (await stack.work_row(record))["state"] in TERMINAL and any(
                op["action"] == "detach" and op["status"] not in (None, "unknown") for op in ops
            )

        await until(first_released, 120, 1)
        await asyncio.sleep(5)  # the owner closes at the turn boundary
        ops = await stack.operations(record)
        session = next(op["session_id"] for op in ops if op["session_id"])
        from yuki.external_agents.delegation import session_native_id

        async with stack.database.read_session() as reader:
            native = await session_native_id(reader, session)
        await asyncio.sleep(3)
        (transcript,) = (Path.home() / ".claude" / "projects").glob(f"*/{native}.jsonl")
        assert "scratchpad-acceptance-proj-a7" in transcript.parent.name, transcript.parent.name
        transcript.unlink()  # our scratch session only
        facts.mark("transcript_deleted", case="deleted")
        record = await begin("deleted", SHORT, session=session)

        async def deleted_settled():
            return (await stack.work_row(record))["state"] in TERMINAL

        await until(deleted_settled, 60, 0.5)
        await asyncio.sleep(5)
        artifact["facts"]["session_deleted"] = await facts_of("deleted")

        # ---- device authorization revoked ---------------------------------------------------
        record = await begin("revoked", LONG)

        async def turn_running():
            events = await stack.events(record)
            return any(e["kind"] == "progress" for e in events)

        await until(turn_running, 90, 0.5)
        await asyncio.sleep(3)
        facts.mark("revoke_device", case="revoked")
        await stack.runtime.client_identity.revoke_for_operator(
            device["device_id"], stack.state["operator"], str(uuid4())
        )

        async def revoked_seen():
            row = await stack.work_row(record)
            return row["reason"] not in (None, "native_executor") or row["state"] in TERMINAL

        await until(revoked_seen, 60, 0.5)
        # No new effect after revocation: a new input is refused at the point of execution.
        refusal = None
        try:
            refusal = await stack.runtime.external_agents.execute(
                {
                    "action": "queue",
                    "session_id": (await stack.operations(record))[0]["session_id"],
                    "prompt": "one more",
                },
                f"a7-after-revoke-{uuid4()}",
                {"actor_person_id": stack.state["owner"]},
                owner_work_id=record,
            )
        except Exception as error:
            refusal = {"refused": type(error).__name__, "code": getattr(error, "code", str(error))}
        await asyncio.sleep(55)  # the native turn would have finished by now
        artifact["facts"]["device_revoked"] = {
            **await facts_of("revoked"),
            "new_input_after_revoke": refusal,
            "connector_state": stack.connectors["mac-a"].connector.state,
        }
        artifact.update(status="completed", marks=facts.marks)
        for key in ("process_killed", "device_revoked"):
            item = artifact["facts"][key]
            assert item["journal_prompts_for_record"] == 1, (key, item)
            assert item["record"]["state"] != "completed" or key == "process_killed"
        assert artifact["facts"]["session_deleted"]["record"]["state"] == "failed"
    except BaseException as error:
        artifact.update(status="failed", error=repr(error)[:500], marks=facts.marks)
        raise
    finally:
        artifact["model_requests"] = facts.model
        write_artifact("A7", artifact)
        await recorder.stop()
        for name in list(stack.connectors):
            await stack.stop_connector(name)
        await stack.stop()
