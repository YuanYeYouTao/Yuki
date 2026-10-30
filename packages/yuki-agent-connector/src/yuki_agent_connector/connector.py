"""Outbound device transport; the native owners outlive network connections."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from . import PROTOCOL_VERSION
from .computer import computer_status
from .config import Config
from .drivers import Driver, DriverError, make_driver
from .files import FileTransfers
from .journal import Journal
from .terminal import LocalTerminal

logger = logging.getLogger(__name__)
READ_ACTIONS = {"list", "get", "read", "status", "stat", "release"}


class Connector:
    def __init__(
        self, config: Config, journal: Journal, *, config_path: Path | None = None
    ) -> None:
        self.config, self.journal = config, journal
        self.config_path = config_path
        self.drivers: dict[str, Driver] = {}
        self.errors: dict[str, str] = {}
        self.stream: ClientConnection | None = None
        self.pending_events = asyncio.Event()
        self.tasks: set[asyncio.Task[None]] = set()
        self.stopped = asyncio.Event()
        self.catalog_changed = asyncio.Event()
        self._startup: list[asyncio.Task[None]] = []
        self.files = FileTransfers(journal)
        self.terminal = LocalTerminal(
            journal.path.parent / "terminal", journal, self._terminal_event
        )

    async def _terminal_event(self, kind: str, value: dict[str, Any]) -> None:
        self.journal.append({"event_id": str(uuid4()), "kind": kind, **value})
        self.pending_events.set()

    def refresh_access(self) -> None:
        if self.config_path is None:
            return
        configured = Config.load(self.config_path)
        if configured.device_id != self.config.device_id:
            raise ValueError("The active service belongs to its original device")
        if configured.full_access != self.config.full_access:
            self.config = replace(self.config, full_access=configured.full_access)
            self.catalog_changed.set()

    def device_capabilities(self) -> dict[str, Any]:
        computer = computer_status()
        return {
            "full_access": self.config.full_access is True,
            "platform": computer["platform"],
            "shell": computer["shell"],
            "tty": False,
            "interrupt_supported": computer["interrupt_supported"],
        }

    async def _device_command(self, command: dict[str, Any]) -> dict[str, Any]:
        resource, action = command["resource"], command["action"]
        args = command.get("arguments", {})
        if resource == "terminal" and action == "status":
            return {"full_access": self.config.full_access is True, **computer_status()}
        if action == "get" and resource == "terminal":
            return self.journal.operation(args["operation_id"])
        allowed_closed = (
            resource == "terminal" and action in {"read", "interrupt", "cancel", "close"}
        ) or (resource == "file" and action in {"release", "get"})
        if self.config.full_access is not True and not allowed_closed:
            raise DriverError("full_access_disabled", "Enable full access on this computer first")
        if resource == "terminal":
            return await self.terminal.execute(command)
        if resource == "file":
            return await self.files.execute(command)
        raise DriverError("invalid_resource", f"Unsupported device resource: {resource}")

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)

        def finished(task: asyncio.Task[None]) -> None:
            self.tasks.discard(task)
            if not task.cancelled() and (error := task.exception()) is not None:
                logger.error("Connector operation failed: %s", type(error).__name__)

        task.add_done_callback(finished)

    async def _recover(self, instance_id: str) -> None:
        driver = self.drivers[instance_id]
        for command, _ in self.journal.observations(instance_id):
            current = self.journal.observation(command["id"])
            if current is not None:
                await driver.recover(*current)

    async def start_drivers(self) -> None:
        async def start(spec: Any) -> None:
            async def emit(kind: str, value: dict[str, Any]) -> None:
                if kind == "connection":
                    if value["status"] == "available":
                        self.errors.pop(spec.id, None)
                        self._spawn(self._recover(spec.id))
                    else:
                        self.errors[spec.id] = value.get(
                            "reason", value.get("error_type", "native_disconnected")
                        )
                    self.catalog_changed.set()
                    return
                event = {
                    "type": "event",
                    "event_id": str(uuid4()),
                    "instance_id": spec.id,
                    "kind": kind,
                    **value,
                }
                self.journal.append(event)
                self.pending_events.set()

            driver: Driver | None = None
            try:
                driver = make_driver(spec, emit)
                self.drivers[spec.id] = driver
                self.errors[spec.id] = "starting"
                await driver.start()
                self.errors.pop(spec.id, None)
                self.catalog_changed.set()
                await self._recover(spec.id)
            except asyncio.CancelledError:
                if driver:
                    await driver.stop()
                    self.drivers.pop(spec.id, None)
                raise
            except Exception as exc:
                self.errors[spec.id] = type(exc).__name__
                self.catalog_changed.set()
                logger.error("Native instance unavailable: %s (%s)", spec.id, type(exc).__name__)
                if driver:
                    await driver.stop()
                    self.drivers.pop(spec.id, None)

        self._startup.extend(
            asyncio.create_task(start(spec), name=f"native-start-{spec.id}")
            for spec in self.config.instances
        )
        # Each native owner starts/reconnects independently of its siblings.
        await asyncio.sleep(0)

    def hello(self) -> dict[str, Any]:
        return {
            "type": "hello",
            "protocol_version": PROTOCOL_VERSION,
            "device_id": self.config.device_id,
            "device_capabilities": self.device_capabilities(),
            "instances": [
                {
                    "id": spec.id,
                    "label": spec.label,
                    "driver": spec.driver,
                    "capabilities": self.drivers[spec.id].capabilities
                    if spec.id in self.drivers and spec.id not in self.errors
                    else [],
                    "status": "available"
                    if spec.id in self.drivers and spec.id not in self.errors
                    else "unavailable",
                    **({"error_type": self.errors[spec.id]} if spec.id in self.errors else {}),
                }
                for spec in self.config.instances
            ],
        }

    async def dispatch(self, command: dict[str, Any]) -> dict[str, Any]:
        key = command["id"]
        mutation = command["action"] not in READ_ACTIONS
        recorded = False
        try:
            if command.get("resource"):
                self.refresh_access()
            if mutation:
                previous = self.journal.begin(command)
                if previous is not None:
                    return previous
                recorded = True
            arguments = command.get("arguments", {})
            if command.get("resource"):
                result = await self._device_command(command)
            elif command["action"] == "get" and arguments.get("operation_id"):
                result = self.journal.operation(arguments["operation_id"])
                if result.get("status") == "unknown":
                    observation = self.journal.observation(arguments["operation_id"])
                    driver = self.drivers.get(command["instance_id"])
                    if observation and driver and command["instance_id"] not in self.errors:
                        original, receipt = observation
                        await driver.recover(original, receipt)
                        result = self.journal.operation(arguments["operation_id"])
            elif command["action"] == "detach":
                driver = self.drivers.get(command["instance_id"])
                result = (
                    await driver.execute(command)
                    if driver is not None
                    else {
                        "admitted": True,
                        "detached": True,
                        "pending": False,
                        "status": "detached",
                    }
                )
            else:
                driver = self.drivers.get(command["instance_id"])
                if driver is None or command["instance_id"] in self.errors:
                    raise DriverError(
                        "instance_unavailable", "The configured native instance is unavailable"
                    )
                result = await driver.execute(command)
            response = {"type": "response", "id": key, "result": result}
        except DriverError as exc:
            response = {
                "type": "response",
                "id": key,
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "details": exc.details,
                    "admitted": False,
                    "executed": False,
                },
            }
            if exc.uncertain:
                response = {
                    "type": "response",
                    "id": key,
                    "result": {
                        "status": "unknown",
                        "reason": exc.code,
                        "details": exc.details,
                    },
                }
        except (KeyError, ValueError, TypeError) as exc:
            response = {
                "type": "response",
                "id": key,
                "error": {
                    "code": "invalid_command",
                    "message": str(exc),
                    "admitted": False,
                    "executed": False,
                },
            }
            if recorded:
                response = {
                    "type": "response",
                    "id": key,
                    "result": {
                        "status": "unknown",
                        "reason": "native_result_not_confirmed",
                        "error_type": type(exc).__name__,
                    },
                }
        except Exception as exc:
            # An exception after native dispatch cannot prove failure/no effect.
            logger.error("Native operation uncertain: %s (%s)", key, type(exc).__name__)
            response = {
                "type": "response",
                "id": key,
                "result": {
                    "status": "unknown",
                    "reason": "native_result_not_confirmed",
                    "error_type": type(exc).__name__,
                },
            }
        if recorded:
            self.journal.complete(key, response)
            response = {"type": "response", "id": key, "result": self.journal.operation(key)}
        return response

    async def _command(self, command: dict[str, Any], websocket: ClientConnection) -> None:
        try:
            response = await self.dispatch(command)
            await websocket.send(json.dumps(response, ensure_ascii=False))
        except ConnectionClosed:
            # The original dispatch remains independent of its transport.
            pass
        except Exception:
            # A missing response cannot leave the peer awaiting a live RPC forever.
            await websocket.close(code=1011, reason="Command response unavailable")
            raise

    async def _events(self, websocket: ClientConnection) -> None:
        after = 0
        while True:
            self.pending_events.clear()
            original = self.journal.next_event(after)
            if original is None:
                await self.pending_events.wait()
                continue
            sequence, _, encoded = original
            # WebSocket write backpressure controls transport. Commit ACK only
            # prunes durable originals; it isn't a one-event RTT credit gate.
            await websocket.send(encoded)
            after = sequence
            # send() may complete without yielding when the socket is writable.
            await asyncio.sleep(0)

    async def _catalog(self, websocket: ClientConnection) -> None:
        while True:
            await self.catalog_changed.wait()
            self.catalog_changed.clear()
            await websocket.send(
                json.dumps(
                    {
                        "type": "catalog",
                        "instances": self.hello()["instances"],
                        "device_capabilities": self.device_capabilities(),
                    },
                    ensure_ascii=False,
                )
            )

    async def _read(self, websocket: ClientConnection) -> None:
        async for raw in websocket:
            value = json.loads(raw)
            if value.get("type") == "ack":
                self.journal.ack(value["event_id"])
            elif value.get("type") == "command":
                self._spawn(self._command(value, websocket))
            elif value.get("type") == "revoked":
                raise DriverError("credential_revoked", "Connector authorization was revoked")

    async def connection(self, websocket: ClientConnection) -> None:
        self.stream = websocket
        await websocket.send(json.dumps(self.hello(), ensure_ascii=False))
        owners = [
            asyncio.create_task(owner(websocket))
            for owner in (self._read, self._events, self._catalog)
        ]
        try:
            done, _ = await asyncio.wait(owners, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self.stream = None
            for task in owners:
                task.cancel()
            await asyncio.gather(*owners, return_exceptions=True)

    async def run(self) -> None:
        try:
            await self.start_drivers()
            async for websocket in connect(
                self.config.url,
                additional_headers={"Authorization": f"Bearer {self.config.token}"},
                max_size=None,
            ):
                try:
                    await self.connection(websocket)
                except ConnectionClosed as exc:
                    if (
                        exc.rcvd
                        and exc.rcvd.code == 1008
                        and exc.rcvd.reason == "unsupported_connector_protocol"
                    ):
                        logger.error(
                            "unsupported_connector_protocol: Connector and Yuki protocol versions "
                            "are incompatible; update the older installation"
                        )
                        await self.stopped.wait()
                        break
                    if exc.rcvd and exc.rcvd.code in {1008, 4001, 4003, 4401, 4403}:
                        await self.authorization_lost()
                        break
                    logger.info("Server disconnected; native executions continue")
                except DriverError as exc:
                    if exc.code != "credential_revoked":
                        raise
                    await self.authorization_lost()
                    break
                if self.stopped.is_set():
                    break
        except InvalidStatus as exc:
            if exc.response.status_code not in {401, 403}:
                raise
            await self.authorization_lost()
        finally:
            # Application/service shutdown is distinct from a socket reconnect.
            # Codex closes only its client/proxy; SDK/ACP processes are our own.
            original = sys.exception()

            async def close_owners() -> None:
                await self.terminal.close()
                await self.files.close()
                owners = (*self._startup, *self.tasks)
                for task in owners:
                    task.cancel()
                await asyncio.gather(*owners, return_exceptions=True)
                await asyncio.gather(*(driver.stop() for driver in self.drivers.values()))

            joined = asyncio.create_task(close_owners())
            cancelled: asyncio.CancelledError | None = None
            while not joined.done():
                try:
                    await asyncio.shield(joined)
                except asyncio.CancelledError as exc:
                    cancelled = cancelled or exc
            joined.result()
            if cancelled is not None and not isinstance(original, asyncio.CancelledError):
                raise cancelled

    async def authorization_lost(self) -> None:
        logger.error(
            "Server authorization ended; native owners remain alive until local service stop"
        )
        await self.stopped.wait()


@contextlib.contextmanager
def installation_lock(path: Path) -> Any:
    """One owner per connector installation; not an execution/session scheduler."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            stream.seek(1)
            stream.truncate()
            stream.write(str(os.getpid()).encode("ascii"))
            stream.flush()
            yield
        finally:
            stream.seek(1)
            stream.truncate()
            stream.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
