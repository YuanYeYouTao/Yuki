"""A scripted server end of the Connector WebSocket and a Desktop-like owner of the process."""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from test_desktop_stdio_lifecycle import runtime
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

ARTIFACTS = Path(__file__).resolve().parents[3] / "test-results" / "device-computer"
FAKE = Path(__file__).resolve().parent / "fakes" / "screen_connector.py"
# Bounds every wait for the process; generous for cold CI runners, not a success gate.
WAIT = 30.0


async def until(check: Callable[[], Any]) -> Any:
    deadline = time.monotonic() + WAIT
    while not (value := check()):
        if time.monotonic() > deadline:
            raise TimeoutError("condition not reached")
        await asyncio.sleep(0.01)
    return value


class Hellos:
    def __init__(self, cloud: Cloud) -> None:
        self.cloud = cloud

    async def wait_for(self, count: int) -> None:
        await until(lambda: len(self.cloud.hello) >= count)


class Cloud:
    """The server side: records hello/catalog, acknowledges events, answers commands."""

    def __init__(self) -> None:
        self.socket: ServerConnection | None = None
        self.hello: list[dict[str, Any]] = []
        self.catalogs: list[dict[str, Any]] = []
        self.waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.connected = Hellos(self)

    async def handler(self, socket: ServerConnection) -> None:
        try:
            async for raw in socket:
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "hello":
                    self.socket = socket
                    self.hello.append(message)
                elif kind == "catalog":
                    self.catalogs.append(message)
                elif kind == "response":
                    future = self.waiting.pop(message["id"], None)
                    if future is not None and not future.done():
                        future.set_result(message)
                elif "event_id" in message:
                    await socket.send(json.dumps({"type": "ack", "event_id": message["event_id"]}))
        except ConnectionClosed:
            pass

    async def command(
        self, resource: str, action: str, identity: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        assert self.socket is not None
        future = asyncio.get_running_loop().create_future()
        self.waiting[identity] = future
        await self.socket.send(
            json.dumps(
                {
                    "type": "command",
                    "resource": resource,
                    "action": action,
                    "id": identity,
                    "arguments": arguments,
                },
                ensure_ascii=False,
            )
        )
        response = await asyncio.wait_for(future, 60)
        return {key: response[key] for key in ("result", "error") if key in response}

    async def image(self, path: str, identity: str) -> bytes:
        """The server's download loop: stat, read every chunk, release."""
        metadata = (await self.command("file", "stat", f"{identity}:stat", {"path": path}))[
            "result"
        ]
        data = b""
        while True:
            part = (
                await self.command(
                    "file",
                    "read",
                    f"{identity}:read:{len(data)}",
                    {
                        "transfer_id": metadata["transfer_id"],
                        "version": metadata["version"],
                        "offset": len(data),
                    },
                )
            )["result"]
            data += base64.b64decode(part["data_base64"])
            if part["eof"]:
                break
        await self.command(
            "file", "release", f"{identity}:release", {"transfer_id": metadata["transfer_id"]}
        )
        return data

    async def catalog_where(self, check: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        return await until(  # type: ignore[no-any-return]
            lambda: next((value for value in self.catalogs if check(value)), None)
        )


class ConnectorProcess:
    """The actual CLI behind the fake screen, owned the way Desktop owns it."""

    def __init__(self, state: Path, data: Path, *, desktop: bool = True) -> None:
        self.state, self.data, self.desktop = state, data, desktop
        self.lines: list[dict[str, Any]] = []
        self.raw: list[str] = []
        self.mark = 0

    async def start(self) -> None:
        executable, environment = runtime()
        self.process = await asyncio.create_subprocess_exec(
            executable,
            "-B",
            str(FAKE),
            str(self.state),
            "--data-dir",
            str(self.data),
            "run",
            *(["--desktop-stdin"] if self.desktop else []),
            env=environment,
            stdin=asyncio.subprocess.PIPE if self.desktop else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        self.reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self.process.stdout is not None
        while raw := await self.process.stdout.readline():
            text = raw.decode()
            self.raw.append(text)
            self.lines.append(json.loads(text))

    def send(self, value: dict[str, Any] | bytes) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(
            value if isinstance(value, bytes) else (json.dumps(value) + "\n").encode()
        )

    async def wait_for(self, check: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        def found() -> tuple[int, dict[str, Any]] | None:
            for index in range(self.mark, len(self.lines)):
                if check(self.lines[index]):
                    return index, self.lines[index]
            return None

        index, line = await until(found)
        self.mark = index + 1
        return line

    async def stop(self) -> tuple[int, str]:
        assert self.process.stdin is not None and self.process.stderr is not None
        self.process.stdin.close()
        await asyncio.wait_for(self.process.wait(), 20)
        await self.reader
        return self.process.returncode or 0, (await self.process.stderr.read()).decode()

    async def kill(self) -> None:
        self.process.kill()
        await self.process.wait()
        await self.reader
