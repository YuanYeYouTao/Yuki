"""A delegation's scope is not bypassed by the user's own allow rules (#650, 0.9.8).

Task book §16.4 S1–S2: the Claude `ask` override left out MCP tools, and an ACP
session in claude-agent-acp's `default` mode still loads the user's allow rules, so
an allowed Bash command or MCP tool runs without any request reaching the connector.
Both were observed with the real agents before the fix (see the opt-in runs below).

Failure cases written before the implementation:
- C1: a narrowed Claude scope's `ask` override has no rule covering MCP tools, so a
  user allow rule for an MCP tool runs it without can_use_tool;
- C2: an MCP tool request in a narrowed Claude scope is auto-approved instead of
  reported (a scope grants files, commands and network, never MCP tools);
- A1: a narrowed ACP scope opens its session (session/new) without the `ask`
  override, so the user's allowed Bash command or MCP tool runs without
  session/request_permission;
- A2: session/load or session/resume for a narrowed scope carries no override;
- A3: an unscoped prompt or a full scope sends the override (it would change the
  agent's own behaviour where nothing is narrowed);
- A4: a session held without the override (opened unscoped or full) accepts a later
  narrowed prompt instead of `scope_conflict`, or sends that prompt or a mode change
  to the agent;
- A5: a session opened with the override refuses later unscoped or full prompts;
- R1/R2 (opt-in, real agents): the real Claude CLI or claude-agent-acp writes a file
  outside the scope through a user-allowed tool without a reported request.
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

from yuki_agent_connector.config import Instance
from yuki_agent_connector.drivers import DriverError
from yuki_agent_connector.drivers import claude as claude_module
from yuki_agent_connector.drivers.acp import ACPDriver

FULL: dict[str, Any] = {"write_paths": ["/"], "network": True, "commands": ["*"]}
MCP_SERVER = Path(__file__).parent / "fakes" / "mcp_touch_server.py"
ARTIFACT = Path(__file__).resolve().parents[1] / "test-results" / "scope-user-allow-rules"


def override(row: dict[str, Any]) -> list[str] | None:
    """The `ask` rules a session/new, load or resume carried to claude-agent-acp."""
    meta = row.get("meta")
    if meta is None:
        return None
    return meta["claudeCode"]["options"]["settings"]["permissions"]["ask"]  # type: ignore[no-any-return]


# --- Claude ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c1_c2_narrowed_claude_scope_sends_mcp_tools_through_the_connector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, transports, configurations, events = await claude.started(tmp_path, monkeypatch)
    work = tmp_path / "work"
    work.mkdir()
    try:
        await driver.execute(
            {
                "id": "delegated",
                "action": "prompt",
                "arguments": {
                    "prompt": "fix it",
                    "cwd": str(tmp_path),
                    "scope": {"write_paths": [str(work)], "commands": ["pytest"], "network": True},
                },
            }
        )
        await claude.until(events, lambda kind, value: value["operation_id"] == "delegated")
        options = configurations[0]
        assert options.settings is not None
        assert "mcp__*" in json.loads(options.settings)["permissions"]["ask"]
        # Even with network and a path argument inside the scope: reported, not approved.
        arguments = {"path": str(work / "a.txt")}
        await transports[0].incoming.put(
            claude.tool_request("mcp", "mcp__yukiprobe__touch", arguments)
        )
        _kind, reported = await claude.until(events, lambda kind, value: kind == "permission")
        assert reported["operation_id"] == "delegated"
        assert reported["payload"]["tool"] == "mcp__yukiprobe__touch"
        assert claude.response_for(transports[0], "request-mcp") is None
    finally:
        await driver.stop()


# --- ACP ------------------------------------------------------------------------------

USER_ALLOW = "Bash,mcp__yukiprobe__touch"
NARROW: dict[str, Any] = {"commands": ["pytest"]}


def allowed_by_user(tmp_path: Path) -> dict[str, Any]:
    """Two requests the user's own allow rules would run without asking."""
    return {
        "during": [
            {
                **acp.permission(
                    "bash", "execute", raw={"command": "touch x", "cwd": str(tmp_path)}
                ),
                "native_tool": "Bash",
            },
            {
                **acp.permission("mcp", "other", raw={"path": str(tmp_path / "x")}),
                "native_tool": "mcp__yukiprobe__touch",
            },
        ]
    }


