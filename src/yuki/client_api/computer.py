"""Paired-client computer reads, tickets, Manager observations and RFB proxy."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select
from starlette.websockets import WebSocketState

from yuki.client_api.computer_commands import ComputerCommands, public_control, release_key
from yuki.client_api.db_models import ClientDeviceModel
from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal
from yuki.client_api.rfb import RfbInputFilter, RfbProtocolError
from yuki.identity.db_models import CanonicalPersonModel
from yuki.identity.person_identity import current_person_id
from yuki.runtime.work_schema_v1 import work
from yuki.sandbox.client import SandboxClient
from yuki.sandbox.protocol import read_protocol_line


class ClientComputer:
    def __init__(self, identity: ClientIdentityService, sandbox: SandboxClient) -> None:
        self.identity, self.sandbox = identity, sandbox
        self.commands = ComputerCommands(identity, sandbox)
        self.snapshot: dict[str, Any] | None = None
        self.connected = False
        self.revision = 0
        self._tickets: dict[str, tuple[float, ClientPrincipal]] = {}
        self._online: dict[str, int] = {}
        self._streams: dict[WebSocket, ClientPrincipal] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._input_updates: dict[WebSocket, Callable[[], Awaitable[None]]] = {}
        self._authority_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()

    def device_connection(self, device_id: str, delta: int) -> None:
        previous = self._online.get(device_id, 0)
        current = previous + delta
        if current:
            self._online[device_id] = current
        else:
            self._online.pop(device_id, None)
        if bool(previous) != bool(current):
            self.revision += 1
            self.identity.database.commit_hints.publish()

    def control_view(self, control: dict[str, Any]) -> dict[str, Any]:
        result = public_control(control)
        if control.get("holder") == "human":
            result["device_online"] = bool(self._online.get(control["device_id"]))
        return result

    async def authenticate(self, principal: ClientPrincipal) -> ClientPrincipal:
        async with self.identity.database.read_session() as reader:
            return await self.identity.require_principal(reader, principal)

    async def rpc(self, method: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.sandbox.execute(method, args or {}, request_id=str(uuid4()))

    def help_for(self, work_id: str) -> bool:
        return any(
            item["kind"] == "help" and item.get("work_id") == work_id
            for item in (self.snapshot or {}).get("help_requests", [])
        )

    def run_kind(self, run_id: str, work_id: str) -> str | None:
        return next(
            (
                "help_request" if item["kind"] == "help" else "wait_control"
                for item in (self.snapshot or {}).get("help_requests", [])
                if item["run_id"] == run_id and item.get("work_id") == work_id
            ),
            None,
        )

    async def environment(self, principal: ClientPrincipal) -> dict[str, Any]:
        status, display, snapshot = await asyncio.gather(
            self.rpc("environment_status"), self.rpc("display_status"), self.rpc("control_get")
        )
        async with self.identity.database.read_session() as reader:
            principal = await self.identity.require_principal(reader, principal)
        available = "error" not in status and type(status.get("ready")) is bool
        known_control = snapshot.get("control") if "error" not in snapshot else None
        return {
            "state": ("running" if status["ready"] else "stopped") if available else "unavailable",
            "display": display.get("display", "unavailable"),
            "generation": status.get("generation"),
            "control": self.control_view(known_control)
            if isinstance(known_control, dict)
            else None,
            "help_requested": any(
                item["kind"] == "help" for item in snapshot.get("help_requests", [])
            )
            if "error" not in snapshot
            else None,
        }

    async def get(self, principal: ClientPrincipal) -> dict[str, Any]:
        principal = await self.authenticate(principal)
        await self.start()
        status, display, snapshot, browser = await asyncio.gather(
            self.rpc("environment_status"),
            self.rpc("display_status"),
            self.rpc("control_get"),
            self.rpc("tabs_list"),
        )
        tabs = browser.get("tabs", [])
        work_ids = {tab["work_id"] for tab in tabs if tab.get("work_id")}
        async with self.identity.database.read_session() as reader:
            states: dict[str, str] = (
                {
                    row.id: row.state
                    for row in (
                        await reader.execute(
                            select(work.c.id, work.c.state).where(work.c.id.in_(work_ids))
                        )
                    ).all()
                }
                if work_ids
                else {}
            )
        await self.authenticate(principal)
        return {
            "environment": ("running" if status["ready"] else "stopped")
            if "error" not in status
            else "unavailable",
            "display": display.get("display", "unavailable"),
            "control": self.control_view(snapshot["control"]) if "error" not in snapshot else None,
            "help_requests": [
                {key: item[key] for key in ("wait_id", "kind", "work_id", "note", "since")}
                for item in snapshot.get("help_requests", [])
            ],
            "tabs": [
                {
                    "target_id": tab["target_id"],
                    "title": tab.get("title", ""),
                    "url_host": urlsplit(tab.get("url", "")).hostname or "",
                    "work_id": tab.get("work_id") if tab.get("work_id") in states else None,
                    "work_state": states.get(tab.get("work_id")),
                    "ownership": "work" if tab.get("work_id") in states else "unknown",
                }
                for tab in tabs
            ],
            "tabs_available": browser.get("available", "error" not in browser),
            "resources": {key: status["limits"][key] for key in ("memory_mib", "cpus")}
            if status.get("limits")
            else None,
            "revision": self.revision,
        }

    async def command(
        self, principal: ClientPrincipal, request_id: str, action: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        principal = await self.authenticate(principal)
        await self.start()

        async def confirm_sleep() -> None:
            current = await self.get(principal)
            active = [
                tab
                for tab in current["tabs"]
                if tab["ownership"] == "work"
                and tab["work_state"] not in {"completed", "failed", "cancelled"}
            ]
            if active or (current["control"] or {}).get("holder") == "agent":
                raise ClientError(
                    "precondition_failed",
                    details={
                        "tabs": active,
                        "unknown_tabs": sum(
                            tab["ownership"] == "unknown" for tab in current["tabs"]
                        ),
                        "control": current["control"],
                    },
                )

        return await self.commands.execute(
            principal,
            request_id,
            action,
            payload,
            before_effect=confirm_sleep
            if action == "sleep" and not payload.get("confirm_active_work_tabs")
            else None,
        )

    async def ticket(self, principal: ClientPrincipal, root_path: str = "") -> dict[str, Any]:
        principal = await self.authenticate(principal)
        await self.start()
        now = time.monotonic()
        self._tickets = {key: value for key, value in self._tickets.items() if value[0] > now}
        ticket = secrets.token_urlsafe(32)
        self._tickets[ticket] = (now + 60, principal)
        return {
            "ticket": ticket,
            "expires_at": (datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
            "ws_path": root_path.rstrip("/") + "/client/v1/computer/display",
        }

    async def files(
        self, principal: ClientPrincipal, path: str, after: str | None, limit: int
    ) -> dict[str, Any]:
        await self.authenticate(principal)
        result = await self.rpc("files_list", {"path": path, "after": after or "", "limit": limit})
        if result.get("error"):
            raise ClientError("not_found")
        await self.authenticate(principal)
        return result

    async def content(
        self, principal: ClientPrincipal, path: str
    ) -> tuple[dict[str, Any], AsyncIterator[bytes], Callable[[], Awaitable[None]]]:
        await self.authenticate(principal)
        stack = AsyncExitStack()
        try:
            metadata, reader, _ = await stack.enter_async_context(
                self.sandbox.stream("files_read", {"path": path})
            )
        except (OSError, ValueError):
            raise ClientError("not_found") from None
        try:
            await self.authenticate(principal)
        except BaseException:
            await stack.aclose()
            raise

        async def body() -> AsyncIterator[bytes]:
            try:
                while data := await reader.read(65536):
                    await self.authenticate(principal)
                    yield data
            finally:
                await stack.aclose()

        return metadata, body(), stack.aclose

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if any(task.done() for task in self._tasks):
                for task in self._tasks:
                    if task.done():
                        task.result()
                await self._close_locked()
            if not self._tasks:
                self._tasks = [
                    asyncio.create_task(self._watch()),
                    asyncio.create_task(self._authority_watch()),
                ]

    async def close(self) -> None:
        async with self._lifecycle_lock:
            await self._close_locked()

    async def _close_locked(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.connected = False
        await self._close_streams()

    async def _close_streams(self) -> None:
        for socket in tuple(self._streams):
            if socket.application_state != WebSocketState.DISCONNECTED:
                await socket.close(code=1012)

    async def _watch(self) -> None:
        while True:
            try:
                async with self.sandbox.stream("control_watch", {}) as (snapshot, reader, _):
                    while True:
                        if not isinstance(snapshot, dict) or not isinstance(
                            snapshot.get("control"), dict
                        ):
                            raise ValueError("invalid_control_snapshot")
                        self.snapshot, self.connected = snapshot, True
                        self.revision += 1
                        await self._authority_changed()
                        self.identity.database.commit_hints.publish()
                        snapshot = json.loads(await read_protocol_line(reader))
            except NotImplementedError:
                return
            except (OSError, ValueError, TimeoutError) as exc:
                if self.connected:
                    self.revision += 1
                    self.connected, self.snapshot = False, None
                    await self._close_streams()
                    self.identity.database.commit_hints.publish()
                if isinstance(exc, ValueError) and str(exc) == "unknown_method":
                    return
                await asyncio.sleep(1)

    async def _authority_watch(self) -> None:
        queue = self.identity.database.commit_hints.subscribe()
        try:
            await self._authority_changed()
            while await queue.get():
                await self._authority_changed()
            await self._close_streams()
            await self._authority_changed()
        finally:
            self.identity.database.commit_hints.unsubscribe(queue)

    async def _authority_changed(self) -> None:
        async with self._authority_lock:
            for socket, principal in tuple(self._streams.items()):
                try:
                    await self.authenticate(principal)
                except ClientError:
                    if socket.application_state != WebSocketState.DISCONNECTED:
                        await socket.close(code=1008)
                else:
                    update = self._input_updates.get(socket)
                    if update:
                        try:
                            await update()
                        except (OSError, RfbProtocolError):
                            if socket.application_state != WebSocketState.DISCONNECTED:
                                await socket.close(code=1012)
            control = (self.snapshot or {}).get("control", {})
            if control.get("holder") != "human":
                return
            async with self.identity.database.read_session() as reader:
                person = await reader.get(CanonicalPersonModel, control["person_id"])
                device = await reader.get(ClientDeviceModel, control["device_id"])
                root = (
                    await reader.get(
                        CanonicalPersonModel, await current_person_id(reader, person.id)
                    )
                    if person
                    else None
                )
                reason = (
                    "person_forgotten"
                    if person is None
                    else "person_disabled"
                    if not person.enabled or root is None or not root.enabled
                    else "device_revoked"
                    if device is None or device.revoked_at is not None
                    else None
                )
            if reason:
                key = release_key(control["lease_id"])
                saved = await self.sandbox.execute(
                    "control_request_get", {"key": key}, request_id=key
                )
                if saved.get("error") != "unknown_request":
                    return
                await self.sandbox.execute(
                    "control_set",
                    {
                        "action": "return",
                        "lease_id": control["lease_id"],
                        "actor": {"system": True},
                        "reason": reason,
                    },
                    request_id=key,
                )

    async def display(self, socket: WebSocket, ticket: str) -> None:
        if "binary" not in socket.headers.get("sec-websocket-protocol", "").replace(" ", "").split(
            ","
        ):
            await socket.close(code=1008)
            return
        saved = self._tickets.pop(ticket, None)
        if saved is None or saved[0] <= time.monotonic() or not self.connected:
            await socket.close(code=1008)
            return
        try:
            principal = await self.authenticate(saved[1])
        except ClientError:
            await socket.close(code=1008)
            return
        await socket.accept(subprotocol="binary")
        self._streams[socket] = principal
        self.device_connection(principal.device_id, 1)
        parser, lock = RfbInputFilter(), asyncio.Lock()
        tasks: list[asyncio.Task[None]] = []
        try:
            async with self.sandbox.stream("display_stream", {}) as (_, reader, writer):

                async def input_update() -> None:
                    control = (self.snapshot or {}).get("control", {})
                    async with lock:
                        writer.write(
                            parser.set_holder(
                                self.connected
                                and control.get("holder") == "human"
                                and control.get("device_id") == principal.device_id
                            )
                        )
                        await writer.drain()

                self._input_updates[socket] = input_update
                await input_update()

                async def send() -> None:
                    while True:
                        data = await socket.receive_bytes()
                        await self.authenticate(principal)
                        await input_update()
                        async with lock:
                            writer.write(parser.feed(data))
                            await writer.drain()

                async def receive() -> None:
                    while data := await reader.read(65536):
                        await self.authenticate(principal)
                        await socket.send_bytes(data)

                tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    async with lock:
                        writer.write(parser.close())
                        await writer.drain()
        except (OSError, ValueError, ClientError, WebSocketDisconnect, RfbProtocolError):
            pass
        finally:
            self._streams.pop(socket, None)
            self.device_connection(principal.device_id, -1)
            self._input_updates.pop(socket, None)
            if socket.application_state != WebSocketState.DISCONNECTED:
                await socket.close()
