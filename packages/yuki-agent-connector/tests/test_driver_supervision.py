from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from test_codex_driver import Owner, command
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from yuki_agent_connector.config import Instance
from yuki_agent_connector.drivers import DriverError
from yuki_agent_connector.drivers.acp import ACPDriver, Session
from yuki_agent_connector.drivers.claude import ClaudeDriver, ManagedSession
from yuki_agent_connector.drivers.codex import CodexDriver


@pytest.mark.asyncio
async def test_codex_eof_reconnect_preserves_queue_and_rejects_old_connection_question():
    owner = Owner()
    events = []
    reconnected = asyncio.Event()

    async def emit(kind, value):
        events.append((kind, value))
        if kind == "connection" and value["status"] == "available":
            reconnected.set()

    async with serve(owner.handler, "127.0.0.1", 0) as server:
        driver = CodexDriver(
            Instance(
                "c",
                "codex",
                "c",
                "unused",
                endpoint=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
            ),
            emit,
        )
        await driver.start()
        try:
            original_connection = driver.rpc.connection_id
            await driver.execute(command("queue", identity="queue", prompt="once"))
            await driver.execute(
                {**command("watch", identity="watch"), "observation_scope": "session"}
            )
            await driver._notification(
                {
                    "id": 7,
                    "method": "item/tool/requestUserInput",
                    "params": {"threadId": "thread", "turnId": "turn"},
                }
            )
            await owner.socket.close()
            await asyncio.wait_for(reconnected.wait(), 4)
            assert original_connection != driver.rpc.connection_id
            assert driver.requests == {}
            lost = [
                value
                for kind, value in events
                if kind == "progress"
                and value["payload"].get("reason") == "native_event_stream_lost"
            ]
            assert {item["operation_id"] for item in lost} == {"watch", "queue"}
            assert all(item["payload"]["status"] == "unknown" for item in lost)
            assert (
                len([call for call in owner.calls if call.get("method") == "thread/queue/add"]) == 1
            )
            await driver._notification(
                {
                    "id": 7,
                    "method": "item/tool/requestUserInput",
                    "params": {"threadId": "thread", "turnId": "turn"},
                }
            )
            with pytest.raises(DriverError, match="connection"):
                await driver.execute(
                    command(
                        "respond",
                        execution="turn",
                        permission_id="7",
                        connection_id=original_connection,
                        response={"answers": {}},
                    )
                )
            assert "7" in driver.requests
            await driver.execute(
                command(
                    "respond",
                    execution="turn",
                    permission_id="7",
                    connection_id=driver.rpc.connection_id,
                    response={"answers": {}},
                )
            )
            assert (await asyncio.wait_for(owner.responses.get(), 1))["id"] == 7
        finally:
            before = len(events)
            await driver.stop()
            assert len(events) == before


@pytest.mark.asyncio
async def test_cancelled_rpc_response_does_not_disconnect_other_original_calls():
    owner = Owner()
    events = []

    async def emit(kind, value):
        events.append((kind, value))

    async with serve(owner.handler, "127.0.0.1", 0) as server:
        driver = CodexDriver(
            Instance(
                "c",
                "codex",
                "c",
                "unused",
                endpoint=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
            ),
            emit,
        )
        await driver.start()
        try:
            original_connection = driver.rpc.connection_id
            cancelled = asyncio.get_running_loop().create_future()
            driver.rpc.pending["cancelled"] = cancelled
            cancelled.cancel()
            await driver.rpc.send({"id": "cancelled", "method": "thread/list", "params": {}})
            result = await asyncio.wait_for(driver.execute(command("list")), 1)
            assert result["sessions"][0]["id"] == "thread"
            assert driver.rpc.connection_id == original_connection and events == []
            driver.rpc.pending.pop("cancelled")
        finally:
            await driver.stop()


