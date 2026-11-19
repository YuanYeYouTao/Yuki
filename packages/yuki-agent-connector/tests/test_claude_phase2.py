"""Claude connector 0.9.0 (task book §4.3, §4.4, §4.8, §4.11, §15).

Failure cases written before the implementation:
- F1: a watch marked observe receives an answerable request, or the driver writes
  anything into an observed session; a terminal session cannot be observed at all;
- F9: a scoped delegation lets a user allow rule (or anything outside the scope) run
  without the connector deciding, or an unscoped command changes 0.8.0 options;
- F11: a session live in another Claude process is resumed (silent fork) instead of
  refused, or an explicit fork touches the original session ID;
- F13: after release Yuki's SDK process still holds the session;
- F16: a turn the CLI starts by itself is attributed to nothing while its SDK owner
  is held by a delegation, or to an owner that was detached;
- §4.16: a read-only view hides inputs written but not yet taken up.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from test_claude_driver import NativeTransport, _permission, _result

from yuki_agent_connector.config import Instance
from yuki_agent_connector.drivers import DriverError
from yuki_agent_connector.drivers import claude as module

Events = asyncio.Queue[tuple[str, dict[str, Any]]]


async def started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, list[NativeTransport], list[ClaudeAgentOptions], Events]:
    transports: list[NativeTransport] = []
    configurations: list[ClaudeAgentOptions] = []

    def factory(*, options: ClaudeAgentOptions) -> ClaudeSDKClient:
        transport = NativeTransport()
        transports.append(transport)
        configurations.append(options)
        return ClaudeSDKClient(options=options, transport=transport)

    monkeypatch.setattr(module, "ClaudeSDKClient", factory)
    events: Events = asyncio.Queue()

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.put_nowait((kind, payload))

    profile = tmp_path / "profile"
    (profile / "sessions").mkdir(parents=True)
    driver = module.ClaudeDriver(
        Instance(
            "claude",
            "claude",
            "Claude",
            sys.executable,
            cwd=str(tmp_path),
            env={"CLAUDE_CONFIG_DIR": str(profile)},
        ),
        emit,
    )
    driver.registry_interval = 0.01
    await driver.start()
    return driver, transports, configurations, events


async def until(
    events: Events, predicate: Callable[[str, dict[str, Any]], bool]
) -> tuple[str, dict[str, Any]]:
    while True:
        kind, value = await asyncio.wait_for(events.get(), 3)
        if predicate(kind, value):
            return kind, value


def registry(tmp_path: Path, pid: int, session_id: str, status: str, **extra: Any) -> Path:
    path = tmp_path / "profile" / "sessions" / f"{pid}.json"
    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "sessionId": session_id,
                "cwd": str(tmp_path),
                "kind": "interactive",
                "entrypoint": "cli",
                "status": status,
                "statusUpdatedAt": extra.pop("updated", 1),
                **extra,
            }
        )
    )
    return path


async def dead_pid() -> int:
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    await process.wait()
    return process.pid


def response_for(transport: NativeTransport, request_id: str) -> dict[str, Any] | None:
    for write in transport.writes:
        if write["type"] == "control_response" and write["response"]["request_id"] == request_id:
            return write["response"]["response"]  # type: ignore[no-any-return]
    return None


async def answered(transport: NativeTransport, request_id: str) -> dict[str, Any]:
    async with asyncio.timeout(3):
        while (response := response_for(transport, request_id)) is None:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    return response


def tool_request(tool_use_id: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "control_request",
        "request_id": f"request-{tool_use_id}",
        "request": {
            "subtype": "can_use_tool",
            "tool_name": tool,
            "tool_use_id": tool_use_id,
            "input": arguments,
        },
    }


def state_running(session_id: str, uuid: str) -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "session_state_changed",
        "uuid": uuid,
        "session_id": session_id,
        "state": "running",
    }


@pytest.mark.asyncio
async def test_claude_declares_scope_observe_and_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, *_ = await started(tmp_path, monkeypatch)
    try:
        assert {"scope", "observe", "release"} <= set(driver.capabilities)
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_observe_watch_of_an_sdk_session_gets_metadata_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, transports, _, events = await started(tmp_path, monkeypatch)
    try:
        prompt = await driver.execute(
            {"id": "owner", "action": "prompt", "arguments": {"prompt": "work"}}
        )
        session_id = prompt["session_id"]
        await until(events, lambda kind, value: value["operation_id"] == "owner")
        await driver.execute(
            {
                "id": "observer",
                "action": "watch",
                "session_id": session_id,
                "observation_scope": "session",
                "arguments": {"observation": "observe"},
            }
        )
        writes = len(transports[0].writes)
        await transports[0].incoming.put(_permission("seen", "rm -rf build"))
        seen: dict[str, dict[str, Any]] = {}
        while len(seen) < 2:
            kind, value = await asyncio.wait_for(events.get(), 3)
            assert kind == "permission"
            seen[value["operation_id"]] = value
        assert "native_request" in seen["owner"]["payload"]
        observed = seen["observer"]["payload"]
        assert observed["observed"] is True and observed["native_request_id"] == "seen"
        assert observed["tool"] == "Bash"
        assert not {"native_request", "permission_id", "connection_id", "options"} & set(observed)
        assert "rm -rf" not in json.dumps(seen["observer"])
        # Observing writes nothing into the session.
        assert not [w for w in transports[0].writes[writes:] if w["type"] == "user"]
        # The owner detaches: the observer still never becomes an answerer.
        await driver.execute(
            {
                "id": "release-owner",
                "action": "detach",
                "session_id": session_id,
                "arguments": {"operation_id": "owner"},
            }
        )
        kind, unowned = await until(events, lambda kind, value: kind == "permission")
        assert unowned["operation_id"] is None and unowned["payload"]["unowned"] is True
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_observe_watch_of_a_terminal_session_follows_the_session_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, transports, _, events = await started(tmp_path, monkeypatch)
    try:
        with pytest.raises(DriverError, match="readonly"):
            await driver.execute(
                {"id": "plain", "action": "watch", "session_id": "terminal", "arguments": {}}
            )
        with pytest.raises(DriverError) as missing:
            await driver.execute(
                {
                    "id": "absent",
                    "action": "watch",
                    "session_id": "terminal",
                    "observation_scope": "session",
                    "arguments": {"observation": "observe"},
                }
            )
        assert missing.value.code == "not_live"
        row = registry(tmp_path, os.getpid(), "terminal", "idle")
        watching = await driver.execute(
            {
                "id": "observer",
                "action": "watch",
                "session_id": "terminal",
                "observation_scope": "session",
                "arguments": {"observation": "observe"},
            }
        )
        assert watching["status"] == "idle" and watching["pending"] is True
        assert watching["admitted"] is True and watching["session_id"] == "terminal"
        registry(tmp_path, os.getpid(), "terminal", "busy", updated=2)
        kind, busy = await until(events, lambda kind, value: True)
        assert kind == "progress" and busy["operation_id"] == "observer"
        assert busy["payload"]["status"] == "running"
        registry(
            tmp_path,
            os.getpid(),
            "terminal",
            "waiting",
            waitingFor="permission prompt",
            updated=3,
        )
        kind, waiting = await until(events, lambda kind, value: True)
        assert kind == "permission" and waiting["operation_id"] == "observer"
        payload = waiting["payload"]
        assert payload["observed"] is True and payload["native_request_id"] == "terminal:3"
        assert payload["waiting_for"] == "permission prompt"
        assert not {"permission_id", "connection_id", "options", "native_request"} & set(payload)
        assert not driver.requests
        registry(tmp_path, os.getpid(), "terminal", "busy", updated=4)
        kind, resolved = await until(events, lambda kind, value: True)
        assert kind == "progress" and resolved["payload"]["permission_resolved"] == "terminal:3"
        registry(tmp_path, os.getpid(), "terminal", "idle", updated=5)
        kind, idle = await until(events, lambda kind, value: True)
        assert kind == "progress" and idle["payload"]["execution_status"] == "completed"
        row.unlink()
        kind, gone = await until(events, lambda kind, value: True)
        assert kind == "progress" and gone["payload"]["status"] == "unknown"
        assert gone["payload"]["reason"] == "not_live"
        assert transports == []  # No SDK process was opened for an observed terminal.
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_unscoped_prompt_keeps_the_0_8_0_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, _, configurations, _ = await started(tmp_path, monkeypatch)
    try:
        await driver.execute({"id": "plain", "action": "prompt", "arguments": {"prompt": "go"}})
        options = configurations[0]
        assert options.settings is None and options.add_dirs == []
        assert options.setting_sources == ["user", "project", "local"]
        assert options.disallowed_tools == [] and options.permission_mode is None
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_scope_sends_every_side_effect_through_the_connector_and_never_widens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, transports, configurations, events = await started(tmp_path, monkeypatch)
    work = tmp_path / "work"
    work.mkdir()
    try:
        prompt = await driver.execute(
            {
                "id": "delegated",
                "action": "prompt",
                "arguments": {
                    "prompt": "fix it",
                    "cwd": str(tmp_path),
                    "scope": {"write_paths": [str(work)], "commands": ["pytest"]},
                },
            }
        )
        await until(events, lambda kind, value: value["operation_id"] == "delegated")
        options = configurations[0]
        assert options.setting_sources == ["user", "project", "local"]
        assert [str(item) for item in options.add_dirs] == [str(work)]
        assert options.settings is not None
        ask = json.loads(options.settings)["permissions"]["ask"]
        assert {"Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "SendMessage"} <= set(ask)
        transport = transports[0]
        inside = [
            ("write-in", "Write", {"file_path": str(work / "a.py"), "content": "x"}),
            ("edit-in", "Edit", {"file_path": str(work / "sub" / "b.py")}),
            ("test", "Bash", {"command": "pytest -q tests"}),
        ]
        for key, tool, arguments in inside:
            await transport.incoming.put(tool_request(key, tool, arguments))
            assert (await answered(transport, f"request-{key}"))["behavior"] == "allow"
        outside = [
            ("write-out", "Write", {"file_path": str(tmp_path / "elsewhere.py")}),
            ("escape", "Edit", {"file_path": str(work / ".." / "escape.py")}),
            ("chained", "Bash", {"command": "pytest; rm -rf ~"}),
            ("other", "Bash", {"command": "rm -rf build"}),
            ("fetch", "WebFetch", {"url": "https://example.com"}),
            ("message", "SendMessage", {"to": "peer", "message": "hi"}),
        ]
        for key, tool, arguments in outside:
            await transport.incoming.put(tool_request(key, tool, arguments))
            _kind, reported = await until(events, lambda kind, value: kind == "permission")
            assert reported["operation_id"] == "delegated"
            assert reported["payload"]["native_request_id"] == key
            assert response_for(transport, f"request-{key}") is None
        assert prompt["session_id"] == configurations[0].session_id
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_network_scope_allows_fetch_but_never_cross_session_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, transports, _, events = await started(tmp_path, monkeypatch)
    try:
        await driver.execute(
            {
                "id": "delegated",
                "action": "prompt",
                "arguments": {"prompt": "read docs", "scope": {"network": True}},
            }
        )
        await until(events, lambda kind, value: value["operation_id"] == "delegated")
        transport = transports[0]
        await transport.incoming.put(tool_request("fetch", "WebFetch", {"url": "https://x"}))
        assert (await answered(transport, "request-fetch"))["behavior"] == "allow"
        await transport.incoming.put(tool_request("message", "SendMessage", {"to": "peer"}))
        _kind, reported = await until(events, lambda kind, value: kind == "permission")
        assert reported["payload"]["native_request_id"] == "message"
        # Default write path is the session cwd.
        await transport.incoming.put(
            tool_request("write", "Write", {"file_path": str(tmp_path / "in-cwd.txt")})
        )
        assert (await answered(transport, "request-write"))["behavior"] == "allow"
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_session_live_in_another_process_is_refused_and_fork_keeps_the_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, transports, configurations, _ = await started(tmp_path, monkeypatch)

    async def inspect(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"native": {"cwd": str(tmp_path)}}

    monkeypatch.setattr(driver, "_inspect", inspect)
    registry(tmp_path, os.getpid(), "live", "busy")
    try:
        with pytest.raises(DriverError) as refused:
            await driver.execute(
                {
                    "id": "resume-live",
                    "action": "prompt",
                    "session_id": "live",
                    "arguments": {"prompt": "continue", "resume": True},
                }
            )
        assert refused.value.code == "session_live_elsewhere"
        assert refused.value.details == {"options": ["fork", "handoff"]}
        assert transports == []
        forked = await driver.execute(
            {
                "id": "fork-live",
                "action": "prompt",
                "session_id": "live",
                "arguments": {"prompt": "continue", "resume": True, "fork": True},
            }
        )
        options = configurations[0]
        assert options.resume == "live" and options.fork_session is True
        assert options.session_id and options.session_id != "live"
        assert forked["session_id"] == options.session_id
        assert forked["forked_from"] == "live"
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_registry_rows_of_exited_processes_do_not_block_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, _, configurations, _ = await started(tmp_path, monkeypatch)

    async def inspect(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"native": {"cwd": str(tmp_path)}}

    monkeypatch.setattr(driver, "_inspect", inspect)
    registry(tmp_path, await dead_pid(), "stale", "busy")
    try:
        resumed = await driver.execute(
            {
                "id": "resume",
                "action": "prompt",
                "session_id": "stale",
                "arguments": {"prompt": "continue", "resume": True},
            }
        )
        assert resumed["session_id"] == "stale"
        assert configurations[0].resume == "stale" and configurations[0].fork_session is False
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_release_closes_the_sdk_owner_at_the_turn_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, transports, _, events = await started(tmp_path, monkeypatch)
    try:
        prompt = await driver.execute(
            {"id": "owner", "action": "prompt", "arguments": {"prompt": "work"}}
        )
        session_id = prompt["session_id"]
        await until(events, lambda kind, value: value["operation_id"] == "owner")
        released = await driver.execute(
            {
                "id": "release",
                "action": "detach",
                "session_id": session_id,
                "arguments": {"operation_id": "owner", "release": True},
            }
        )
        assert released["detached"] is True and released["closed"] == "after_turn"
        assert transports[0].closed is False
        await transports[0].incoming.put(_result(session_id, "done"))
        async with asyncio.timeout(3):
            while not transports[0].closed:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        assert driver.sessions[session_id].stopped.is_set()

        async def inspect(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return {"native": {"cwd": str(tmp_path)}}

        monkeypatch.setattr(driver, "_inspect", inspect)
        # An idle owner closes at once.
        second = await driver.execute(
            {
                "id": "again",
                "action": "prompt",
                "session_id": session_id,
                "arguments": {"prompt": "more", "resume": True},
            }
        )
        await until(events, lambda kind, value: value["operation_id"] == "again")
        await transports[1].incoming.put(_result(second["session_id"], "done"))
        await until(events, lambda kind, value: kind == "work_finished")
        closed = await driver.execute(
            {
                "id": "release-idle",
                "action": "detach",
                "session_id": session_id,
                "arguments": {"operation_id": "again", "release": True},
            }
        )
        assert closed["closed"] == "now" and transports[1].closed is True
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_release_keeps_an_owner_another_operation_still_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, transports, _, events = await started(tmp_path, monkeypatch)
    try:
        prompt = await driver.execute(
            {"id": "owner", "action": "prompt", "arguments": {"prompt": "work"}}
        )
        session_id = prompt["session_id"]
        await until(events, lambda kind, value: value["operation_id"] == "owner")
        await driver.execute(
            {
                "id": "watcher",
                "action": "watch",
                "session_id": session_id,
                "observation_scope": "session",
                "arguments": {},
            }
        )
        kept = await driver.execute(
            {
                "id": "release",
                "action": "detach",
                "session_id": session_id,
                "arguments": {"operation_id": "owner", "release": True},
            }
        )
        assert kept["detached"] is True and kept["closed"] is None
        await transports[0].incoming.put(_result(session_id, "done"))
        await until(
            events,
            lambda kind, value: kind == "progress" and "execution_status" in value["payload"],
        )
        assert transports[0].closed is False
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_self_started_turn_belongs_to_the_held_owner_until_detach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, transports, _, events = await started(tmp_path, monkeypatch)
    try:
        prompt = await driver.execute(
            {"id": "owner", "action": "prompt", "arguments": {"prompt": "work"}}
        )
        session_id = prompt["session_id"]
        transport = transports[0]
        await until(events, lambda kind, value: value["operation_id"] == "owner")
        await transport.incoming.put(_result(session_id, "done"))
        await until(events, lambda kind, value: kind == "work_finished")
        await transport.incoming.put(state_running(session_id, "wakeup-turn"))
        kind, begun = await until(events, lambda kind, value: True)
        assert kind == "progress" and begun["operation_id"] is None
        assert begun["owner_operation_id"] == "owner"
        assert begun["followup_execution"] == "wakeup-turn"
        assert begun["execution_id"] == "wakeup-turn"
        assert begun["payload"]["status"] == "running" and begun["payload"]["pending"] is True
        await transport.incoming.put(_permission("followup-wait", "make test"))
        kind, waiting = await until(events, lambda kind, value: kind == "permission")
        assert waiting["operation_id"] is None and waiting["owner_operation_id"] == "owner"
        assert waiting["followup_execution"] == "wakeup-turn"
        payload = waiting["payload"]
        assert payload.get("unowned") is not True and "native_request" in payload
        await driver.execute(
            {
                "id": "answer",
                "action": "respond",
                "session_id": session_id,
                "execution_id": "wakeup-turn",
                "arguments": {
                    "operation_id": "followup:owner:wakeup-turn",
                    "owner_operation_id": "owner",
                    "connection_id": payload["connection_id"],
                    "permission_id": "followup-wait",
                    "option_id": "allow",
                },
            }
        )
        assert (await answered(transport, "request-followup-wait"))["behavior"] == "allow"
        await transport.incoming.put(_result(session_id, "woke up and finished"))
        kind, finished = await until(events, lambda kind, value: kind == "completed")
        assert finished["operation_id"] is None and finished["owner_operation_id"] == "owner"
        assert finished["followup_execution"] == "wakeup-turn"
        assert finished["payload"]["status"] == "completed"
        assert finished["payload"]["pending"] is False
        # After the owner is detached a self-started turn is nobody's: metadata, deny-only.
        await driver.execute(
            {
                "id": "detach",
                "action": "detach",
                "session_id": session_id,
                "arguments": {"operation_id": "owner"},
            }
        )
        await transport.incoming.put(state_running(session_id, "later-turn"))
        await transport.incoming.put(_permission("later-wait", "make test"))
        kind, unowned = await until(events, lambda kind, value: kind == "permission")
        assert unowned["operation_id"] is None and "owner_operation_id" not in unowned
        assert unowned["payload"]["unowned"] is True
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_session_view_lists_inputs_written_but_not_taken_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, transports, _, events = await started(tmp_path, monkeypatch)
    try:
        prompt = await driver.execute(
            {"id": "owner", "action": "prompt", "arguments": {"prompt": "work"}}
        )
        session_id = prompt["session_id"]
        await until(events, lambda kind, value: value["operation_id"] == "owner")
        transports[0].echo = False
        queued = await driver.execute(
            {
                "id": "later",
                "action": "queue",
                "session_id": session_id,
                "arguments": {"prompt": "after this"},
            }
        )

        async def inspect(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return {"session_id": session_id}

        monkeypatch.setattr(driver, "_inspect", inspect)
        view = await driver.execute(
            {"id": "read", "action": "get", "session_id": session_id, "arguments": {}}
        )
        assert view["queued_inputs"] == [
            {"execution_id": queued["execution_id"], "operation_id": "later"}
        ]
    finally:
        await driver.stop()