async def answer_until_done(driver: ACPDriver, events: acp.Events, operation: str) -> list[str]:
    """Reject every reported request of ``operation``; return them in order."""
    reported: list[str] = []
    while True:
        kind, value = await acp.until(events, lambda kind, value: True)
        if kind == "permission" and value["operation_id"] == operation:
            reported.append(value["payload"]["tool"]["toolCallId"])
            await driver.execute(
                {
                    "id": f"answer-{len(reported)}",
                    "action": "respond",
                    "session_id": value["session_id"],
                    "execution_id": value["execution_id"],
                    "arguments": {
                        "operation_id": operation,
                        "connection_id": driver.owner,
                        "permission_id": value["payload"]["permission_id"],
                        "option_id": "reject",
                    },
                }
            )
        if kind == "completed" and value["operation_id"] == operation:
            return reported


@pytest.mark.asyncio
async def test_a1_narrowed_acp_session_reports_what_the_user_allowed(tmp_path: Path) -> None:
    driver, events, trace = await acp.started(
        tmp_path, FAKE_ACP_MODES=acp.CLAUDE_MODES, FAKE_ACP_USER_ALLOW=USER_ALLOW
    )
    try:
        await driver.execute(acp.prompt("scoped", allowed_by_user(tmp_path), scope=NARROW))
        assert await answer_until_done(driver, events, "scoped") == ["bash", "mcp"]
        assert acp.traced(trace, "ran_unasked") == []
        [created] = acp.traced(trace, "session/new")
        ask = override(created)
        assert ask is not None
        assert {"Bash", "Edit", "Write", "WebFetch", "SendMessage", "mcp__*"} <= set(ask)
        [mode] = acp.traced(trace, "session/set_mode")
        assert mode["mode"] == "default"
    finally:
        await driver.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["load", "resume"])
async def test_a2_loaded_or_resumed_session_carries_the_override(
    tmp_path: Path, stored: str
) -> None:
    environment = {"FAKE_ACP_MODES": acp.CLAUDE_MODES, "FAKE_ACP_USER_ALLOW": USER_ALLOW}
    if stored == "load":
        environment["FAKE_ACP_NO_LIST"] = "1"
    else:
        environment["FAKE_ACP_STORED"] = json.dumps([{"sessionId": "stored", "cwd": str(tmp_path)}])
    driver, events, trace = await acp.started(tmp_path, **environment)
    try:
        command = acp.prompt("scoped", allowed_by_user(tmp_path), scope=NARROW, resume=True)
        await driver.execute({**command, "session_id": "stored", "recorded_cwd": str(tmp_path)})
        assert await answer_until_done(driver, events, "scoped") == ["bash", "mcp"]
        assert acp.traced(trace, "ran_unasked") == []
        [opened] = acp.traced(trace, f"session/{stored}")
        assert "mcp__*" in (override(opened) or [])
    finally:
        await driver.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [None, FULL], ids=["unscoped", "full"])
async def test_a3_unscoped_or_full_session_is_opened_without_the_override(
    tmp_path: Path, scope: dict[str, Any] | None
) -> None:
    driver, events, trace = await acp.started(tmp_path, FAKE_ACP_MODES=acp.CLAUDE_MODES)
    try:
        arguments = {} if scope is None else {"scope": scope}
        await driver.execute(acp.prompt("plain", {}, **arguments))
        await acp.until(events, lambda kind, value: kind == "completed")
        [created] = acp.traced(trace, "session/new")
        assert "meta" not in created
    finally:
        await driver.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [None, FULL], ids=["unscoped", "full"])