@pytest.mark.asyncio
async def test_recovery_send_on_closed_socket_keeps_original_unknown_without_stopping_owner():
    events = []

    async def emit(kind, value):
        events.append((kind, value))

    async def peer(socket):
        await socket.close()

    async with serve(peer, "127.0.0.1", 0) as server:
        async with connect(
            f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", proxy=None
        ) as socket:
            await socket.wait_closed()
            driver = CodexDriver(Instance("c", "codex", "c", "unused"), emit)
            driver.rpc.ws = socket
            await driver.recover(
                command("watch", execution="A"), {"session_id": "thread", "execution_id": "A"}
            )
            assert events[-1][1]["payload"]["status"] == "unknown"
            assert events[-1][1]["payload"]["error_code"] == "unknown"
            assert events[-1][1]["operation_id"] == "operation"
            assert driver.watches == {"operation": "thread"}
            assert driver.rpc.pending == {}


@pytest.mark.asyncio
async def test_session_watch_crosses_turns_single_execution_ends_and_detach_removes_prompt():
    events = []

    async def emit(kind, value):
        events.append((kind, value))

    driver = CodexDriver(Instance("c", "codex", "c", "unused"), emit)
    driver.watches.update(session="thread", single="thread")
    driver.watch_turns.update(session=None, single="A")
    driver.turn_operations["A"] = "prompt"
    driver.turn_sessions["A"] = "thread"
    await driver.execute(command("detach", operation_id="prompt"))
    for turn in ("A", "B"):
        await driver._notification(
            {
                "method": "turn/started",
                "params": {"threadId": "thread", "turn": {"id": turn, "status": "inProgress"}},
            }
        )
        await driver._notification(
            {
                "method": "turn/completed",
                "params": {"threadId": "thread", "turn": {"id": turn, "status": "completed"}},
            }
        )
    session_events = [(kind, event) for kind, event in events if event["operation_id"] == "session"]
    assert len(session_events) == 4
    assert all(kind == "progress" for kind, _ in session_events)
    assert [event["execution_id"] for _, event in session_events] == ["A", "A", "B", "B"]
    assert session_events[-1][1]["payload"]["status"] == "idle"
    assert driver.watches == {"session": "thread"}
    assert all(event["operation_id"] != "prompt" for _, event in events)
    assert len([event for kind, event in events if kind == "completed"]) == 1


@pytest.mark.asyncio
async def test_paginated_loaded_and_turns_find_original_and_missing_execution_stays_unknown():
    calls = []

    async def emit(kind, value):
        pass

    async def call(method, params):
        calls.append((method, params))
        if method == "thread/loaded/list":
            return (
                {"data": ["thread"]}
                if params.get("cursor")
                else {"data": ["other"], "nextCursor": "second"}
            )
        if method == "thread/turns/list":
            return (
                {"data": [{"id": "old", "status": "completed"}]}
                if params.get("cursor")
                else {"data": [{"id": "new", "status": "inProgress"}], "nextCursor": "older"}
            )
        return {"thread": {"id": "thread", "status": {"type": "idle"}}}

    driver = CodexDriver(Instance("c", "codex", "c", "unused"), emit)
    driver.rpc = SimpleNamespace(call=call)
    found = await driver.execute(command("watch", execution="old"))
    assert found["execution_id"] == "old" and found["status"] == "completed"
    missing = await driver.execute(command("get", execution="absent"))
    assert missing["status"] == "unknown" and missing["pending"]
    assert missing["reason"] == "execution_not_found"
    assert all(
        params.get("itemsView") == "notLoaded"
        for method, params in calls
        if method == "thread/turns/list"
    )
    assert all(
        params.get("includeTurns") is False for method, params in calls if method == "thread/read"
    )


