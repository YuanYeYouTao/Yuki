"""A2 (taskbook §10.3, D6): observe never injects input and never answers.

"The user's terminal" is a private tmux server (``tmux -L p2acc``): a real Claude Code
2.1.311 session (manual permission mode) and a real Codex 0.169 TUI attached with
``codex --remote`` to OUR app-server. Yuki observes both (``start_subtask``,
``relationship="observe"``, from a plain chat turn with no current Work). Each session
then hits a permission prompt. Pass facts: one notice Work per request whose scripted
``send`` tells the group; the observation cards say ``answer_at: native_terminal``;
the Client API answer is refused (``observe_only``); the person answers in tmux; the
connector journal and the server hold only watch/detach for those sessions; the
Claude transcript and the Codex thread contain only the terminal's own inputs.
Artifact: ``test-results/external-agent-phase2-acceptance/A2.json``.
"""

from __future__ import annotations

import asyncio
import json
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
    claude_registry,
    codex_instance,
    codex_items,
    codex_rpc,
    codex_tui_prompt,
    journal_commands,
    loaded_threads,
    project,
    run_root,
    say,
    started,
    terminal_claude,
    terminal_codex,
    terminal_turn,
    tmux,
    tmux_send,
    tui_thread,
    tui_turns_done,
    until,
    write_artifact,
)

pytestmark = pytest.mark.real_agent

INPUT_ACTIONS = {"prompt", "queue", "steer", "respond", "cancel"}


def claude_user_entries(session_id: str) -> list[dict[str, Any]]:
    """User-typed entries of the transcript (no tool results): structure only."""
    (path,) = (Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl")
    rows = []
    for line in path.read_text().splitlines():
        value = json.loads(line)
        if value.get("type") != "user":
            continue
        content = (value.get("message") or {}).get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "tool_result" for part in content
        ):
            continue
        rows.append(
            {
                "uuid": value.get("uuid"),
                "entrypoint": value.get("entrypoint"),
                "promptSource": value.get("promptSource"),
                "userType": value.get("userType"),
            }
        )
    return rows


