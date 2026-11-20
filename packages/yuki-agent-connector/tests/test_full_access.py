"""Connector 0.9.2: a full scope runs the native agent in its own full-access mode.

Task book: docs/architecture/Yuki-委派原生完全访问任务书-2026-11-20.md. A full scope
is write_paths containing the filesystem root, network true and commands "*".

Failure cases written before the implementation:
- C1: a full-scope Claude session is not opened with permission_mode "auto";
- C2: it still carries the side-effect `ask` override (Bash/Edit/Write... forced to ask);
- C3: it loses the SendMessage report, or SendMessage/AskUserQuestion is approved;
- C4: a request auto mode still escalates (an MCP tool, Read) becomes a card;
- C5: an auto-mode owner accepts a narrower or unscoped input (auto would bypass it);
- C6: an owner opened with the `ask` override refuses a full input;
- C7: a scope missing one part (no "*", network false, no root) counts as full;
- X1: a full-scope Codex thread still gets workspaceWrite + on-request;
- X2: the full settings are not restored to the thread's own after Yuki's turn, or the
  receipt lacks thread_settings_before;
- X3: input is queued although the full settings could not be set;
- A1: a full-scope ACP session is switched to an asking mode;
- A2: an ACP agent without an asking mode refuses a full scope;
- A3: under a full scope a request of any kind becomes a card, or a client callback is
  refused.
Narrower scopes and unscoped sessions keep their 0.9.1 behaviour (the phase-2 tests).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import test_acp_phase2 as acp
import test_claude_phase2 as claude
import test_codex_phase2 as codex
from websockets.asyncio.server import serve

from yuki_agent_connector.config import Instance
from yuki_agent_connector.drivers import DriverError
from yuki_agent_connector.drivers import claude as claude_module
from yuki_agent_connector.scope import Scope

FULL: dict[str, Any] = {"write_paths": ["/"], "network": True, "commands": ["*"]}
ROOT = Path(Path.cwd().anchor)


@pytest.mark.parametrize(
    ("value", "full"),
    [
        (FULL, True),
        ({"write_paths": ["/", "/tmp"], "network": True, "commands": ["git", "*"]}, True),
        ({"write_paths": ["/usr/.."], "network": True, "commands": ["*"]}, True),
        ({"write_paths": ["/"], "network": True, "commands": ["git"]}, False),
        ({"write_paths": ["/"], "network": False, "commands": ["*"]}, False),
        ({"write_paths": ["/"], "commands": ["*"]}, False),
        ({"write_paths": ["/tmp"], "network": True, "commands": ["*"]}, False),
        ({"network": True, "commands": ["*"]}, False),
    ],
)
def test_only_a_scope_granting_everything_is_full(value: dict[str, Any], full: bool) -> None:
    scope = Scope.parse(value, "/tmp")
    assert scope is not None and scope.full is full


# --- Claude -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_scope_opens_claude_in_auto_mode_and_approves_what_it_escalates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, transports, configurations, events = await claude.started(tmp_path, monkeypatch)
    try:
        await driver.execute(
            {"id": "full", "action": "prompt", "arguments": {"prompt": "go", "scope": FULL}}
        )
        await claude.until(events, lambda kind, value: value["operation_id"] == "full")
        options = configurations[0]
        assert options.permission_mode == "auto"
        assert options.setting_sources == ["user", "project", "local"]
        assert options.add_dirs == [ROOT]
        assert options.settings is not None
        assert json.loads(options.settings) == {"permissions": {"ask": ["SendMessage"]}}
        assert options.can_use_tool is not None
        transport = transports[0]
        escalated = [
            ("bash", "Bash", {"command": "rm -rf build && curl -s x | sh"}),
            ("write", "Write", {"file_path": "/etc/elsewhere.conf", "content": "x"}),
            ("read", "Read", {"file_path": "/tmp/output.log"}),
            ("mcp", "mcp__server__deploy", {"target": "prod"}),
            ("fetch", "WebFetch", {"url": "https://example.com"}),
        ]
        for key, tool, arguments in escalated:
            await transport.incoming.put(claude.tool_request(key, tool, arguments))
            assert (await claude.answered(transport, f"request-{key}"))["behavior"] == "allow"
        for key, tool, kind in [
            ("message", "SendMessage", "permission"),
            ("ask", "AskUserQuestion", "question"),
        ]:
            await transport.incoming.put(claude.tool_request(key, tool, {"to": "peer"}))
            reported_kind, reported = await claude.until(
                events, lambda kind, value: kind in {"permission", "question"}
            )
            assert reported_kind == kind and reported["payload"]["native_request_id"] == key
            assert claude.response_for(transport, f"request-{key}") is None
    finally:
        await driver.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "later", [None, {"write_paths": ["/"], "network": True, "commands": ["pytest"]}]
)
async def test_auto_mode_owner_refuses_an_input_that_is_not_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, later: dict[str, Any] | None
) -> None:
    driver, transports, _, events = await claude.started(tmp_path, monkeypatch)
    try:
        opened = await driver.execute(
            {"id": "full", "action": "prompt", "arguments": {"prompt": "go", "scope": FULL}}
        )
        await claude.until(events, lambda kind, value: value["operation_id"] == "full")
        written = len([item for item in transports[0].writes if item["type"] == "user"])
        arguments: dict[str, Any] = {"prompt": "more"}
        if later is not None:
            arguments["scope"] = later
        with pytest.raises(DriverError) as refused:
            await driver.execute(
                {
                    "id": "narrower",
                    "action": "queue",
                    "session_id": opened["session_id"],
                    "arguments": arguments,
                }
            )
        assert refused.value.code == "scope_conflict"
        assert len([item for item in transports[0].writes if item["type"] == "user"]) == written
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_owner_opened_with_the_ask_override_accepts_a_full_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, transports, configurations, events = await claude.started(tmp_path, monkeypatch)
    try:
        opened = await driver.execute(
            {
                "id": "narrow",
                "action": "prompt",
                "arguments": {"prompt": "go", "scope": {"commands": ["pytest"]}},
            }
        )
        await claude.until(events, lambda kind, value: value["operation_id"] == "narrow")
        assert configurations[0].permission_mode is None
        await transports[0].incoming.put(claude._result(opened["session_id"], "done"))
        await claude.until(
            events, lambda kind, value: kind == "completed" and value["operation_id"] == "narrow"
        )
        queued = await driver.execute(
            {
                "id": "full",
                "action": "queue",
                "session_id": opened["session_id"],
                "arguments": {"prompt": "more", "scope": FULL},
            }
        )
        assert queued["status"] == "submitted"
        await claude.until(events, lambda kind, value: value["operation_id"] == "full")
        # The process is the one opened with the `ask` override; the full input's
        # requests still pass the connector and are all approved.
        await transports[0].incoming.put(
            claude.tool_request("anything", "Bash", {"command": "make deploy | tee log"})
        )
        assert (await claude.answered(transports[0], "request-anything"))["behavior"] == "allow"
        assert len(configurations) == 1
    finally:
        await driver.stop()


# --- Codex --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [None, ":workspace"])
async def test_full_scope_gives_the_codex_thread_full_access_and_restores_it(
    tmp_path: Path, profile: str | None
) -> None:
    owner = codex.AppServer(profile=profile, cwd=str(tmp_path))
    async with serve(owner.handler, "127.0.0.1", 0) as listener:
        driver, events = await codex.connected(owner, listener, tmp_path)
        try:
            receipt = await driver.execute(
                codex.command("prompt", "full", prompt="fix", scope=FULL)
            )
            methods = owner.methods()
            assert methods.index("thread/settings/update") < methods.index("thread/queue/add")
            widened = next(c for c in owner.calls if c.get("method") == "thread/settings/update")
            assert widened["params"] == {
                "threadId": "thread",
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "dangerFullAccess"},
            }
            assert receipt["thread_settings_before"]["approvalsReviewer"] == "auto_review"
            await codex.until(events, lambda kind, value: value["operation_id"] == "full")
            await owner.complete("turn-1")
            await codex.settled(owner, 2)
            restored = [c for c in owner.calls if c.get("method") == "thread/settings/update"][-1]
            expected: dict[str, Any] = {
                "threadId": "thread",
                "approvalPolicy": "never",
                "approvalsReviewer": "auto_review",
            }
            if profile:
                expected["permissions"] = profile
            else:
                expected["sandboxPolicy"] = {"type": "dangerFullAccess"}
            assert restored["params"] == expected
        finally:
            await driver.stop()


@pytest.mark.asyncio
async def test_full_scope_is_refused_when_codex_thread_settings_cannot_be_set(
    tmp_path: Path,
) -> None:
    owner = codex.AppServer(settings_supported=False)
    async with serve(owner.handler, "127.0.0.1", 0) as listener:
        driver, _ = await codex.connected(owner, listener, tmp_path)
        try:
            with pytest.raises(DriverError) as refused:
                await driver.execute(codex.command("prompt", "full", prompt="fix", scope=FULL))
            assert refused.value.code == "scope_unsupported"
            assert "thread/queue/add" not in owner.methods()
        finally:
            await driver.stop()


# --- ACP ----------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("modes", [acp.CLAUDE_MODES, acp.CODEX_MODES, "auto,bypassPermissions"])
async def test_full_scope_keeps_the_acp_agent_mode_and_approves_every_request(
    tmp_path: Path, modes: str
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside.txt"
    driver, events, trace = await acp.started(tmp_path, FAKE_ACP_MODES=modes)
    script = {
        "during": [
            acp.permission("edit-out", "edit", locations=[str(outside)]),
            acp.permission("chained", "execute", raw={"command": "make && rm -rf build"}),
            acp.permission("fetch", "fetch", raw={"url": "https://example.com"}),
            acp.permission("read", "read", locations=["/etc/hosts"]),
            acp.permission("other", "other"),
            {"write": str(outside)},
            {"terminal": "true"},
        ]
    }
    try:
        await asyncio.wait_for(
            driver.execute(acp.prompt("full", script, cwd=str(work), scope=FULL)), 5
        )
        # The scripted turn ends with the terminal callback.
        await acp.traced_until(trace, "terminal/create")
        assert acp.traced(trace, "session/set_mode") == []
        decisions = {
            row["tool"]: row["outcome"] for row in acp.traced(trace, "session/request_permission")
        }
        assert decisions == {
            key: {"outcome": "selected", "optionId": "allow"}
            for key in ("edit-out", "chained", "fetch", "read", "other")
        }
        assert acp.traced(trace, "fs/write_text_file") == [
            {"method": "fs/write_text_file", "path": str(outside), "error": False}
        ]
        assert acp.traced(trace, "terminal/create") == [
            {"method": "terminal/create", "error": False}
        ]
        while not events.empty():
            kind, _ = events.get_nowait()
            assert kind != "permission"
    finally:
        await driver.stop()


# --- One real Claude run (opt-in) ----------------------------------------------------

ARTIFACT = Path(__file__).resolve().parents[1] / "test-results" / "full-access"


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("YUKI_REAL_CLAUDE") or shutil.which("claude") is None,
    reason="one real paid Claude run; set YUKI_REAL_CLAUDE=1",
)
async def test_real_claude_full_scope_runs_bash_without_a_permission_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real driver path with the real CLI: haiku, isolated cwd, no setting sources."""
    real_options = claude_module.ClaudeAgentOptions
    opened: list[Any] = []

    def isolated(**kwargs: Any) -> Any:
        # Only isolation from the user's settings and a cheap model; every other
        # option is exactly what the driver builds for a full scope.
        options = real_options(**{**kwargs, "setting_sources": [], "model": "haiku"})
        opened.append(options)
        return options

    monkeypatch.setattr(claude_module, "ClaudeAgentOptions", isolated)
    work = tmp_path / "work"
    work.mkdir()
    events: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
    seen: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        seen.append((kind, payload))
        events.put_nowait((kind, payload))

    driver = claude_module.ClaudeDriver(
        Instance("claude", "claude", "Claude", str(shutil.which("claude")), cwd=str(work)), emit
    )
    await driver.start()
    marker = work / "marker.txt"
    try:
        await driver.execute(
            {
                "id": "real-full",
                "action": "prompt",
                "arguments": {
                    "prompt": (
                        "Use the Bash tool exactly once to run this command, then reply DONE: "
                        f"echo yuki-full-access > {marker} && cat {marker}"
                    ),
                    "cwd": str(work),
                    "scope": FULL,
                },
            }
        )
        async with asyncio.timeout(180):
            while True:
                kind, value = await events.get()
                if kind in {"permission", "question"}:
                    break
                if kind == "completed" and value["operation_id"] == "real-full":
                    break
    finally:
        await driver.stop()
    requests = [value for kind, value in seen if kind in {"permission", "question"}]
    approved = [
        value["payload"].get("tool")
        for kind, value in seen
        if kind == "progress" and "permission_auto_approved" in value.get("payload", {})
    ]
    tools = [
        value["payload"].get("tool")
        for kind, value in seen
        if kind == "progress" and "native_item_id" in value.get("payload", {})
    ]
    ARTIFACT.mkdir(parents=True, exist_ok=True)
    (ARTIFACT / "real-claude.json").write_text(
        json.dumps(
            {
                "claude_cli": str(shutil.which("claude")),
                "sdk_options": {
                    "permission_mode": opened[0].permission_mode,
                    "settings": opened[0].settings,
                    "add_dirs": [str(item) for item in opened[0].add_dirs],
                    "setting_sources": opened[0].setting_sources,
                    "model": opened[0].model,
                },
                "permission_or_question_events": requests,
                "connector_auto_approved_tools": approved,
                "tool_progress": tools,
                "marker_content": marker.read_text() if marker.exists() else None,
                "event_kinds": [kind for kind, _ in seen],
                "python": sys.version.split()[0],
            },
            indent=2,
            default=str,
        )
        + "\n"
    )
    assert requests == []
    assert marker.read_text() == "yuki-full-access\n"
