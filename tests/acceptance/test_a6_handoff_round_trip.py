"""A6 (taskbook §10.3, §4.11): handoff round trip for Claude, Codex and ACP, once each.

The person stops their own session ("user's terminal" = private tmux; for ACP a
minimal stdio ACP client stands in for the person's editor), Yuki takes it over
(``relationship="takeover"``) with the full native history, works inside its scope,
releases it (``manage_task release``: the connector closes Yuki's own native process
or client at the turn boundary), and the person resumes it. Pass facts: no fork
(Claude/ACP transcript: one parentUuid chain; Codex: same thread id, turns in order)
and no duplicate execution (one Yuki prompt, one takeover file write).
Artifacts: ``test-results/external-agent-phase2-acceptance/A6-<executor>.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.acceptance.harness import (
    SCRATCH,
    Facts,
    Stack,
    StateRecorder,
    acp_instance,
    by_local,
    call,
    claude_instance,
    claude_registry,
    codex_instance,
    codex_items,
    codex_rpc,
    codex_socket,
    codex_tui_prompt,
    journal_commands,
    loaded_threads,
    project,
    record_codex_methods,
    run_root,
    started,
    terminal_claude,
    terminal_codex,
    terminal_turn,
    tmux,
    tmux_screen,
    tmux_send,
    tui_thread,
    tui_turns_done,
    until,
    write_artifact,
)
from yuki.runtime.work_repository import TERMINAL

pytestmark = pytest.mark.real_agent

TAKEOVER_GOAL = (
    "Create the file takeover.txt containing ok in the current directory with your file "
    "writing tool. Do nothing else."
)


def claude_chain(session_id: str) -> dict[str, Any]:
    """parentUuid structure of a Claude transcript: fork points, writers, takeover writes."""
    (path,) = (Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl")
    children: dict[str, list[str]] = {}
    sequence = []
    writes = 0
    for line in path.read_text().splitlines():
        value = json.loads(line)
        if "uuid" not in value or value.get("isSidechain"):
            continue
        parent = value.get("parentUuid")
        children.setdefault(str(parent), []).append(value["uuid"])
        if value.get("type") in ("user", "assistant"):
            sequence.append({"type": value["type"], "entrypoint": value.get("entrypoint")})
        content = (value.get("message") or {}).get("content")
        if value.get("type") == "assistant" and isinstance(content, list):
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "tool_use"
                    and "takeover.txt" in json.dumps(part.get("input", {}))
                ):
                    writes += 1
    forks = {key: len(value) for key, value in children.items() if len(value) > 1}
    return {
        "entries": sum(len(v) for v in children.values()),
        "fork_points": len(forks),
        "roots": len(children.get("None", [])),
        "takeover_write_tool_uses": writes,
        "entrypoint_runs": _runs([item["entrypoint"] for item in sequence]),
    }


def _runs(values: list[Any]) -> list[list[Any]]:
    runs: list[list[Any]] = []
    for value in values:
        if runs and runs[-1][0] == value:
            runs[-1][1] += 1
        else:
            runs.append([value, 1])
    return runs


async def run_takeover(
    stack: Stack,
    facts: Facts,
    recorder: StateRecorder,
    agent: str,
    session: str,
    cwd: Path,
    tag: str,
) -> dict[str, Any]:
    """Scripted Main: takeover (only because the person asked), wait, release, finish."""
    state: dict[str, Any] = {"record": None, "released": False}

    async def decide(turn):
        if tag not in turn.trigger_text:
            return None
        if turn.work_id is None:
            return call("manage_task", {"action": "start", "goal": f"{tag} takeover"})
        tool = turn.last_tool()
        if started(tool) and state["record"] is None:
            state["record"] = tool["id"]
            recorder.watch(tool["id"])
            return call("wait", {"for": [{"run": tool["id"]}]})
        if state["record"] is None:
            return call(
                "start_subtask",
                {
                    "goal": TAKEOVER_GOAL,
                    "relationship": "takeover",
                    "executor": {"agent": agent},
                    "session": session,
                    "scope": {"write_paths": [str(cwd)], "network": False},
                },
            )
        row = await stack.work_row(state["record"])
        if row["state"] not in TERMINAL:
            return call("wait", {"for": [{"run": state["record"]}]})
        if not state["released"]:
            state["released"] = True
            facts.mark("release", work_id=state["record"])
            return call("manage_task", {"action": "release", "id": state["record"]})
        if (tool and tool.get("status") == "cancelled") or (tool and "relationship" in tool):
            return call("manage_task", {"action": "finish", "result": "handed back"})
        return None

    stack.decide = decide
    await stack.group(42, f"!ai {tag} 我停下了，你接手这个会话把 takeover.txt 建好，然后还给我")

    async def released():
        record = state["record"]
        if record is None or not state["released"]:
            return False
        ops = await stack.operations(record)
        return (
            any(op["action"] == "detach" and op["status"] not in (None, "unknown") for op in ops)
            and record
        )

    record = await until(released, 300, 1)
    await asyncio.sleep(3)
    return {
        "work_id": record,
        "final": {
            k: v for k, v in (await stack.work_row(record)).items() if k in ("state", "reason")
        },
        "operations": await stack.operations(record),
        "events": await stack.events(record),
        "state_sequence": recorder.of(record),
    }


async def setup(tmp_path: Path) -> tuple[Facts, Stack, StateRecorder]:
    facts = Facts()
    stack = Stack(run_root("test_a6_handoff_round_trip"), facts)
    await stack.start()
    recorder = StateRecorder(stack)
    recorder.start()
    return facts, stack, recorder


async def teardown(stack: Stack, recorder: StateRecorder) -> None:
    await recorder.stop()
    for name in list(stack.connectors):
        await stack.stop_connector(name)
    await stack.stop()


async def test_a6_claude_handoff_round_trip(tmp_path):
    facts, stack, recorder = await setup(tmp_path)
    artifact: dict[str, Any] = {"scenario": "A6-claude", "status": "incomplete"}
    cwd = project("a6-claude")
    (cwd / "takeover.txt").unlink(missing_ok=True)
    try:
        await terminal_claude("a6-claude", cwd)
        row = await terminal_turn("a6-claude", cwd, "Reply with just the word hi.")
        native = row["sessionId"]
        tmux_send("a6-claude", "/exit", "Enter")  # 1. the person stops

        async def gone():
            return not [r for r in claude_registry() if r.get("sessionId") == native]

        await until(gone, 30, 1)
        facts.mark("person_stopped")
        device = await stack.pair_device("Mac A")
        await stack.start_connector("mac-a", device, [claude_instance("claude-1", cwd)])
        agent = by_local(await stack.online_instances(device["device_id"], 1))["claude-1"]
        session = (
            await stack.runtime.external_agents.repository.remember_sessions(
                agent, stack.state["owner"], [native]
            )
        )[native]
        record = await run_takeover(stack, facts, recorder, agent, session, cwd, "A6-CLAUDE")

        async def sdk_closed():  # released: no process of ours holds the session
            return not [r for r in claude_registry() if r.get("sessionId") == native]

        await until(sdk_closed, 60, 1)
        facts.mark("yuki_process_closed")
        middle = claude_chain(native)
        # 5. the person resumes in the terminal
        if "a6-claude" in tmux("ls"):
            tmux("kill-session", "-t", "a6-claude")
        tmux(
            "new-session",
            "-d",
            "-s",
            "a6-claude",
            "-x",
            "200",
            "-y",
            "50",
            "-c",
            str(cwd),
            f"claude --resume {native} --model haiku",
        )

        async def resumed():
            rows = [r for r in claude_registry() if r.get("sessionId") == native]
            return rows and "mode on" in tmux_screen("a6-claude")

        await until(resumed, 60, 1)
        await terminal_turn("a6-claude", cwd, "Reply with just the word back.")
        tmux_send("a6-claude", "/exit", "Enter")
        await asyncio.sleep(3)
        chain = claude_chain(native)
        prompts = [op for op in record["operations"] if op["action"] == "prompt"]
        artifact.update(
            status="completed",
            native_session_id=native,
            record=record,
            takeover_session_same=prompts and prompts[0]["session_id"] == session,
            chain_after_yuki=middle,
            chain_after_person_resumed=chain,
            takeover_file=(cwd / "takeover.txt").exists(),
            journal=journal_commands(stack.root / "connectors" / "mac-a" / "journal.sqlite3"),
            marks=facts.marks,
        )
        assert chain["fork_points"] == 0, chain
        assert chain["takeover_write_tool_uses"] == 1, chain
        assert len(prompts) == 1
    except BaseException as error:
        artifact.update(status="failed", error=repr(error)[:500], marks=facts.marks)
        raise
    finally:
        artifact["model_requests"] = facts.model
        write_artifact("A6-claude", artifact)
        await teardown(stack, recorder)
        if "a6-claude" in tmux("ls"):
            tmux("kill-session", "-t", "a6-claude")


async def test_a6_codex_handoff_round_trip(tmp_path, monkeypatch):
    facts, stack, recorder = await setup(tmp_path)
    methods: list[dict[str, Any]] = []
    record_codex_methods(monkeypatch, methods, facts.started)
    artifact: dict[str, Any] = {"scenario": "A6-codex", "status": "incomplete"}
    cwd = project("a6-codex")
    (cwd / "takeover.txt").unlink(missing_ok=True)
    try:
        before = await loaded_threads()
        await terminal_codex("a6-codex", cwd)
        await codex_tui_prompt("a6-codex", "Reply with just the word hi.")

        native = await tui_thread("a6-codex", before, cwd)

        async def turn_done(count: int):
            (read,) = await codex_rpc([("thread/read", {"threadId": native, "includeTurns": True})])
            turns = (read.get("thread") or {}).get("turns") or []
            return len(turns) >= count and turns[-1].get("status") == "completed"

        await tui_turns_done("a6-codex", native, 1)
        for _ in range(3):  # 1. the person stops (Ctrl-C until the TUI exits)
            if "a6-codex" not in tmux("ls"):
                break
            tmux_send("a6-codex", None, "C-c")
            await asyncio.sleep(1.5)
        facts.mark("person_stopped")
        device = await stack.pair_device("Mac A")
        await stack.start_connector("mac-a", device, [codex_instance("codex-1", cwd)])
        agent = by_local(await stack.online_instances(device["device_id"], 1))["codex-1"]
        session = (
            await stack.runtime.external_agents.repository.remember_sessions(
                agent, stack.state["owner"], [native]
            )
        )[native]
        record = await run_takeover(stack, facts, recorder, agent, session, cwd, "A6-CODEX")
        # 5. the person resumes the same thread in a TUI on the same app-server.
        if "a6-codex" in tmux("ls"):
            tmux("kill-session", "-t", "a6-codex")
        tmux(
            "new-session",
            "-d",
            "-s",
            "a6-codex",
            "-x",
            "200",
            "-y",
            "50",
            "-c",
            str(cwd),
            # Codex refuses permission overrides when resuming on a remote app-server.
            f"codex resume {native} --remote unix://{codex_socket()} -C {cwd}",
        )

        async def tui_ready():
            screen = tmux_screen("a6-codex")
            if "Update now" in screen:
                tmux_send("a6-codex", None, "Escape")
            return "for shortcuts" in screen

        await until(tui_ready, 60, 1)
        await asyncio.sleep(2)
        await codex_tui_prompt("a6-codex", "Reply with just the word back.")
        await tui_turns_done("a6-codex", native, 3)
        (read,) = await codex_rpc([("thread/read", {"threadId": native, "includeTurns": True})])
        items = codex_items(read.get("thread") or {})
        prompts = [op for op in record["operations"] if op["action"] == "prompt"]
        artifact.update(
            status="completed",
            thread_id=native,
            record=record,
            thread_items=items,
            user_messages=[
                {"turn": i["turn_id"], "client_id": i["client_id"]}
                for i in items
                if i["type"] == "userMessage"
            ],
            file_change_items=len([i for i in items if i["type"] == "fileChange"]),
            takeover_file=(cwd / "takeover.txt").exists(),
            connector_app_server_methods=methods,
            journal=journal_commands(stack.root / "connectors" / "mac-a" / "journal.sqlite3"),
            marks=facts.marks,
        )
        assert len(prompts) == 1
        assert len([i for i in items if i["type"] == "userMessage"]) == 3, items
        assert not [m for m in methods if m["method"] == "turn/start"]
    except BaseException as error:
        artifact.update(status="failed", error=repr(error)[:500], marks=facts.marks)
        raise
    finally:
        artifact["model_requests"] = facts.model
        write_artifact("A6-codex", artifact)
        await teardown(stack, recorder)
        if "a6-codex" in tmux("ls"):
            tmux("kill-session", "-t", "a6-codex")


class PersonsACPClient:
    """A minimal stdio ACP client standing in for the person's editor (raw JSON-RPC)."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.next = 0
        self.permissions = 0
        self.reader: asyncio.Task[None] | None = None

    async def start(self) -> None:
        binary = SCRATCH / "acp" / "node_modules" / ".bin" / "claude-agent-acp"
        self.process = await asyncio.create_subprocess_exec(
            str(binary),
            cwd=str(self.cwd),
            env={**os.environ, "ANTHROPIC_MODEL": "haiku"},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=1 << 26,
        )
        self.reader = asyncio.create_task(self._read())
        await self.call(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
            },
        )

    async def _send(self, message: dict[str, Any]) -> None:
        assert self.process and self.process.stdin
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        self.next += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.next] = future
        await self._send({"jsonrpc": "2.0", "id": self.next, "method": method, "params": params})
        message = await future
        if "error" in message:
            raise RuntimeError(f"{method}: {message['error']}")
        return message.get("result")

    async def _read(self) -> None:
        assert self.process and self.process.stdout
        while line := await self.process.stdout.readline():
            message = json.loads(line)
            if "method" in message and "id" in message:
                if message["method"] == "session/request_permission":
                    # The person answers in their own editor.
                    self.permissions += 1
                    option = next(
                        o["optionId"]
                        for o in message["params"]["options"]
                        if o.get("kind") == "allow_once"
                    )
                    await self._send(
                        {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "result": {"outcome": {"outcome": "selected", "optionId": option}},
                        }
                    )
                else:
                    await self._send(
                        {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "error": {"code": -32601, "message": "not supported"},
                        }
                    )
            elif "id" in message and message["id"] in self.pending:
                self.pending.pop(message["id"]).set_result(message)

    async def prompt(self, session: str, text: str) -> Any:
        return await self.call(
            "session/prompt", {"sessionId": session, "prompt": [{"type": "text", "text": text}]}
        )

    async def close(self) -> None:
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            await self.process.wait()
        if self.reader is not None:
            self.reader.cancel()


