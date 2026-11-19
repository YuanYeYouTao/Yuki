"""A5 (taskbook §10.3): a Claude session live in "the user's terminal" is never resumed.

A real Claude Code 2.1.311 runs in a private tmux server (``tmux -L p2acc``) in a
scratch project. Yuki delegates to that session: the connector reads the session
registry, the record fails with ``session_live_elsewhere`` and its options; Yuki then
forks explicitly (``fork=true``): a new native session completes and the original
transcript is byte-identical (sha256, line count) before and after.
Artifact: ``test-results/external-agent-phase2-acceptance/A5.json``.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import pytest

from tests.acceptance.harness import (
    Facts,
    Stack,
    StateRecorder,
    by_local,
    call,
    claude_instance,
    claude_registry,
    project,
    run_root,
    started,
    terminal_claude,
    terminal_turn,
    tmux,
    tmux_send,
    until,
    write_artifact,
)
from yuki.runtime.work_repository import TERMINAL

pytestmark = pytest.mark.real_agent


def transcript(session_id: str) -> Path:
    found = list((Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"))
    assert len(found) == 1, found
    return found[0]


def digest(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {"sha256": hashlib.sha256(data).hexdigest(), "lines": data.count(b"\n")}


async def test_a5_live_terminal_session_is_refused_and_fork_leaves_it_unchanged(tmp_path):
    facts = Facts()
    stack = Stack(run_root("test_a5_claude_live_session"), facts)
    await stack.start()
    recorder = StateRecorder(stack)
    recorder.start()
    artifact: dict[str, Any] = {"scenario": "A5", "status": "incomplete"}
    cwd = project("a5")
    terminal = "a5-terminal"
    state: dict[str, Any] = {"records": [], "outcomes": []}
    try:
        await terminal_claude(terminal, cwd)
        row = await terminal_turn(terminal, cwd, "Reply with just the word hi.")
        native = row["sessionId"]
        before = digest(transcript(native))
        artifact.update(
            terminal={
                "native_session_id": native,
                "registry_status": row["status"],
                "pid_is_ours": True,
            },
            transcript_before=before,
        )
        device = await stack.pair_device("Mac A")
        await stack.start_connector("mac-a", device, [claude_instance("claude-1", cwd)])
        agent = by_local(await stack.online_instances(device["device_id"], 1))["claude-1"]
        # The same mapping `get_tasks(agents={agent})` creates, for this one native ID only:
        # the profile-wide list would read the user's real sessions, which we never touch.
        mapping = await stack.runtime.external_agents.repository.remember_sessions(
            agent, stack.state["owner"], [native]
        )
        session = mapping[native]

        async def decide(turn):
            if "A5-RESUME" not in turn.trigger_text:
                return None
            if turn.work_id is None:
                return call("manage_task", {"action": "start", "goal": "A5"})
            tool = turn.last_tool()
            # Records are read from the Work tree, not from tool results: a refused start
            # wakes this Work with its card before the tool result is shown again.
            for child in await stack.children_of(turn.work_id):
                if child["work_id"] not in state["records"]:
                    state["records"].append(child["work_id"])
                    recorder.watch(child["work_id"])
            if started(tool) and started(tool) not in state["records"]:
                state["records"].append(tool["id"])
                recorder.watch(tool["id"])
                return call("wait", {"for": [{"run": tool["id"]}]})
            if not state["records"]:
                return call(
                    "start_subtask",
                    {
                        "goal": "Reply with just the word hi.",
                        "executor": {"agent": agent},
                        "session": session,
                    },
                )
            last = await stack.work_row(state["records"][-1])
            if last["state"] not in TERMINAL:
                return call("wait", {"for": [{"run": state["records"][-1]}]})
            if len(state["records"]) == 1:
                # The card offered fork / handoff; the scripted model chooses fork.
                return call(
                    "start_subtask",
                    {
                        "goal": "Reply with just the word forked.",
                        "executor": {"agent": agent},
                        "session": session,
                        "fork": True,
                    },
                )
            if not state.get("finished"):
                state["finished"] = True
                return call("manage_task", {"action": "finish", "result": "A5 done"})
            return None

        stack.decide = decide
        await stack.group(42, "!ai A5-RESUME 继续我终端里的那个会话")

        async def two_records_terminal():
            if len(state["records"]) < 2:
                return False
            rows = [await stack.work_row(item) for item in state["records"]]
            return all(item["state"] in TERMINAL for item in rows)

        await until(two_records_terminal, 300)
        await asyncio.sleep(2)
        refused, forked = state["records"]
        refused_ops = await stack.operations(refused)
        forked_ops = await stack.operations(forked)
        after = digest(transcript(native))
        fork_native = None
        if forked_ops and forked_ops[0]["session_id"]:
            from yuki.external_agents.delegation import session_native_id

            async with stack.database.read_session() as reader:
                fork_native = await session_native_id(reader, forked_ops[0]["session_id"])
        still_live = [r for r in claude_registry() if r.get("sessionId") == native]
        artifact.update(
            status="completed",
            refused={
                "work_id": refused,
                "final": {
                    k: v
                    for k, v in (await stack.work_row(refused)).items()
                    if k in ("state", "reason")
                },
                "operations": refused_ops,
                "events": await stack.events(refused),
                "card": await stack.card(refused),
                "state_sequence": recorder.of(refused),
            },
            forked={
                "work_id": forked,
                "final": {
                    k: v
                    for k, v in (await stack.work_row(forked)).items()
                    if k in ("state", "reason")
                },
                "operations": forked_ops,
                "events": await stack.events(forked),
                "native_session_id": fork_native,
                "state_sequence": recorder.of(forked),
            },
            transcript_after=after,
            original_unchanged=before == after,
            fork_is_new_session=fork_native is not None and fork_native != native,
            terminal_still_live=bool(still_live),
            marks=facts.marks,
        )
        assert (
            refused_ops
            and refused_ops[0]["error"]
            in (
                "session_live_elsewhere",
                {"code": "session_live_elsewhere"},
            )
        ) or "session_live_elsewhere" in str(refused_ops), refused_ops
        assert artifact["original_unchanged"], (before, after)
        assert artifact["fork_is_new_session"]
    except BaseException as error:
        artifact.update(status="failed", error=repr(error)[:500], marks=facts.marks)
        raise
    finally:
        artifact["model_requests"] = facts.model
        write_artifact("A5", artifact)
        await recorder.stop()
        for name in list(stack.connectors):
            await stack.stop_connector(name)
        await stack.stop()
        tmux_send(terminal, "/exit", "Enter")
        await asyncio.sleep(2)
        tmux("kill-session", "-t", terminal) if terminal in tmux("ls") else None