async def test_a4_session_held_without_the_override_refuses_a_narrowed_scope(
    tmp_path: Path, first: dict[str, Any] | None
) -> None:
    driver, events, trace = await acp.started(
        tmp_path, FAKE_ACP_MODES=acp.CLAUDE_MODES, FAKE_ACP_USER_ALLOW=USER_ALLOW
    )
    try:
        arguments = {} if first is None else {"scope": first}
        held = await driver.execute(acp.prompt("first", {}, **arguments))
        await acp.until(events, lambda kind, value: kind == "completed")
        with pytest.raises(DriverError) as refused:
            await driver.execute(
                {
                    **acp.prompt("narrowed", allowed_by_user(tmp_path), scope=NARROW),
                    "session_id": held["session_id"],
                }
            )
        assert refused.value.code == "scope_conflict"
        assert len(acp.traced(trace, "session/prompt")) == 1
        assert acp.traced(trace, "session/set_mode") == []
        assert acp.traced(trace, "ran_unasked") == []
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_a5_session_opened_with_the_override_accepts_later_prompts(
    tmp_path: Path,
) -> None:
    driver, events, trace = await acp.started(tmp_path, FAKE_ACP_MODES=acp.CLAUDE_MODES)
    try:
        held = await driver.execute(acp.prompt("scoped", {}, scope=NARROW))
        await acp.until(events, lambda kind, value: kind == "completed")
        for identity, arguments in (("plain", {}), ("full", {"scope": FULL})):
            await driver.execute(
                {**acp.prompt(identity, {}, **arguments), "session_id": held["session_id"]}
            )
            await acp.until(
                events,
                lambda kind, value, i=identity: kind == "completed" and value["operation_id"] == i,
            )
        assert len(acp.traced(trace, "session/prompt")) == 3
    finally:
        await driver.stop()


# --- Real agents (opt-in) -------------------------------------------------------------


def project_with_user_rules(root: Path, allow: list[str]) -> Path:
    """A project whose local settings stand in for the user's own allow rules."""
    project = root / "project"
    (project / ".claude").mkdir(parents=True)
    (project / "work").mkdir()
    (project / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"yukiprobe": {"command": sys.executable, "args": [str(MCP_SERVER)]}}}
        )
    )
    (project / ".claude" / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": allow}, "enableAllProjectMcpServers": True})
    )
    return project


async def run_and_deny(
    driver: Any, events: asyncio.Queue[tuple[str, dict[str, Any]]], operation: str
) -> list[tuple[str, dict[str, Any]]]:
    """Deny every reported request (the owner's refusal) until the turn ends."""
    seen: list[tuple[str, dict[str, Any]]] = []
    async with asyncio.timeout(240):
        while True:
            kind, value = await events.get()
            seen.append((kind, value))
            if kind == "permission":
                await driver.execute(
                    {
                        "id": f"deny-{len(seen)}",
                        "action": "respond",
                        "session_id": value["session_id"],
                        "execution_id": value["execution_id"],
                        "arguments": {
                            "operation_id": operation,
                            "connection_id": value["payload"]["connection_id"],
                            "permission_id": value["payload"]["permission_id"],
                            "option_id": "deny",
                        },
                    }
                )
            if kind == "completed" and value.get("operation_id") == operation:
                return seen


