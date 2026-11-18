"""Connector 0.9.0 review findings, each written as a failing case before its fix.

- B1: a release detach of an operation that already finished never reaches the
  driver, so Yuki's idle native process keeps holding the session; a plain detach
  of a finished operation leaves it as the owner of later self-started turns;
- M1: a Codex scope that does not cover the thread cwd is silently widened by
  workspaceWrite (cwd, /tmp and $TMPDIR are writable);
- M2: cancelling a queued scoped Codex item leaves the narrowed settings on the
  thread; a failed restore is silent; a restart forgets the original settings;
- M3: ACP mode/command updates start a follow-up turn; a follow-up Yuki ends is
  left pending forever;
- M4: an input merged into a running follow-up turn renames that turn's owner;
- M5: an owner answer that persists beyond the one request is accepted;
- M6: a queued Codex prompt is not recovered after a restart;
- S4: a Windows process the connector may not query counts as exited;
- S7: a duplicate detach while the original runs sends a placeholder receipt;
- S8: per-turn and per-operation driver state is never released; a failed ACP
  restart is silent.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from test_acp_phase2 import AGENT, permission, prompt, traced_until
from test_acp_phase2 import started as acp_started
from test_acp_phase2 import until as acp_until
from test_claude_driver import NativeTransport, _result
from test_claude_phase2 import started as claude_started
from test_claude_phase2 import state_running
from test_claude_phase2 import until as claude_until
from test_codex_phase2 import AppServer, connected, settled
from test_codex_phase2 import command as codex_command
from test_codex_phase2 import until as codex_until
from websockets.asyncio.server import serve

from yuki_agent_connector.config import Config, Instance
from yuki_agent_connector.connector import Connector, instance_source
from yuki_agent_connector.drivers import DriverError
from yuki_agent_connector.drivers import claude as claude_module
from yuki_agent_connector.journal import Journal


def receipt(journal: Journal, key: str) -> dict[str, Any]:
    rows = journal.db.execute(
        "SELECT event FROM events WHERE json_extract(event, '$.kind')='receipt' "
        "AND json_extract(event, '$.operation_id')=? ORDER BY sequence",
        (key,),
    ).fetchall()
    return json.loads(rows[-1][0])["payload"]["result"]  # type: ignore[no-any-return]


def journal_events(journal: Journal) -> list[dict[str, Any]]:
    return [json.loads(row[0]) for row in journal.db.execute("SELECT event FROM events")]


async def eventually(check: Any) -> None:
    async with asyncio.timeout(5):
        while not check():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


def agent_command(identity: str, action: str, **values: Any) -> dict[str, Any]:
    arguments = values.pop("arguments", {})
    return {
        "type": "command",
        "id": identity,
        "instance_id": values.pop("instance", "claude"),
        "action": action,
        "arguments": arguments,
        **values,
    }


async def claude_connector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Connector, Journal, list[NativeTransport]]:
    transports: list[NativeTransport] = []

    def factory(*, options: ClaudeAgentOptions) -> ClaudeSDKClient:
        transport = NativeTransport()
        transports.append(transport)
        return ClaudeSDKClient(options=options, transport=transport)

    monkeypatch.setattr(claude_module, "ClaudeSDKClient", factory)
    profile = tmp_path / "profile"
    (profile / "sessions").mkdir(parents=True)
    spec = Instance(
        "claude",
        "claude",
        "Claude",
        sys.executable,
        cwd=str(tmp_path),
        env={"CLAUDE_CONFIG_DIR": str(profile)},
    )
    journal = Journal(tmp_path / "journal.db", namespace="server/device")
    connector = Connector(Config("ws://localhost/", "device", "token", [spec]), journal)
    await connector.start_drivers()
    await eventually(lambda: "claude" in connector.drivers and "claude" not in connector.errors)
    return connector, journal, transports


async def finished_prompt(
    connector: Connector, journal: Journal, transports: list[NativeTransport]
) -> str:
    assert (
        await connector.dispatch(agent_command("delegated", "prompt", arguments={"prompt": "go"}))
        is None
    )
    session_id = receipt(journal, "delegated")["session_id"]
    await eventually(lambda: journal.operation("delegated").get("admitted") is True)
    await transports[0].incoming.put(_result(session_id, "done"))
    await eventually(lambda: journal.operation("delegated")["status"] == "completed")
    await eventually(lambda: "delegated" not in connector.operations)
    return session_id  # type: ignore[no-any-return]


async def shutdown(connector: Connector) -> None:
    for task in connector._startup:
        task.cancel()
    await asyncio.gather(*connector._startup, *connector.tasks, return_exceptions=True)
    for driver in list(connector.drivers.values()):
        await driver.stop()
    connector.journal.close()


@pytest.mark.asyncio
async def test_b1_release_of_a_finished_delegation_closes_the_idle_claude_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector, journal, transports = await claude_connector(tmp_path, monkeypatch)
    try:
        session_id = await finished_prompt(connector, journal, transports)
        assert transports[0].closed is False
        await connector.dispatch(
            agent_command(
                "release",
                "detach",
                session_id=session_id,
                arguments={"operation_id": "delegated", "release": True},
            )
        )
        released = receipt(journal, "release")
        assert released["detached"] is True and released["closed"] == "now"
        assert transports[0].closed is True
        driver = connector.drivers["claude"]
        assert driver.sessions[session_id].stopped.is_set()  # type: ignore[attr-defined]
    finally:
        await shutdown(connector)


@pytest.mark.asyncio
async def test_b1_detached_finished_operation_owns_no_later_self_started_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector, journal, transports = await claude_connector(tmp_path, monkeypatch)
    try:
        session_id = await finished_prompt(connector, journal, transports)
        await connector.dispatch(
            agent_command(
                "detach",
                "detach",
                session_id=session_id,
                arguments={"operation_id": "delegated"},
            )
        )
        await transports[0].incoming.put(state_running(session_id, "later-turn"))
        await transports[0].incoming.put(
            {
                "type": "control_request",
                "request_id": "request-late",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Bash",
                    "tool_use_id": "late",
                    "input": {"command": "make"},
                },
            }
        )
        await eventually(
            lambda: any(item["kind"] == "permission" for item in journal_events(journal))
        )
        [waiting] = [item for item in journal_events(journal) if item["kind"] == "permission"]
        assert waiting["operation_id"] is None and "owner_operation_id" not in waiting
        assert waiting["payload"]["unowned"] is True
        # The released owner can then be closed: no detached operation holds it.
        await connector.dispatch(
            agent_command(
                "release",
                "detach",
                session_id=session_id,
                arguments={"operation_id": "delegated", "release": True},
            )
        )
        assert receipt(journal, "release")["closed"] in {"now", "after_turn"}
    finally:
        await shutdown(connector)


@pytest.mark.asyncio
async def test_b1_release_of_a_finished_acp_delegation_closes_its_session(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "trace.jsonl"
    spec = Instance(
        "acp",
        "acp",
        "ACP",
        sys.executable,
        args=[str(AGENT)],
        cwd=str(tmp_path),
        env={"FAKE_ACP_TRACE": str(trace)},
    )
    journal = Journal(tmp_path / "journal.db", namespace="server/device")
    connector = Connector(Config("ws://localhost/", "device", "token", [spec]), journal)
    await connector.start_drivers()
    await eventually(lambda: "acp" in connector.drivers and "acp" not in connector.errors)
    try:
        await connector.dispatch(
            agent_command("delegated", "prompt", instance="acp", arguments={"prompt": "{}"})
        )
        session_id = receipt(journal, "delegated")["session_id"]
        await eventually(lambda: journal.operation("delegated")["status"] == "completed")
        await eventually(lambda: "delegated" not in connector.operations)
        await connector.dispatch(
            agent_command(
                "release",
                "detach",
                instance="acp",
                session_id=session_id,
                arguments={"operation_id": "delegated", "release": True},
            )
        )
        assert receipt(journal, "release")["closed"] == "now"
        [closed] = await traced_until(trace, "session/close")
        assert closed["session"] == session_id
    finally:
        await shutdown(connector)


@pytest.mark.asyncio
async def test_m1_codex_scope_not_covering_the_thread_cwd_is_refused(tmp_path: Path) -> None:
    owner = AppServer()  # Thread cwd /user/project.
    work = tmp_path / "work"
    work.mkdir()
    async with serve(owner.handler, "127.0.0.1", 0) as listener:
        driver, _ = await connected(owner, listener, tmp_path)
        try:
            with pytest.raises(DriverError) as refused:
                await driver.execute(
                    codex_command(
                        "prompt", "scoped", prompt="x", scope={"write_paths": [str(work)]}
                    )
                )
            assert refused.value.code == "scope_unsupported"
            assert "thread/settings/update" not in owner.methods()
            assert "thread/queue/add" not in owner.methods()
        finally:
            await driver.stop()


@pytest.mark.asyncio
async def test_m2_cancelled_queued_scoped_item_restores_the_thread_settings(
    tmp_path: Path,
) -> None:
    owner = AppServer()
    owner.active = "user-turn"  # The user's turn runs: Yuki's item waits in the queue.
    async with serve(owner.handler, "127.0.0.1", 0) as listener:
        driver, _ = await connected(owner, listener, tmp_path)
        try:
            await driver.execute(codex_command("queue", "scoped", prompt="x", scope={}))
            await settled(owner, 1)
            await driver.execute(codex_command("cancel", "cancel", operation_id="scoped"))
            await settled(owner, 2)
            restored = [c for c in owner.calls if c.get("method") == "thread/settings/update"][-1]
            assert restored["params"]["approvalPolicy"] == "never"
        finally:
            await driver.stop()


@pytest.mark.asyncio
async def test_m2_failed_restore_is_reported_not_silent(tmp_path: Path) -> None:
    owner = AppServer()
    owner.settings_failures = {2}
    async with serve(owner.handler, "127.0.0.1", 0) as listener:
        driver, events = await connected(owner, listener, tmp_path)
        try:
            await driver.execute(codex_command("prompt", "scoped", prompt="x", scope={}))
            await codex_until(events, lambda kind, value: value["operation_id"] == "scoped")
            await owner.complete("turn-1")
            _, failed = await codex_until(
                events, lambda kind, value: "scope_restore_failed" in value["payload"]
            )
            assert failed["operation_id"] == "scoped" and failed["session_id"] == "thread"
            assert failed["payload"]["thread_settings"]["approvalPolicy"] == "never"
        finally:
            await driver.stop()


@pytest.mark.asyncio
async def test_m2_m6_restart_recovers_queued_prompt_scope_and_original_settings(
    tmp_path: Path,
) -> None:
    owner = AppServer()
    owner.active = "user-turn"
    async with serve(owner.handler, "127.0.0.1", 0) as listener:
        first, _ = await connected(owner, listener, tmp_path)
        original = codex_command("queue", "scoped", prompt="x", scope={})
        try:
            submitted = await first.execute(original)
        finally:
            await first.stop()
        # The persisted receipt carries the thread's own settings and the race fact.
        assert submitted["thread_settings_before"]["approvalPolicy"] == "never"
        assert submitted["scope_settings"] == "thread"
        restarted, events = await connected(owner, listener, tmp_path)
        try:
            await restarted.recover(original, submitted)
            assert restarted.queue_operations.get("scoped") == submitted["queued_submission_id"]
            owner.active = None
            await owner.start_turn("scoped")
            await codex_until(events, lambda kind, value: value.get("execution_id") == "turn-1")
            await owner.complete("turn-1")
            await settled(owner, 2)
            restored = [c for c in owner.calls if c.get("method") == "thread/settings/update"][-1]
            assert restored["params"]["approvalPolicy"] == "never"
        finally:
            await restarted.stop()


@pytest.mark.asyncio
async def test_m3_mode_updates_start_no_followup_and_yuki_ends_followups_terminally(
    tmp_path: Path,
) -> None:
    driver, events, _ = await acp_started(tmp_path)
    try:
        commands = {
            "update": {"sessionUpdate": "available_commands_update", "availableCommands": []}
        }
        first = await driver.execute(prompt("owner", {"after": [commands]}))
        await acp_until(events, lambda kind, value: kind == "work_finished")
        await asyncio.sleep(0.1)  # The update after the prompt's response is handled.
        assert driver.sessions[first["session_id"]].followup is None
        tool = {"tool": {"toolCallId": "bg", "title": "bg", "kind": "execute"}}
        await driver.execute(
            {**prompt("again", {"after": [tool]}), "session_id": first["session_id"]}
        )
        _, begun = await acp_until(events, lambda kind, value: "followup_execution" in value)
        await driver.execute({**prompt("third", {}), "session_id": first["session_id"]})
        kind, ended = await acp_until(
            events,
            lambda kind, value: (
                value.get("followup_execution") == begun["followup_execution"]
                and value["payload"].get("pending") is False
            ),
        )
        assert kind == "completed" and ended["payload"]["status"] == "cancelled"
        assert ended["payload"]["reason"] == "native_turn_end_not_reported"
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_m4_input_merged_into_a_followup_keeps_the_followup_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, transports, _, events = await claude_started(tmp_path, monkeypatch)
    try:
        submitted = await driver.execute(
            {"id": "first", "action": "prompt", "arguments": {"prompt": "work"}}
        )
        session_id = submitted["session_id"]
        transport = transports[0]
        await claude_until(events, lambda kind, value: value["operation_id"] == "first")
        await transport.incoming.put(_result(session_id, "done"))
        await claude_until(events, lambda kind, value: kind == "work_finished")
        await transport.incoming.put(state_running(session_id, "wake"))
        await claude_until(events, lambda kind, value: value.get("followup_execution") == "wake")
        transport.echo = False
        await driver.execute(
            {
                "id": "second",
                "action": "queue",
                "session_id": session_id,
                "arguments": {"prompt": "also"},
            }
        )
        await transport.take_up()
        await transport.incoming.put(_result(session_id, "both done"))
        completed = []
        while len(completed) < 2:
            _kind, value = await claude_until(events, lambda kind, value: kind == "completed")
            completed.append(value)
        owners = {value.get("owner_operation_id") for value in completed} - {None}
        assert owners == {"first"}
        assert {value["operation_id"] for value in completed} == {None, "second"}
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_m5_acp_owner_answer_must_not_persist_beyond_the_request(tmp_path: Path) -> None:
    driver, events, _ = await acp_started(tmp_path)
    script = {"during": [permission("once", "execute", raw={"command": "make"})]}
    try:
        task = asyncio.create_task(driver.execute(prompt("owner", script)))
        _, waiting = await acp_until(events, lambda kind, value: kind == "permission")
        answer = {
            "action": "respond",
            "session_id": waiting["session_id"],
            "execution_id": waiting["execution_id"],
            "arguments": {
                "operation_id": "owner",
                "connection_id": driver.owner,
                "permission_id": waiting["payload"]["permission_id"],
            },
        }
        with pytest.raises(DriverError) as refused:
            await driver.execute(
                {
                    **answer,
                    "id": "always",
                    "arguments": {**answer["arguments"], "option_id": "always"},
                }
            )
        assert refused.value.code == "one_time_only"
        assert refused.value.details == {"options": ["allow", "reject"]}
        await driver.execute(
            {**answer, "id": "once", "arguments": {**answer["arguments"], "option_id": "allow"}}
        )
        await task
    finally:
        await driver.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    ["acceptForSession", {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["make"]}}],
)
async def test_m5_codex_owner_answer_must_not_persist_beyond_the_request(
    tmp_path: Path, decision: Any
) -> None:
    owner = AppServer()
    async with serve(owner.handler, "127.0.0.1", 0) as listener:
        driver, events = await connected(owner, listener, tmp_path)
        try:
            await driver.execute(codex_command("prompt", "yuki", prompt="x"))
            await codex_until(events, lambda kind, value: value["operation_id"] == "yuki")
            await owner.request(
                5,
                "item/commandExecution/requestApproval",
                "turn-1",
                command="make",
                availableDecisions=["accept", "acceptForSession", "decline", "cancel"],
            )
            await codex_until(events, lambda kind, value: kind == "permission")
            with pytest.raises(DriverError) as refused:
                await driver.execute(
                    codex_command(
                        "respond",
                        "answer",
                        execution="turn-1",
                        operation_id="yuki",
                        permission_id="5",
                        connection_id=driver.rpc.connection_id,
                        response={"decision": decision},
                    )
                )
            assert refused.value.code == "one_time_only"
            assert refused.value.details == {"options": ["accept", "decline", "cancel"]}
            assert owner.answers.empty()
        finally:
            await driver.stop()


def test_s4_windows_access_denied_process_is_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    class Kernel:
        def __init__(self, error: int) -> None:
            self.error = error

        def OpenProcess(self, *args: Any) -> int:
            return 0

        def GetLastError(self) -> int:
            return self.error

    monkeypatch.setattr(claude_module.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", type("W", (), {"kernel32": Kernel(5)}), raising=False)
    assert claude_module.pid_alive(4) is True
    monkeypatch.setattr(ctypes, "windll", type("W", (), {"kernel32": Kernel(87)}), raising=False)
    assert claude_module.pid_alive(4) is False


@pytest.mark.asyncio
async def test_s7_duplicate_detach_while_dispatching_sends_no_placeholder_receipt(
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()

    class Driver:
        spec = Instance("claude", "fake", "Fake", "fake")
        capabilities: tuple[str, ...] = ()

        async def execute(self, command: dict[str, Any]) -> dict[str, Any]:
            await gate.wait()
            return {"status": "completed", "detached": True, "admitted": True}

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

        async def recover(self, *args: Any) -> None:
            pass

    journal = Journal(tmp_path / "journal.db", namespace="server/device")
    connector = Connector(Config("ws://localhost/", "device", "token"), journal)
    driver = Driver()
    connector.drivers["claude"] = driver  # type: ignore[assignment]
    connector.operations["watch"] = driver  # type: ignore[assignment]
    journal.begin(
        agent_command("watch", "watch", session_id="s"), owner_source=instance_source(driver.spec)
    )
    detach = agent_command("detach", "detach", arguments={"operation_id": "watch"})
    try:
        running = asyncio.create_task(connector.dispatch(detach))
        await asyncio.sleep(0.05)
        assert await connector.dispatch(detach) is None
        assert [item for item in journal_events(journal) if item["kind"] == "receipt"] == []
        gate.set()
        await running
        assert receipt(journal, "detach")["detached"] is True
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_s8_codex_item_paths_end_with_their_turn(tmp_path: Path) -> None:
    owner = AppServer()
    async with serve(owner.handler, "127.0.0.1", 0) as listener:
        driver, events = await connected(owner, listener, tmp_path)
        try:
            await driver.execute(codex_command("prompt", "yuki", prompt="x"))
            await codex_until(events, lambda kind, value: value["operation_id"] == "yuki")
            await owner.notify(
                "item/started",
                {
                    "threadId": "thread",
                    "turnId": "turn-1",
                    "item": {"id": "patch", "type": "fileChange", "changes": [{"path": "/a"}]},
                },
            )
            await owner.complete("turn-1")
            await codex_until(events, lambda kind, value: kind == "completed")
            assert not driver.item_paths
        finally:
            await driver.stop()


@pytest.mark.asyncio
async def test_s8_claude_scope_of_a_superseded_or_detached_operation_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, transports, _, events = await claude_started(tmp_path, monkeypatch)
    try:
        first = await driver.execute(
            {"id": "first", "action": "prompt", "arguments": {"prompt": "a", "scope": {}}}
        )
        session_id = first["session_id"]
        await claude_until(events, lambda kind, value: value["operation_id"] == "first")
        await transports[0].incoming.put(_result(session_id, "done"))
        await claude_until(events, lambda kind, value: kind == "work_finished")
        await driver.execute(
            {
                "id": "second",
                "action": "prompt",
                "session_id": session_id,
                "arguments": {"prompt": "b", "scope": {}},
            }
        )
        await claude_until(events, lambda kind, value: value["operation_id"] == "second")
        assert set(driver.scopes) == {"second"}
        await driver.execute(
            {
                "id": "detach",
                "action": "detach",
                "session_id": session_id,
                "arguments": {"operation_id": "second"},
            }
        )
        assert driver.scopes == {}
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_s8_failed_acp_restart_reports_the_instance_unavailable(tmp_path: Path) -> None:
    driver, events, _ = await acp_started(tmp_path, FAKE_ACP_NO_CLOSE="1")
    try:
        idle = await driver.execute(prompt("idle", {}))
        await acp_until(events, lambda kind, value: kind == "work_finished")

        async def broken_start() -> None:
            raise FileNotFoundError("agent removed")

        driver.start = broken_start  # type: ignore[method-assign]
        closed = await driver.execute(
            {
                "id": "release",
                "action": "detach",
                "session_id": idle["session_id"],
                "arguments": {"operation_id": "idle", "release": True},
            }
        )
        assert closed["closed"] == "now"
        _, lost = await acp_until(events, lambda kind, value: kind == "connection")
        assert lost["status"] == "unavailable"
        assert lost["reason"] == "native_acp_restart_failed"
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_fork_receipt_names_the_new_session_and_its_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, _, configurations, _ = await claude_started(tmp_path, monkeypatch)

    async def inspect(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"native": {"cwd": str(tmp_path)}}

    monkeypatch.setattr(driver, "_inspect", inspect)
    try:
        forked = await driver.execute(
            {
                "id": "fork",
                "action": "prompt",
                "session_id": "original",
                "arguments": {"prompt": "go", "resume": True, "fork": True},
            }
        )
        assert forked["session_id"] == configurations[0].session_id != "original"
        assert forked["forked_from"] == "original"
    finally:
        await driver.stop()