@pytest.mark.asyncio
async def test_unknown_prompt_receipt_reconciles_client_id_without_dispatching():
    events = []
    calls = []

    async def emit(kind, value):
        events.append((kind, value))

    async def call(method, params):
        calls.append(method)
        assert method == "thread/turns/list"
        return {
            "data": [
                {
                    "id": "original",
                    "status": "completed",
                    "items": [{"type": "userMessage", "clientId": "prompt-op"}],
                }
            ]
        }

    driver = CodexDriver(Instance("c", "codex", "c", "unused"), emit)
    driver.rpc = SimpleNamespace(call=call)
    await driver.recover(
        {**command("prompt", identity="prompt-op"), "observation_scope": "execution"},
        {"status": "unknown"},
    )
    assert events[0][0] == "completed"
    assert events[0][1]["execution_id"] == "original"
    assert calls == ["thread/turns/list"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_caller", [False, True])
async def test_detach_joins_inflight_original_recovery_before_returning(cancel_caller):
    reading, released = asyncio.Event(), asyncio.Event()
    events = []

    async def emit(kind, value):
        events.append((kind, value))

    async def call(method, params):
        assert method == "thread/turns/list"
        reading.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    driver = CodexDriver(Instance("c", "codex", "c", "unused"), emit)
    driver.rpc = SimpleNamespace(call=call)
    recovering = asyncio.create_task(
        driver.recover(
            command("prompt", execution="A"), {"session_id": "thread", "execution_id": "A"}
        )
    )
    await asyncio.wait_for(reading.wait(), 1)
    if cancel_caller:
        recovering.cancel()
        with pytest.raises(asyncio.CancelledError):
            await recovering
    else:
        await driver.execute(command("detach", operation_id="operation"))
        await asyncio.wait_for(recovering, 1)
    assert released.is_set() and not driver.recoveries and not driver.turn_operations
    await driver._notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread",
                "turnId": "A",
                "item": {"type": "agentMessage", "text": "late progress"},
            },
        }
    )
    assert events == []