async def test_a2_observe_tells_the_user_and_injects_nothing(tmp_path):
    facts = Facts()
    stack = Stack(run_root("test_a2_observe_regression"), facts)
    await stack.start()
    recorder = StateRecorder(stack)
    recorder.start()
    artifact: dict[str, Any] = {"scenario": "A2", "status": "incomplete"}
    claude_cwd, codex_cwd = project("a2-claude"), project("a2-codex")
    state: dict[str, Any] = {"observations": {}, "notices": [], "plan": []}
    try:
        await terminal_claude("a2-claude", claude_cwd, manual=True)
        claude_row = await terminal_turn("a2-claude", claude_cwd, "Reply with just the word hi.")
        claude_native = claude_row["sessionId"]
        before_threads = await loaded_threads()
        await terminal_codex("a2-codex", codex_cwd)
        await codex_tui_prompt("a2-codex", "Reply with just the word hi.")

        codex_native = await tui_thread("a2-codex", before_threads, codex_cwd)

        await tui_turns_done("a2-codex", codex_native, 1)

        device = await stack.pair_device("Mac A")
        await stack.start_connector(
            "mac-a",
            device,
            [claude_instance("claude-1", claude_cwd), codex_instance("codex-1", codex_cwd)],
        )
        ids = by_local(await stack.online_instances(device["device_id"], 2))
        repository = stack.runtime.external_agents.repository
        owner = stack.state["owner"]
        claude_session = (
            await repository.remember_sessions(ids["claude-1"], owner, [claude_native])
        )[claude_native]
        codex_session = (await repository.remember_sessions(ids["codex-1"], owner, [codex_native]))[
            codex_native
        ]
        targets = [
            ("claude", ids["claude-1"], claude_session),
            ("codex", ids["codex-1"], codex_session),
        ]

        async def decide(turn):
            current = turn.control.current if turn.control else None
            if current and str(current.get("goal", "")).startswith("告诉用户观察记录"):
                if turn.last_role == "tool":
                    return call("manage_task", {"action": "finish", "result": "told"})
                state["notices"].append(
                    {"work_id": current["id"], "t": round(asyncio.get_running_loop().time(), 3)}
                )
                facts.mark("notice_activation", work_id=current["id"])
                return say("你在终端里的会话在等你批准，请直接在终端处理。")
            if "A2-OBSERVE" in turn.trigger_text:
                tool = turn.last_tool()
                if started(tool) and started(tool) not in state["observations"].values():
                    name = targets[len(state["observations"])][0]
                    state["observations"][name] = tool["id"]
                    recorder.watch(tool["id"])
                if len(state["observations"]) < len(targets):
                    name, agent, session = targets[len(state["observations"])]
                    return call(
                        "start_subtask",
                        {
                            "goal": f"观察我终端里的 {name} 会话",
                            "relationship": "observe",
                            "executor": {"agent": agent},
                            "session": session,
                        },
                    )
                if turn.last_role == "tool" and not state.get("confirmed"):
                    state["confirmed"] = True
                    return say("好，我只看着，需要你处理时提醒你。")
                return None
            if "A2-STOP" in turn.trigger_text:
                tool = turn.last_tool()
                stopped = state.setdefault("stopped", [])
                if tool and tool.get("status") == "cancelled":
                    stopped.append(tool["id"])
                pending = [i for i in state["observations"].values() if i not in stopped]
                if pending:
                    return call("manage_task", {"action": "cancel", "id": pending[0]})
                return None
            return None

        stack.decide = decide
        await stack.group(42, "!ai A2-OBSERVE 帮我看着终端里的两个会话")

        async def observing():
            return len(state["observations"]) == 2

        await until(observing, 60)
        await asyncio.sleep(3)
        facts.mark("observing")

        # The person's own sessions hit permission prompts.
        tmux_send("a2-claude", "Run the shell command: touch made-by-terminal.txt", "Enter")
        await codex_tui_prompt(
            "a2-codex",
            "Run the shell command echo hi, requesting escalated sandbox permissions "
            "(require_escalated) so that I must approve it.",
        )
        facts.mark("terminal_prompts_typed")

        async def requests_seen():
            seen = {}
            for name, identity in state["observations"].items():
                events = [e for e in await stack.events(identity) if e["kind"] == "permission"]
                if events:
                    seen[name] = events[0]
            return seen if len(seen) == 2 else None

        await until(requests_seen, 180, 1)

        async def notices_sent():
            if len(state["notices"]) < 2:
                return False
            rows = [await stack.work_row(n["work_id"]) for n in state["notices"]]
            return all(row["state"] == "completed" for row in rows)

        await until(notices_sent, 60)
        cards = {
            name: await stack.card(identity) for name, identity in state["observations"].items()
        }
        # Even the device's person cannot answer an observed request through Yuki.
        refusals = {}
        for name, identity in state["observations"].items():
            pending = cards[name]["pending"]
            if pending:
                response = await stack.http_client.post(
                    f"/client/v1/works/{identity}/native-requests/"
                    f"{pending[0]['permission_id']}/answer",
                    headers=stack.client_headers,
                    json={"request_id": str(uuid4()), "option_id": "deny"},
                )
                refusals[name] = {
                    "status": response.status_code,
                    "code": response.json().get("problem", {}).get("code"),
                }
        facts.mark("user_answers_in_terminal")
        # The person answers in the terminal (Claude: 1. Yes; Codex: y).
        claude_waiting = [r for r in claude_registry() if r.get("sessionId") == claude_native]
        tmux_send("a2-claude", None, "Enter")
        tmux_send("a2-codex", "y")

        async def resolved():
            done = {}
            for name, identity in state["observations"].items():
                events = await stack.events(identity)
                # Claude: registry left "waiting"; Codex: app-server serverRequest/resolved.
                done[name] = any(
                    e["permission_resolved"] or e["native_method"] == "serverRequest/resolved"
                    for e in events
                )
            return all(done.values())

        await until(resolved, 120, 1)
        await asyncio.sleep(10)
        cards_after_answer = {
            name: await stack.card(identity) for name, identity in state["observations"].items()
        }
        # Stop observing: only Yuki's watch ends; nothing native is cancelled.
        await stack.group(42, "!ai A2-STOP 不用看了")

        async def stopped():
            rows = [await stack.work_row(i) for i in state["observations"].values()]
            return all(row["state"] == "cancelled" for row in rows)

        await until(stopped, 60)
        await asyncio.sleep(3)

        journal = journal_commands(stack.root / "connectors" / "mac-a" / "journal.sqlite3")
        observed_sessions = {claude_session, codex_session, claude_native, codex_native}
        journal_on_observed = [
            row
            for row in journal
            if row["session_id"] in observed_sessions or row["action"] in INPUT_ACTIONS
        ]
        server_ops = {
            name: await stack.operations(identity)
            for name, identity in state["observations"].items()
        }
        (thread,) = await codex_rpc(
            [("thread/read", {"threadId": codex_native, "includeTurns": True})]
        )
        items = codex_items(thread.get("thread") or {})
        user_items = [item for item in items if item["type"] == "userMessage"]
        claude_users = claude_user_entries(claude_native)
        notice_inputs = []
        for notice in state["notices"]:
            for row in await stack.inputs_of(notice["work_id"]):
                notice_inputs.append({**row, "work_id": notice["work_id"]})
        artifact.update(
            status="completed",
            agents={
                "claude": "Claude Code 2.1.311 (terminal, haiku, manual mode)",
                "codex": "codex-cli 0.169 TUI --remote to own app-server, gpt-6-luna low",
            },
            native={
                "claude_session": claude_native,
                "codex_thread": codex_native,
                "claude_waiting_row_status": [r.get("status") for r in claude_waiting],
            },
            observations={
                name: {
                    "work_id": identity,
                    "final": {
                        k: v
                        for k, v in (await stack.work_row(identity)).items()
                        if k in ("state", "reason", "parent_work_id")
                    },
                    "card_while_waiting": cards[name],
                    "events": await stack.events(identity),
                    "operations": server_ops[name],
                    "state_sequence": recorder.of(identity),
                }
                for name, identity in state["observations"].items()
            },
            notices=state["notices"],
            pending_after_terminal_answer={n: c["pending"] for n, c in cards_after_answer.items()},
            notice_inputs=notice_inputs,
            client_answer_refusals=refusals,
            journal=journal,
            journal_input_or_observed_rows=journal_on_observed,
            codex_thread_items=items,
            codex_user_messages=len(user_items),
            codex_user_messages_with_client_id=[i for i in user_items if i["client_id"]],
            claude_user_entries=claude_users,
            marks=facts.marks,
        )
        assert all(r["action"] in {"watch", "detach"} for r in journal_on_observed), journal
        assert all(
            op["action"] in {"watch", "detach"} for ops in server_ops.values() for op in ops
        ), server_ops
        assert len(user_items) == 2, items  # the two prompts typed in the TUI
        assert len(claude_users) == 2 and all(
            u["entrypoint"] in (None, "cli") for u in claude_users
        ), claude_users
        assert all(r["code"] == "observe_only" for r in refusals.values()), refusals
        assert len(state["notices"]) == 2
    except BaseException as error:
        artifact.update(status="failed", error=repr(error)[:500], marks=facts.marks)
        raise
    finally:
        artifact["model_requests"] = facts.model
        write_artifact("A2", artifact)
        await recorder.stop()
        for name in list(stack.connectors):
            await stack.stop_connector(name)
        await stack.stop()
        for session in ("a2-claude", "a2-codex"):
            if session in tmux("ls"):
                tmux("kill-session", "-t", session)
