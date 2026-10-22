from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk._internal.transport import Transport
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from websockets.asyncio.server import serve

from yuki_agent_connector.config import Config, Instance
from yuki_agent_connector.connector import Connector
from yuki_agent_connector.drivers import DriverError
from yuki_agent_connector.drivers import claude as module
from yuki_agent_connector.journal import Journal


class NativeTransport(Transport):
    """Real SDK parsing/control, deterministic native wire, no paid model."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.writes: list[dict[str, Any]] = []
        self.prompt = asyncio.Event()
        self.response = asyncio.Event()
        self.closed = False

    async def connect(self) -> None:
        pass

    async def write(self, data: str) -> None:
        message = json.loads(data)
        self.writes.append(message)
        if message["type"] == "control_request":
            await self.incoming.put(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": message["request_id"],
                        "response": {},
                    },
                }
            )
        elif message["type"] == "user":
            self.prompt.set()
            await self.incoming.put(message)
        elif message["type"] == "control_response":
            self.response.set()

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        while (message := await self.incoming.get()) is not None:
            yield message

    async def close(self) -> None:
        self.closed = True
        await self.incoming.put(None)

    def is_ready(self) -> bool:
        return not self.closed

    async def end_input(self) -> None:
        pass


@pytest.mark.asyncio
async def test_real_sdk_native_preset_permissions_completion_and_no_arbitrary_attach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transports: list[NativeTransport] = []
    configurations: list[ClaudeAgentOptions] = []

    def factory(*, options: ClaudeAgentOptions) -> ClaudeSDKClient:
        transport = NativeTransport()
        transports.append(transport)
        configurations.append(options)
        return ClaudeSDKClient(options=options, transport=transport)

    monkeypatch.setattr(module, "ClaudeSDKClient", factory)
    events: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.put_nowait((kind, payload))

    driver = module.ClaudeDriver(
        Instance(
            "claude",
            "claude",
            "Claude",
            sys.executable,
            cwd=str(tmp_path),
            env={"CLAUDE_CONFIG_DIR": str(tmp_path / "native-profile")},
        ),
        emit,
    )
    await driver.start()
    try:
        with pytest.raises(DriverError, match="explicitly authorize resume"):
            await driver.execute(
                {
                    "id": "stored",
                    "action": "prompt",
                    "session_id": "external",
                    "arguments": {"prompt": "do not start"},
                }
            )
        started = await asyncio.wait_for(
            driver.execute(
                {
                    "id": "original",
                    "action": "prompt",
                    "arguments": {"prompt": "native work"},
                }
            ),
            3,
        )
        session_id, execution_id = started["session_id"], started["execution_id"]
        assert started["admitted"] is True
        options = configurations[0]
        assert options.system_prompt == {"type": "preset", "preset": "claude_code"}
        assert options.tools == {"type": "preset", "preset": "claude_code"}
        assert options.setting_sources == ["user", "project", "local"]
        assert options.cli_path == sys.executable
        assert options.fork_session is False and options.max_turns is None
        built = SubprocessCLITransport(prompt="", options=options)._build_command()
        assert built[0] == sys.executable
        assert "--setting-sources=user,project,local" in built
        assert "--dangerously-skip-permissions" not in built
        with pytest.raises(DriverError, match="active"):
            await driver.execute(
                {
                    "id": "duplicate",
                    "action": "prompt",
                    "session_id": session_id,
                    "arguments": {"prompt": "duplicate"},
                }
            )
        transport = transports[0]
        await transport.incoming.put(
            {
                "type": "control_request",
                "request_id": "native-ask",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "AskUserQuestion",
                    "tool_use_id": "original-question",
                    "input": {"questions": [{"question": "Proceed?"}]},
                },
            }
        )
        kind, question = await asyncio.wait_for(events.get(), 3)
        assert kind == "question" and question["operation_id"] == "original"
        assert question["payload"]["native_request_id"] == "original-question"
        await driver.execute(
            {
                "id": "reply",
                "action": "respond",
                "session_id": session_id,
                "execution_id": execution_id,
                "arguments": {
                    "permission_id": "original-question",
                    "response": {"behavior": "allow", "answers": {"Proceed?": "yes"}},
                },
            }
        )
        await asyncio.wait_for(transport.response.wait(), 3)
        response = next(w for w in transport.writes if w["type"] == "control_response")
        assert response["response"]["response"]["updatedInput"]["answers"] == {"Proceed?": "yes"}
        await transport.incoming.put(
            {
                "type": "result",
                "subtype": "success",
                "duration_ms": 1,
                "duration_api_ms": 1,
                "is_error": False,
                "num_turns": 1,
                "session_id": session_id,
                "result": "done",
                "usage": {"input_tokens": 10, "cache_read_input_tokens": 8},
            }
        )
        kind, completed = await asyncio.wait_for(events.get(), 3)
        assert kind == "completed" and completed["execution_id"] == execution_id
        assert completed["payload"]["usage"]["cache_read_input_tokens"] == 8
        await driver.execute(
            {"id": "watch", "action": "watch", "session_id": session_id, "arguments": {}}
        )
        await driver.execute(
            {
                "id": "detach",
                "action": "detach",
                "session_id": session_id,
                "arguments": {"operation_id": "watch"},
            }
        )
        assert not transport.closed
        with pytest.raises(DriverError, match="precise native"):
            await driver.execute({"id": "steer", "action": "steer", "arguments": {}})
    finally:
        await asyncio.wait_for(driver.stop(), 3)
    assert transports[0].closed


@pytest.mark.asyncio
async def test_profiles_readonly_discovery_are_process_isolated(tmp_path: Path) -> None:
    async def emit(kind: str, payload: dict[str, Any]) -> None:
        pass

    drivers = [
        module.ClaudeDriver(
            Instance(
                str(index),
                "claude",
                "Claude",
                sys.executable,
                cwd=str(tmp_path),
                env={"CLAUDE_CONFIG_DIR": str(tmp_path / f"profile-{index}")},
            ),
            emit,
        )
        for index in range(2)
    ]
    results = await asyncio.gather(
        *(driver.execute({"id": "query", "action": "list", "arguments": {}}) for driver in drivers)
    )
    assert results == [{"sessions": [], "next_cursor": None}, {"sessions": [], "next_cursor": None}]
    assert all(not driver.sessions for driver in drivers)


@pytest.mark.asyncio
async def test_server_revocation_does_not_exit_sdk_or_kill_its_original_native_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = NativeTransport()
    monkeypatch.setattr(
        module,
        "ClaudeSDKClient",
        lambda *, options: ClaudeSDKClient(
            options=options,
            transport=transport,
        ),
    )
    journal = Journal(tmp_path / "private" / "journal.db", namespace="server/device")
    connector = Connector(Config("ws://localhost/", "device", "token"), journal)

    async def emit(kind: str, value: dict[str, Any]) -> None:
        journal.append(
            {"type": "event", "event_id": kind, "kind": kind, "instance_id": "claude", **value}
        )

    driver = module.ClaudeDriver(
        Instance("claude", "claude", "Claude", sys.executable, cwd=str(tmp_path)), emit
    )
    await driver.start()
    started = await driver.execute(
        {"id": "original", "action": "prompt", "arguments": {"prompt": "native work"}}
    )
    connector.drivers["claude"] = driver
    held = asyncio.Event()

    async def no_restart() -> None:
        pass

    original_hold = connector.authorization_lost

    async def hold() -> None:
        held.set()
        await original_hold()

    monkeypatch.setattr(connector, "start_drivers", no_restart)
    monkeypatch.setattr(connector, "authorization_lost", hold)

    async def server(socket: Any) -> None:
        await socket.recv()
        await socket.close(1008, "revoked")

    async with serve(server, "127.0.0.1", 0) as listener:
        connector.config = Config(
            f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}", "device", "token"
        )
        task = asyncio.create_task(connector.run())
        await asyncio.wait_for(held.wait(), 3)
        assert not transport.closed and not task.done()
        assert driver.sessions[started["session_id"]].active
        await transport.incoming.put(
            {
                "type": "result",
                "subtype": "success",
                "duration_ms": 1,
                "duration_api_ms": 1,
                "is_error": False,
                "num_turns": 1,
                "session_id": started["session_id"],
                "result": "completed locally",
            }
        )
        # Querying the native control transport gives the stream reader a real
        # barrier; the native result remains locally durable after revocation.
        await driver.sessions[started["session_id"]].client.interrupt()
        assert not transport.closed
        connector.stopped.set()
        await asyncio.wait_for(task, 3)
    assert transport.closed
    assert journal.pending()[0]["payload"]["text"] == "completed locally"
    journal.close()