@pytest.mark.asyncio
async def test_session_recovery_follows_latest_turn_instead_of_pinning_previous_receipt():
    events, calls = [], []

    async def emit(kind, value):
        events.append((kind, value))

    async def call(method, params):
        calls.append((method, params))
        if method == "thread/turns/list":
            return {
                "data": [{"id": "B", "status": "inProgress"}, {"id": "A", "status": "completed"}],
                "nextCursor": "older",
            }
        if method == "thread/loaded/list":
            return {"data": ["thread"]}
        assert method == "thread/resume" and params["excludeTurns"]
        return {"thread": {"id": "thread"}}

    driver = CodexDriver(Instance("c", "codex", "c", "unused"), emit)
    driver.rpc = SimpleNamespace(call=call)
    await driver.recover(
        {**command("watch"), "observation_scope": "session"},
        {"session_id": "thread", "execution_id": "A", "status": "unknown"},
    )
    assert events[-1][1]["execution_id"] == "B"
    assert events[-1][1]["payload"]["status"] == "running"
    assert driver.watch_turns["operation"] is None
    assert len([method for method, _ in calls if method == "thread/turns/list"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("driver_type", [ClaudeDriver, ACPDriver])
@pytest.mark.parametrize("session_known", [True, False])
async def test_sdk_session_watch_survives_terminal_and_detach_stops_original_upload(
    driver_type, session_known
):
    events = []

    async def emit(kind, value):
        events.append((kind, value))

    driver = driver_type(
        Instance("c", "claude" if driver_type is ClaudeDriver else "acp", "c", "unused"), emit
    )
    if driver_type is ClaudeDriver:
        session = ManagedSession(
            "session", SimpleNamespace(), asyncio.get_running_loop().create_future()
        )
    else:
        session = Session("session", ".")
    session.execution, session.operation = "A", "prompt"
    session.watches = {"continuous": None, "single": "A"}
    driver.sessions[session.id] = session
    await driver.execute(
        {
            "id": "detach",
            "action": "detach",
            "session_id": "session" if session_known else None,
            "arguments": {"operation_id": "prompt"},
        }
    )
    await driver._publish(session, "completed", {"status": "completed"})
    assert {event["operation_id"] for _, event in events} == {"continuous", "single"}
    assert next(kind for kind, event in events if event["operation_id"] == "single") == "completed"
    continuous = next(
        (kind, event) for kind, event in events if event["operation_id"] == "continuous"
    )
    assert continuous[0] == "progress" and continuous[1]["payload"]["status"] == "idle"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [("running", "unknown"), ("completed", "completed")])
async def test_claude_normal_eof_marks_original_unknown_without_fabricating_recovery(
    status, expected
):
    events = []

    async def emit(kind, value):
        events.append((kind, value))

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def receive_messages(self):
            for message in ():
                yield message

    driver = ClaudeDriver(Instance("c", "claude", "c", "unused"), emit)
    session = ManagedSession(
        "session",
        Client(),
        asyncio.get_running_loop().create_future(),
        operation="original",
        execution="A",
        status=status,
        active=True,
    )
    await driver._serve(session)
    assert session.status == expected and not session.active and session.stopped.is_set()
    assert events[0][1]["payload"]["reason"] == "native_sdk_connection_lost"
    assert events[0][1]["operation_id"] == "original"


@pytest.mark.asyncio
async def test_acp_initializing_owner_is_joined_when_start_is_cancelled(monkeypatch):
    entered, closed = asyncio.Event(), asyncio.Event()

    class Connection:
        async def initialize(self, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    @asynccontextmanager
    async def spawn(*args, **kwargs):
        try:
            yield Connection(), SimpleNamespace()
        finally:
            closed.set()

    async def emit(kind, value):
        pass

    monkeypatch.setattr("yuki_agent_connector.drivers.acp.spawn_agent_process", spawn)
    driver = ACPDriver(Instance("c", "acp", "c", "unused"), emit)
    startup = asyncio.create_task(driver.start())
    await asyncio.wait_for(entered.wait(), 1)
    startup.cancel()
    await asyncio.gather(startup, return_exceptions=True)
    await asyncio.wait_for(driver.stop(), 1)
    assert closed.is_set() and driver.worker.done()


@pytest.mark.asyncio
async def test_acp_process_exit_updates_native_status_and_clears_old_questions(monkeypatch):
    exited = asyncio.Event()
    events = []

    class Connection:
        async def initialize(self, **kwargs):
            return SimpleNamespace(
                agent_capabilities=SimpleNamespace(model_dump=lambda **kwargs: {})
            )

    @asynccontextmanager
    async def spawn(*args, **kwargs):
        yield Connection(), SimpleNamespace(wait=exited.wait)

    async def emit(kind, value):
        events.append((kind, value))

    monkeypatch.setattr("yuki_agent_connector.drivers.acp.spawn_agent_process", spawn)
    driver = ACPDriver(Instance("c", "acp", "c", "unused"), emit)
    await driver.start()
    session = Session("session", ".", status="running", operation="original", execution="A")
    driver.sessions[session.id] = session
    question = asyncio.get_running_loop().create_future()
    driver.requests["7"] = (session.id, [], question)
    driver.permission_refs[(session.id, "tool")] = 7
    exited.set()
    await asyncio.wait_for(driver.worker, 1)
    assert session.status == "unknown" and driver.connection is None
    assert question.cancelled() and not driver.permission_refs
    assert events[0][0] == "connection" and events[0][1]["status"] == "unavailable"
    assert events[1][1]["operation_id"] == "original"


@pytest.mark.asyncio
async def test_meaningful_items_share_mapping_and_full_diff_is_not_uploaded():
    events = []

    async def emit(kind, value):
        events.append(value)

    driver = CodexDriver(Instance("c", "codex", "c", "unused"), emit)
    driver.watches["watch"] = "thread"
    driver.watch_turns["watch"] = None
    for kind in ("collabAgentToolCall", "dynamicToolCall", "mcpToolCall", "reasoning"):
        await driver._notification(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "turnId": "A",
                    "item": {
                        "id": "item",
                        "type": kind,
                        "status": "completed",
                        "arguments": "private full input",
                    },
                },
            }
        )
    await driver._notification(
        {
            "method": "turn/diff/updated",
            "params": {"threadId": "thread", "turnId": "A", "diff": "full patch"},
        }
    )
    assert len(events) == 3
    assert all("arguments" not in event["payload"]["item"] for event in events)
