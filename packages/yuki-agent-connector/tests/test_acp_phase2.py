"""ACP connector 0.9.0 against a scripted stdio agent (task book §4.3, §4.4, §4.8, §15 E5).

Failure cases written before the implementation:
- F9: a scoped session keeps the user's auto mode (no request_permission at all), an
  out-of-scope permission is approved by the connector, a client fs/terminal callback
  outside the scope runs, or a scope is accepted when no asking mode exists;
- F1: an observe watch receives an answerable request;
- unowned request (no receiving operation) is uploaded in full or accepts allow;
- F16: an agent-initiated turn inside a held session is attributed to nothing;
- F13/G9: release leaves Yuki's ACP session (or process) holding the session.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from yuki_agent_connector.config import Instance
from yuki_agent_connector.drivers import DriverError
from yuki_agent_connector.drivers.acp import ACPDriver

AGENT = Path(__file__).parent / "fakes" / "acp_scope_agent.py"
Events = asyncio.Queue[tuple[str, dict[str, Any]]]
CLAUDE_MODES = "default,acceptEdits,plan,auto,bypassPermissions"
CODEX_MODES = "read-only,workspace-write,agent,agent-full-access"


async def started(tmp_path: Path, **env: str) -> tuple[ACPDriver, Events, Path]:
    trace = tmp_path / "trace.jsonl"
    events: Events = asyncio.Queue()

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.put_nowait((kind, payload))

    driver = ACPDriver(
        Instance(
            "acp",
            "acp",
            "ACP",
            sys.executable,
            args=[str(AGENT)],
            cwd=str(tmp_path),
            env={"FAKE_ACP_TRACE": str(trace), **env},
        ),
        emit,
    )
    await asyncio.wait_for(driver.start(), 5)
    return driver, events, trace


def traced(trace: Path, method: str | None = None) -> list[dict[str, Any]]:
    if not trace.exists():
        return []
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    return [row for row in rows if method is None or row["method"] == method]


async def traced_until(trace: Path, method: str, count: int = 1) -> list[dict[str, Any]]:
    async with asyncio.timeout(5):
        while len(rows := traced(trace, method)) < count:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    return rows


async def until(events: Events, predicate: Any) -> tuple[str, dict[str, Any]]:
    while True:
        kind, value = await asyncio.wait_for(events.get(), 5)
        if predicate(kind, value):
            return kind, value


def permission(
    identity: str,
    kind: str,
    *,
    locations: list[str] | None = None,
    raw: dict[str, Any] | None = None,
    reject: str = "reject",
) -> dict[str, Any]:
    tool: dict[str, Any] = {"toolCallId": identity, "title": identity, "kind": kind}
    if locations is not None:
        tool["locations"] = [{"path": path} for path in locations]
    if raw is not None:
        tool["rawInput"] = raw
    return {
        "permission": {
            "toolCall": tool,
            "options": [
                {"optionId": "allow", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "always", "name": "Always", "kind": "allow_always"},
                {"optionId": reject, "name": "Reject", "kind": "reject_once"},
            ],
        }
    }


def prompt(identity: str, script: dict[str, Any], **arguments: Any) -> dict[str, Any]:
    return {
        "id": identity,
        "action": "prompt",
        "arguments": {"prompt": json.dumps(script), **arguments},
    }


@pytest.mark.asyncio
async def test_acp_declares_scope_observe_and_release(tmp_path: Path) -> None:
    driver, _, _ = await started(tmp_path)
    try:
        assert {"scope", "observe", "release"} <= set(driver.capabilities)
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_unscoped_prompt_does_not_change_the_agent_mode(tmp_path: Path) -> None:
    driver, _, trace = await started(tmp_path, FAKE_ACP_MODES=CLAUDE_MODES)
    try:
        await driver.execute(prompt("plain", {}))
        await traced_until(trace, "session/prompt")
        assert traced(trace, "session/set_mode") == []
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_scoped_session_asks_and_the_connector_decides_only_inside_the_scope(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    driver, events, trace = await started(tmp_path, FAKE_ACP_MODES=CLAUDE_MODES)
    script = {
        "during": [
            permission("edit-in", "edit", locations=[str(work / "a.py")]),
            permission("test", "execute", raw={"command": "pytest -q", "cwd": str(tmp_path)}),
            {"write": str(work / "callback-in.txt")},
            {"write": str(tmp_path / "callback-out.txt")},
            {"terminal": "rm"},
            permission("edit-out", "edit", raw={"file_path": str(tmp_path / "b.py")}),
            permission("chained", "execute", raw={"command": "pytest; rm -rf ~"}),
            permission("fetch", "fetch", raw={"url": "https://example.com"}),
        ]
    }
    try:
        task = asyncio.create_task(
            driver.execute(
                prompt(
                    "scoped",
                    script,
                    scope={"write_paths": [str(work)], "commands": ["pytest"]},
                )
            )
        )
        [mode] = await traced_until(trace, "session/set_mode")
        assert mode["mode"] == "default"
        reported = []
        for _ in range(3):
            kind, value = await until(events, lambda kind, value: kind == "permission")
            assert value["operation_id"] == "scoped"
            reported.append(value["payload"]["tool"]["toolCallId"])
            await driver.execute(
                {
                    "id": f"answer-{len(reported)}",
                    "action": "respond",
                    "session_id": value["session_id"],
                    "execution_id": value["execution_id"],
                    "arguments": {
                        "operation_id": "scoped",
                        "connection_id": driver.owner,
                        "permission_id": value["payload"]["permission_id"],
                        "option_id": "reject",
                    },
                }
            )
        assert reported == ["edit-out", "chained", "fetch"]
        await task
        decisions = {
            row["tool"]: row["outcome"]
            for row in await traced_until(trace, "session/request_permission", 5)
        }
        assert decisions["edit-in"] == {"outcome": "selected", "optionId": "allow"}
        assert decisions["test"] == {"outcome": "selected", "optionId": "allow"}
        assert decisions["edit-out"] == {"outcome": "selected", "optionId": "reject"}
        writes = {row["path"]: row["error"] for row in traced(trace, "fs/write_text_file")}
        assert writes == {
            str(work / "callback-in.txt"): False,
            str(tmp_path / "callback-out.txt"): True,
        }
        assert not (tmp_path / "callback-out.txt").exists()
        assert traced(trace, "terminal/create") == [{"method": "terminal/create", "error": True}]
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_codex_style_agent_gets_an_asking_mode_and_a_cancelling_reject_is_reported(
    tmp_path: Path,
) -> None:
    driver, events, trace = await started(tmp_path, FAKE_ACP_MODES=CODEX_MODES)
    script = {
        "during": [permission("outside", "execute", raw={"command": "make"}, reject="cancel")]
    }
    try:
        task = asyncio.create_task(driver.execute(prompt("scoped", script, scope={})))
        [mode] = await traced_until(trace, "session/set_mode")
        assert mode["mode"] in {"read-only", "workspace-write"}
        kind, value = await until(events, lambda kind, value: kind == "permission")
        assert value["payload"]["reject_ends_prompt"] is True
        await driver.execute(
            {
                "id": "answer",
                "action": "respond",
                "session_id": value["session_id"],
                "execution_id": value["execution_id"],
                "arguments": {
                    "operation_id": "scoped",
                    "connection_id": driver.owner,
                    "permission_id": value["payload"]["permission_id"],
                    "option_id": "allow",
                },
            }
        )
        await task
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_scope_is_refused_when_the_agent_offers_no_asking_mode(tmp_path: Path) -> None:
    driver, _, trace = await started(tmp_path, FAKE_ACP_MODES="auto,bypassPermissions")
    try:
        with pytest.raises(DriverError) as refused:
            await driver.execute(prompt("scoped", {}, scope={"network": False}))
        assert refused.value.code == "scope_unsupported"
        assert traced(trace, "session/prompt") == []
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_request_without_receiver_is_metadata_only_and_deny_only(tmp_path: Path) -> None:
    driver, events, trace = await started(tmp_path)
    script = {"during": [permission("wait", "execute", raw={"command": "rm -rf secret"})]}
    try:
        task = asyncio.create_task(driver.execute(prompt("owner", script)))
        kind, owned = await until(events, lambda kind, value: kind == "permission")
        assert owned["operation_id"] == "owner"
        await driver.execute(
            {
                "id": "detach",
                "action": "detach",
                "session_id": owned["session_id"],
                "arguments": {"operation_id": "owner"},
            }
        )
        kind, unowned = await until(events, lambda kind, value: kind == "permission")
        assert unowned["operation_id"] is None
        payload = unowned["payload"]
        assert payload["unowned"] is True and payload["options"] == ["deny"]
        assert "rm -rf" not in json.dumps(unowned) and "tool" not in payload
        answer = {
            "action": "respond",
            "session_id": unowned["session_id"],
            "execution_id": unowned["execution_id"],
            "arguments": {
                "connection_id": driver.owner,
                "permission_id": payload["permission_id"],
            },
        }
        with pytest.raises(DriverError) as refused:
            await driver.execute(
                {
                    **answer,
                    "id": "allow",
                    "arguments": {**answer["arguments"], "option_id": "allow"},
                }
            )
        assert refused.value.code == "deny_only"
        await driver.execute(
            {**answer, "id": "deny", "arguments": {**answer["arguments"], "option_id": "reject"}}
        )
        kind, resolved = await until(events, lambda kind, value: kind == "progress")
        assert resolved["payload"]["permission_resolved"] == payload["permission_id"]
        await task
        [row] = await traced_until(trace, "session/request_permission")
        assert row["outcome"] == {"outcome": "selected", "optionId": "reject"}
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_agent_initiated_turn_belongs_to_the_held_owner(tmp_path: Path) -> None:
    driver, events, trace = await started(tmp_path)
    script = {
        "after": [
            {"tool": {"toolCallId": "background", "title": "bg", "kind": "execute"}},
            permission("background-wait", "execute", raw={"command": "make"}),
        ]
    }
    try:
        await driver.execute(prompt("owner", script))
        await until(events, lambda kind, value: kind == "work_finished")
        kind, begun = await until(events, lambda kind, value: kind == "progress")
        assert begun["operation_id"] is None and begun["owner_operation_id"] == "owner"
        followup = begun["followup_execution"]
        assert followup and begun["execution_id"] == followup
        kind, waiting = await until(events, lambda kind, value: kind == "permission")
        assert waiting["owner_operation_id"] == "owner"
        assert waiting["followup_execution"] == followup
        assert waiting["payload"].get("unowned") is not True
        await driver.execute(
            {
                "id": "answer",
                "action": "respond",
                "session_id": waiting["session_id"],
                "execution_id": followup,
                "arguments": {
                    "operation_id": f"followup:owner:{followup}",
                    "owner_operation_id": "owner",
                    "connection_id": driver.owner,
                    "permission_id": waiting["payload"]["permission_id"],
                    "option_id": "allow",
                },
            }
        )
        [row] = await traced_until(trace, "session/request_permission")
        assert row["outcome"] == {"outcome": "selected", "optionId": "allow"}
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_observe_watch_receives_metadata_only(tmp_path: Path) -> None:
    driver, events, _ = await started(tmp_path)
    try:
        first = await driver.execute(prompt("first", {}))
        await until(events, lambda kind, value: kind == "work_finished")
        session_id = first["session_id"]
        await driver.execute(
            {
                "id": "observer",
                "action": "watch",
                "session_id": session_id,
                "observation_scope": "session",
                "arguments": {"observation": "observe"},
            }
        )
        script = {"during": [permission("seen", "execute", raw={"command": "make secret"})]}
        task = asyncio.create_task(
            driver.execute({**prompt("second", script), "session_id": session_id})
        )
        seen: dict[str, dict[str, Any]] = {}
        while len(seen) < 2:
            kind, value = await until(events, lambda kind, value: kind == "permission")
            seen[value["operation_id"]] = value["payload"]
        observed = seen["observer"]
        assert observed["observed"] is True and observed["tool"] == "execute"
        assert not {"permission_id", "connection_id", "options"} & set(observed)
        assert "secret" not in json.dumps(observed)
        await driver.execute(
            {
                "id": "answer",
                "action": "respond",
                "session_id": session_id,
                "execution_id": first["execution_id"],
                "arguments": {
                    "operation_id": "second",
                    "connection_id": driver.owner,
                    "permission_id": seen["second"]["permission_id"],
                    "option_id": "allow",
                },
            }
        )
        await task
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_release_closes_the_session_at_the_turn_boundary(tmp_path: Path) -> None:
    driver, events, trace = await started(tmp_path)
    script = {"during": [permission("hold", "execute", raw={"command": "make"})]}
    try:
        task = asyncio.create_task(driver.execute(prompt("owner", script)))
        kind, waiting = await until(events, lambda kind, value: kind == "permission")
        session_id = waiting["session_id"]
        released = await driver.execute(
            {
                "id": "release",
                "action": "detach",
                "session_id": session_id,
                "arguments": {"operation_id": "owner", "release": True},
            }
        )
        assert released["detached"] is True and released["closed"] == "after_turn"
        assert traced(trace, "session/close") == []
        kind, unowned = await until(events, lambda kind, value: kind == "permission")
        await driver.execute(
            {
                "id": "deny",
                "action": "respond",
                "session_id": session_id,
                "execution_id": unowned["execution_id"],
                "arguments": {
                    "connection_id": driver.owner,
                    "permission_id": unowned["payload"]["permission_id"],
                    "option_id": "reject",
                },
            }
        )
        await task
        [closed] = await traced_until(trace, "session/close")
        assert closed["session"] == session_id
        assert session_id not in driver.sessions
        idle = await driver.execute(prompt("idle", {}))
        await until(events, lambda kind, value: kind == "work_finished")
        now = await driver.execute(
            {
                "id": "release-idle",
                "action": "detach",
                "session_id": idle["session_id"],
                "arguments": {"operation_id": "idle", "release": True},
            }
        )
        assert now["closed"] == "now"
        assert traced(trace, "session/close")[-1]["session"] == idle["session_id"]
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_release_without_session_close_ends_the_agent_process(tmp_path: Path) -> None:
    driver, events, trace = await started(tmp_path, FAKE_ACP_NO_CLOSE="1")
    try:
        idle = await driver.execute(prompt("idle", {}))
        await until(events, lambda kind, value: kind == "work_finished")
        [first] = traced(trace, "started")
        now = await driver.execute(
            {
                "id": "release",
                "action": "detach",
                "session_id": idle["session_id"],
                "arguments": {"operation_id": "idle", "release": True},
            }
        )
        assert now["closed"] == "now"
        rows = await traced_until(trace, "started", 2)
        assert rows[1]["pid"] != first["pid"]
        assert not driver.sessions
        again = await driver.execute(prompt("again", {}))
        assert again["admitted"] is True
    finally:
        await driver.stop()
