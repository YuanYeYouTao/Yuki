"""Connection-owned RPCs. A missing reply never authorizes another dispatch."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol
from uuid import uuid4


class DeviceOffline(ConnectionError):
    pass


class JsonSocket(Protocol):
    async def send_json(self, data: Any) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


class DeviceConnection:
    def __init__(self, device_id: str, socket: JsonSocket) -> None:
        self.device_id = device_id
        self.id = str(uuid4())
        self.socket = socket
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.instances: set[str] = set()
        self._send_lock = asyncio.Lock()

    async def send(self, value: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.socket.send_json(value)

    async def request(self, command: dict[str, Any]) -> dict[str, Any]:
        identity = command["id"]
        existing = self.pending.get(identity)
        if existing is not None:
            return await asyncio.shield(existing)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        try:
            await self.send(command)
            return await asyncio.shield(future)
        finally:
            if self.pending.get(identity) is future:
                self.pending.pop(identity, None)
            if not future.done():
                future.cancel()

    def respond(self, message: dict[str, Any]) -> None:
        future = self.pending.get(message.get("id", ""))
        if future is not None and not future.done():
            result = message.get("result")
            if isinstance(result, dict):
                future.set_result(result)
            else:
                error = message.get("error")
                future.set_result({"error": error, "executed": False, "pending": False})

    def disconnected(self) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_exception(DeviceOffline("device_disconnected"))


class DeviceConnections:
    def __init__(self) -> None:
        self._devices: dict[str, DeviceConnection] = {}

    def get(self, device_id: str) -> DeviceConnection:
        connection = self._devices.get(device_id)
        if connection is None:
            raise DeviceOffline("device_offline")
        return connection

    def online(self, device_id: str) -> bool:
        return device_id in self._devices

    async def attach(self, connection: DeviceConnection) -> None:
        previous = self._devices.get(connection.device_id)
        self._devices[connection.device_id] = connection
        if previous is not None:
            previous.disconnected()
            await previous.socket.close(code=1000, reason="connection_replaced")

    def detach(self, connection: DeviceConnection) -> bool:
        current = self._devices.get(connection.device_id) is connection
        if current:
            self._devices.pop(connection.device_id)
        connection.disconnected()
        return current

    def active(self) -> tuple[DeviceConnection, ...]:
        return tuple(self._devices.values())

    async def close(self) -> None:
        connections = tuple(self._devices.values())
        self._devices.clear()
        for connection in connections:
            connection.disconnected()
        results = await asyncio.gather(
            *(connection.socket.close(code=1001) for connection in connections),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise BaseExceptionGroup("connector transport shutdown failed", errors)