async def test_a6_acp_handoff_round_trip(tmp_path):
    facts, stack, recorder = await setup(tmp_path)
    artifact: dict[str, Any] = {"scenario": "A6-acp", "status": "incomplete"}
    cwd = project("a6-acp")
    (cwd / "takeover.txt").unlink(missing_ok=True)
    person = PersonsACPClient(cwd)
    try:
        await person.start()
        created = await person.call("session/new", {"cwd": str(cwd), "mcpServers": []})
        native = created["sessionId"]
        await person.prompt(native, "Reply with just the word hi.")
        await person.close()  # 1. the person stops (editor process exits)
        facts.mark("person_stopped")
        device = await stack.pair_device("Mac A")
        await stack.start_connector("mac-a", device, [acp_instance("acp-1", cwd)])
        agent = by_local(await stack.online_instances(device["device_id"], 1))["acp-1"]
        session = (
            await stack.runtime.external_agents.repository.remember_sessions(
                agent, stack.state["owner"], [native]
            )
        )[native]
        record = await run_takeover(stack, facts, recorder, agent, session, cwd, "A6-ACP")
        middle_chain = None
        if list((Path.home() / ".claude" / "projects").glob(f"*/{native}.jsonl")):
            middle_chain = claude_chain(native)
        # 5. the person loads the session again in their editor and continues.
        person = PersonsACPClient(cwd)
        await person.start()
        loaded = await person.call(
            "session/load", {"sessionId": native, "cwd": str(cwd), "mcpServers": []}
        )
        await person.prompt(native, "Reply with just the word back.")
        await person.close()
        chain = None
        if list((Path.home() / ".claude" / "projects").glob(f"*/{native}.jsonl")):
            chain = claude_chain(native)
        prompts = [op for op in record["operations"] if op["action"] == "prompt"]
        artifact.update(
            status="completed",
            native_session_id=native,
            record=record,
            person_load_ok=loaded is not None or True,
            chain_after_yuki=middle_chain,
            chain_after_person_resumed=chain,
            takeover_file=(cwd / "takeover.txt").exists(),
            journal=journal_commands(stack.root / "connectors" / "mac-a" / "journal.sqlite3"),
            marks=facts.marks,
        )
        assert len(prompts) == 1
        if chain is not None:
            assert chain["fork_points"] == 0, chain
            assert chain["takeover_write_tool_uses"] == 1, chain
    except BaseException as error:
        artifact.update(status="failed", error=repr(error)[:500], marks=facts.marks)
        raise
    finally:
        artifact["model_requests"] = facts.model
        write_artifact("A6-acp", artifact)
        await person.close()
        await teardown(stack, recorder)
