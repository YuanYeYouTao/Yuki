"""Outbound device transport; the native owners outlive network connections."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .config import Config
from .drivers import Driver, DriverError, make_driver
from .journal import Journal

logger = logging.getLogger(__name__)
READ_ACTIONS = {"list", "get"}


class Connector:
    def __init__(self, config: Config, journal: Journal) -> None:
        self.config, self.journal = config, journal
        self.drivers: dict[str, Driver] = {}
        self.errors: dict[str, str] = {}
        self.stream: ClientConnection | None = None
        self.pending_events = asyncio.Event()
        self.tasks: set[asyncio.Task[None]] = set()
        self.stopped = asyncio.Event()

    async def start_drivers(self) -> None:
        async def start(spec: Any) -> None:
            async def emit(kind: str, value: dict[str, Any]) -> None:
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
                await driver.start()
                self.drivers[spec.id] = driver
            except Exception as exc:
                self.errors[spec.id] = type(exc).__name__
                logger.error("Native instance unavailable: %s (%s)", spec.id, type(exc).__name__)
                if driver:
                    await driver.stop()

        await asyncio.gather(*(start(spec) for spec in self.config.instances))
        for command, result in self.journal.observations():
            driver = self.drivers.get(command["instance_id"])
            if driver:
                await driver.recover(command, result)

    def hello(self) -> dict[str, Any]:
        return {
            "type": "hello",
            "protocol_version": 1,
            "device_id": self.config.device_id,
            "instances": [
                {
                    "id": spec.id,
                    "label": spec.label,
                    "driver": spec.driver,
                    "capabilities": self.drivers[spec.id].capabilities
                    if spec.id in self.drivers
                    else [],
                    "status": "available" if spec.id in self.drivers else "unavailable",
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
            if mutation:
                previous = self.journal.begin(command)
                if previous is not None:
                    return previous
                recorded = True
            arguments = command.get("arguments", {})
            if command["action"] == "get" and arguments.get("operation_id"):
                result = self.journal.operation(arguments["operation_id"])
            else:
                driver = self.drivers.get(command["instance_id"])
                if driver is None:
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
        return response

    async def _command(self, command: dict[str, Any], websocket: ClientConnection) -> None:
        response = await self.dispatch(command)
        # If transport disappeared, the result remains in its original journal.
        with contextlib.suppress(ConnectionClosed):
            await websocket.send(json.dumps(response, ensure_ascii=False))

    async def _events(self, websocket: ClientConnection) -> None:
        sent: set[str] = set()
        while True:
            self.pending_events.clear()
            pending = self.journal.pending()
            sent.intersection_update(event["event_id"] for event in pending)
            for event in pending:
                if event["event_id"] not in sent:
                    await websocket.send(json.dumps(event, ensure_ascii=False))
                    sent.add(event["event_id"])
            await self.pending_events.wait()

    async def connection(self, websocket: ClientConnection) -> None:
        self.stream = websocket
        await websocket.send(json.dumps(self.hello(), ensure_ascii=False))
        events = asyncio.create_task(self._events(websocket))
        try:
            async for raw in websocket:
                value = json.loads(raw)
                if value.get("type") == "ack":
                    self.journal.ack(value["event_id"])
                    self.pending_events.set()
                elif value.get("type") == "command":
                    task = asyncio.create_task(self._command(value, websocket))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
                elif value.get("type") == "revoked":
                    raise DriverError("credential_revoked", "Connector authorization was revoked")
        finally:
            self.stream = None
            events.cancel()
            with contextlib.suppress(asyncio.CancelledError, ConnectionClosed):
                await events

    async def run(self) -> None:
        await self.start_drivers()
        try:
            async for websocket in connect(
                self.config.url,
                additional_headers={"Authorization": f"Bearer {self.config.token}"},
                max_size=None,
            ):
                try:
                    await self.connection(websocket)
                except ConnectionClosed as exc:
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
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            await asyncio.gather(*(driver.stop() for driver in self.drivers.values()))

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