def write_artifact(name: str, value: dict[str, Any]) -> None:
    ARTIFACT.mkdir(parents=True, exist_ok=True)
    (ARTIFACT / name).write_text(json.dumps(value, indent=2, default=str) + "\n")


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("YUKI_REAL_CLAUDE") or shutil.which("claude") is None,
    reason="one real paid Claude run; set YUKI_REAL_CLAUDE=1",
)
async def test_r1_real_claude_reports_a_user_allowed_mcp_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real driver path and CLI; only the user's own settings are left out."""
    real_options = claude_module.ClaudeAgentOptions
    opened: list[Any] = []

    def isolated(**kwargs: Any) -> Any:
        options = real_options(
            **{**kwargs, "setting_sources": ["project", "local"], "model": "haiku"}
        )
        opened.append(options)
        return options

    monkeypatch.setattr(claude_module, "ClaudeAgentOptions", isolated)
    project = project_with_user_rules(tmp_path, ["mcp__yukiprobe"])
    target = tmp_path / "outside.txt"
    events: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.put_nowait((kind, payload))

    claude_cli = str(shutil.which("claude"))
    driver = claude_module.ClaudeDriver(
        Instance("claude", "claude", "Claude", claude_cli, cwd=str(project)), emit
    )
    await driver.start()
    try:
        await driver.execute(
            {
                "id": "real-mcp",
                "action": "prompt",
                "arguments": {
                    "prompt": (
                        "Call the MCP tool mcp__yukiprobe__touch exactly once with "
                        f"path={target}, then reply DONE. Use no other tool."
                    ),
                    "cwd": str(project),
                    "scope": {"write_paths": [str(project / "work")], "commands": ["pytest"]},
                },
            }
        )
        seen = await run_and_deny(driver, events, "real-mcp")
    finally:
        await driver.stop()
    reported = [value["payload"]["tool"] for kind, value in seen if kind == "permission"]
    write_artifact(
        "real-claude.json",
        {
            "claude_cli": claude_cli,
            "settings": opened[0].settings,
            "setting_sources": opened[0].setting_sources,
            "user_allow_stand_in": ["mcp__yukiprobe"],
            "reported_tools": reported,
            "outside_file_written": target.exists(),
            "event_kinds": [kind for kind, _ in seen],
        },
    )
    assert reported == ["mcp__yukiprobe__touch"]
    assert not target.exists()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("YUKI_REAL_CLAUDE_AGENT_ACP"),
    reason="one real paid claude-agent-acp run; set YUKI_REAL_CLAUDE_AGENT_ACP=<executable>",
)
async def test_r2_real_claude_agent_acp_reports_user_allowed_tools(tmp_path: Path) -> None:
    """claude-agent-acp loads the user's settings too; the project stands in for them."""
    project = project_with_user_rules(tmp_path, ["Bash(touch:*)", "mcp__yukiprobe"])
    # Inside the cwd but outside the scope's write path, and outside everything.
    bash_target, mcp_target = project / "bash.txt", tmp_path / "mcp.txt"
    events: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.put_nowait((kind, payload))

    agent = os.environ["YUKI_REAL_CLAUDE_AGENT_ACP"]
    driver = ACPDriver(
        Instance(
            "acp",
            "acp",
            "claude-agent-acp",
            agent,
            cwd=str(project),
            env={"ANTHROPIC_MODEL": "haiku"},
        ),
        emit,
    )
    await driver.start()
    try:
        await driver.execute(
            {
                "id": "real-acp",
                "action": "prompt",
                "arguments": {
                    "prompt": (
                        "Do exactly these two steps, then reply DONE. 1) With Bash run: "
                        f"touch {bash_target} 2) Call the MCP tool mcp__yukiprobe__touch "
                        f"with path={mcp_target}. If a step is not permitted, skip it."
                    ),
                    "cwd": str(project),
                    "scope": {"write_paths": [str(project / "work")], "commands": ["pytest"]},
                },
            }
        )
        seen = await run_and_deny(driver, events, "real-acp")
    finally:
        await driver.stop()
    reported = [
        (value["payload"]["tool"].get("kind"), value["payload"]["tool"].get("title"))
        for kind, value in seen
        if kind == "permission"
    ]
    write_artifact(
        "real-claude-agent-acp.json",
        {
            "agent": agent,
            "user_allow_stand_in": ["Bash(touch:*)", "mcp__yukiprobe"],
            "reported": [kind for kind, _ in reported],
            "bash_file_written": bash_target.exists(),
            "mcp_file_written": mcp_target.exists(),
            "event_kinds": [kind for kind, _ in seen],
        },
    )
    assert len(reported) == 2
    assert not bash_target.exists() and not mcp_target.exists()
