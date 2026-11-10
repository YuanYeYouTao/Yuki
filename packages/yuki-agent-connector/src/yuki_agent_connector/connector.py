"""Outbound device transport; the native owners outlive network connections."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from websockets.asyncio.client import ClientConnection, connect, process_exception
from websockets.exceptions import ConnectionClosed, InvalidStatus

from . import PROTOCOL_VERSION
from .computer import computer_status
from .config import Config, Instance
from .desk import Desk
from .drivers import Driver, DriverError, make_driver
from .files import FileTransfers
from .journal import Journal
from .screen import Backend
from .terminal import LocalTerminal

logger = logging.getLogger(__name__)
# Reads are simply answered again on retry; everything else is journaled before it runs.
READ_ACTIONS = {
    "list",
    "get",
    "read",
    "status",
    "stat",
    "release",
    "screenshot",
    "snapshot",
    "resume",
}
ORIGINAL_CONTROLS = {"get", "detach", "cancel", "steer", "respond", "queue", "watch"}


def instance_source(spec: Instance) -> str:
    """Hash the actual configured source and effective native profile, without secrets."""
    if spec.driver == "codex":
        if spec.endpoint:
            target = {"endpoint": spec.endpoint}
        elif spec.socket:
            explicit_socket = Path(spec.socket)
            if not explicit_socket.is_absolute():
                explicit_socket = Path(spec.cwd) / explicit_socket
            target = {"socket": str(explicit_socket.resolve())}
        else:
            environment = spec.environment()
            profile_value = environment.get("CODEX_HOME")
            if profile_value:
                profile = Path(profile_value)
                if not profile.is_absolute():
                    profile = Path(spec.cwd) / profile
            else:
                home_value = environment.get("USERPROFILE" if sys.platform == "win32" else "HOME")
                profile = Path(home_value or Path.home()) / ".codex"
            target = {"profile": str(profile.resolve())}
        encoded = json.dumps(
            {"driver": spec.driver, **target},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    environment = spec.environment()
    home = Path(
        environment.get("USERPROFILE" if sys.platform == "win32" else "HOME") or Path.home()
    )
    explicit = dict(spec.env)
    names = (
        ("CLAUDE_CONFIG_DIR",)
        if spec.driver == "claude"
        else ("CODEX_HOME", "CLAUDE_CONFIG_DIR")
        if spec.driver == "acp"
        else ()
    )
    profiles: dict[str, str] = {}
    for name in names:
        explicit.pop(name, None)
        profile_value = environment.get(name)
        if profile_value:
            path = Path(profile_value)
            profiles[name] = str((path if path.is_absolute() else Path(spec.cwd) / path).resolve())
        elif spec.driver == "claude":
            profiles[name] = str((home / ".claude").resolve())
    socket = Path(spec.socket) if spec.socket else None
    if socket is not None and not socket.is_absolute():
        socket = Path(spec.cwd) / socket
    source = {
        "driver": spec.driver,
        "installation": spec.installation,
        "command": None if spec.installation else spec.command,
        "args": spec.args,
        "cwd": str(Path(spec.cwd).resolve()),
        "environment": explicit,
        "profiles": profiles,
        "home": str(home.resolve()),
        "socket": str(socket.resolve()) if socket else None,
        "endpoint": spec.endpoint,
    }
    encoded = json.dumps(source, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class Connector:
    def __init__(
        self,
        config: Config,
        journal: Journal,
        *,
        config_path: Path | None = None,
        report: Callable[[dict[str, Any]], None] = lambda line: None,
        screen: Backend | None = None,
    ) -> None:
        self.config, self.journal = config, journal
        self.config_path = config_path
        # One line per connection state change; Desktop reads it from stdout.
        self.report = report
        self.state: str | None = None
        self.drivers: dict[str, Driver] = {}
        self.operations: dict[str, Driver] = {}
        self.dispatching: set[str] = set()
        self.detached_work: set[str] = set()
        self.starting: dict[int, asyncio.Task[Any]] = {}
        self.errors: dict[str, str] = {}
        self.stream: ClientConnection | None = None
        self.pending_events = asyncio.Event()
        self.tasks: set[asyncio.Task[None]] = set()
        self.stopped = asyncio.Event()
        self.catalog_changed = asyncio.Event()
        self._startup: set[asyncio.Task[None]] = set()
        # The owner's screen and the Yuki-dedicated Chrome; no screen backend, no computer.
        self.desk = Desk(journal.path.parent, screen, report, self.catalog_changed.set)
        self.files = FileTransfers(journal, self.desk.captures)
        self.terminal = LocalTerminal(
            journal.path.parent / "terminal", journal, self._terminal_event
        )

    def _transition(self, state: str, reason: str | None = None) -> None:
        if state == self.state:
            return
        self.state = state
        logger.info("connector_state %s%s", state, f" reason={reason}" if reason else "")
        self.report({"type": "state", "state": state} | ({"reason": reason} if reason else {}))

    def _retry(self, error: Exception) -> Exception | None:
        """Only handshake 401/403 ends reconnecting; websockets backs off the rest."""
        if isinstance(error, InvalidStatus):
            status = error.response.status_code
            if status in {401, 403}:
                return error
            self._transition("disconnected", f"http_{status}")
            return None
        fatal = process_exception(error)
        if fatal is None:
            self._transition("disconnected", type(error).__name__)
        return fatal

    async def _terminal_event(self, kind: str, value: dict[str, Any]) -> None:
        self.journal.append({"event_id": str(uuid4()), "kind": kind, **value})
        self.pending_events.set()

    def refresh_config(self) -> None:
        if self.config_path is None:
            return
        configured = Config.load(self.config_path)
        if configured.device_id != self.config.device_id:
            raise ValueError("The active service belongs to its original device")
        previous_access = self.config.full_access
        previous = {item.id: item for item in self.config.instances}
        current = {item.id: item for item in configured.instances}
        changed = {
            identity
            for identity, spec in previous.items()
            if identity not in current
            or replace(spec, label=current[identity].label) != current[identity]
        }
        self.config = replace(
            self.config, instances=configured.instances, full_access=configured.full_access
        )
        for identity in changed:
            driver = self.drivers.pop(identity, None)
            self.errors.pop(identity, None)
            if driver is not None:
                start = self.starting.get(id(driver))
                if start is not None and not any(
                    owner is driver for owner in self.operations.values()
                ):
                    start.cancel()
                else:
                    self._stop_if_unused(driver)
        for identity, spec in current.items():
            if identity not in previous or identity in changed:
                self._start(spec)
        if (
            configured.instances != list(previous.values())
            or configured.full_access != previous_access
        ):
            self.catalog_changed.set()

    def _stop_if_unused(self, driver: Driver) -> None:
        if (
            id(driver) not in self.starting
            and not any(value is driver for value in self.drivers.values())
            and not any(value is driver for value in self.operations.values())
        ):
            self._spawn(driver.stop())

    def _release_operation(self, identity: str) -> None:
        if (
            identity in self.dispatching
            or identity in self.detached_work
            or self.journal.observation(identity) is not None
        ):
            return
        driver = self.operations.pop(identity, None)
        if driver is not None:
            self._stop_if_unused(driver)

    def _original_driver(self, instance_id: str, identity: str) -> Driver | None:
        original = self.journal.original(identity)
        if original is None or original[0].get("instance_id") != instance_id:
            raise DriverError("original_operation_not_found", "Original operation is unavailable")
        owner = self.operations.get(identity)
        if owner is not None:
            return owner
        driver = self.drivers.get(instance_id)
        if (
            driver is not None
            and instance_id not in self.errors
            and driver.spec.driver not in {"claude", "acp"}
            and original[1] is not None
            and original[1] == instance_source(driver.spec)
        ):
            return driver
        return None

    def device_capabilities(self) -> dict[str, Any]:
        computer = computer_status()
        screen = self.desk.facts()
        return {
            "full_access": self.config.full_access is True,
            "platform": computer["platform"],
            "shell": computer["shell"],
            "tty": False,
            "interrupt_supported": computer["interrupt_supported"],
            **({"computer": screen} if screen is not None else {}),
        }

    async def _device_command(self, command: dict[str, Any]) -> dict[str, Any]:
        resource, action = command["resource"], command["action"]
        args = command.get("arguments", {})
        if resource == "terminal" and action == "status":
            screen = await asyncio.to_thread(self.desk.facts)
            return {
                "full_access": self.config.full_access is True,
                **computer_status(),
                **({"computer": screen} if screen is not None else {}),
            }
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
        if resource in {"computer", "browser"}:
            return await self.desk.execute(command)
        raise DriverError("invalid_resource", f"Unsupported device resource: {resource}")

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)

        def finished(task: asyncio.Task[None]) -> None:
            self.tasks.discard(task)
            if not task.cancelled() and (error := task.exception()) is not None:
                logger.error("Connector operation failed: %s", type(error).__name__)

        task.add_done_callback(finished)

    async def _recover(self, instance_id: str, driver: Driver) -> None:
        observations = self.journal.observations(instance_id)
        source = instance_source(driver.spec)
        for command, _ in observations:
            original = self.journal.original(command["id"])
            if (
                driver.spec.driver not in {"claude", "acp"}
                and original is not None
                and original[1] == source
            ):
                self.operations.setdefault(command["id"], driver)
        for command, _ in observations:
            owner = self.operations.get(command["id"])
            if owner is not driver:
                continue
            current = self.journal.observation(command["id"])
            if current is not None:
                self.operations[command["id"]] = driver
                await driver.recover(*current)

    def _start(self, spec: Instance) -> None:
        async def start() -> None:
            if not any(replace(item, label=spec.label) == spec for item in self.config.instances):
                return

            async def emit(kind: str, value: dict[str, Any]) -> None:
                if kind == "connection":
                    if driver is None or self.drivers.get(spec.id) is not driver:
                        return
                    if value["status"] == "available":
                        self.errors.pop(spec.id, None)
                        self._spawn(self._recover(spec.id, driver))
                    else:
                        self.errors[spec.id] = value.get(
                            "reason", value.get("error_type", "native_disconnected")
                        )
                    self.catalog_changed.set()
                    return
                if kind == "work_finished":
                    identity = value["operation_id"]
                    self.detached_work.discard(identity)
                    self._release_operation(identity)
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
                if value.get("operation_id"):
                    if kind == "completed":
                        self.detached_work.discard(value["operation_id"])
                    self._release_operation(value["operation_id"])

            driver: Driver | None = None
            try:
                driver = make_driver(spec, emit)
                self.drivers[spec.id] = driver
                self.errors[spec.id] = "starting"
                task = asyncio.current_task()
                assert task is not None
                self.starting[id(driver)] = task
                await driver.start()
                self.starting.pop(id(driver), None)
                if self.drivers.get(spec.id) is not driver:
                    self._stop_if_unused(driver)
                    return
                self.errors.pop(spec.id, None)
                self.catalog_changed.set()
                await self._recover(spec.id, driver)
            except asyncio.CancelledError:
                if driver:
                    self.starting.pop(id(driver), None)
                    await driver.stop()
                    if self.drivers.get(spec.id) is driver:
                        self.drivers.pop(spec.id, None)
                raise
            except Exception as exc:
                if self.drivers.get(spec.id) is driver:
                    self.errors[spec.id] = (
                        exc.code
                        if isinstance(exc, DriverError)
                        else "native_not_installed"
                        if isinstance(exc, FileNotFoundError)
                        else type(exc).__name__
                    )
                    self.catalog_changed.set()
                    self.drivers.pop(spec.id, None)
                logger.error("Native instance unavailable: %s (%s)", spec.id, type(exc).__name__)
                if driver:
                    self.starting.pop(id(driver), None)
                    self._stop_if_unused(driver)

        task = asyncio.create_task(start(), name=f"native-start-{spec.id}")
        self._startup.add(task)
        task.add_done_callback(self._startup.discard)

    async def start_drivers(self) -> None:
        for command, receipt in self.journal.observations():
            if (
                command.get("resource")
                or not command.get("instance_id")
                or command["id"] in self.operations
                or receipt.get("status") == "unknown"
            ):
                continue
            last_confirmed = {
                key: value for key, value in receipt.items() if key != "last_confirmed"
            }
            self.journal.append(
                {
                    "type": "event",
                    "event_id": str(uuid4()),
                    "instance_id": command["instance_id"],
                    "operation_id": command["id"],
                    "kind": "progress",
                    **{
                        key: receipt.get(key) or command.get(key)
                        for key in ("session_id", "execution_id")
                        if receipt.get(key) or command.get(key)
                    },
                    "snapshot_sequence": receipt.get("state_sequence", 0),
                    "payload": {
                        "status": "unknown",
                        "pending": True,
                        "reason": "original_owner_not_attached",
                        "stream_gap": True,
                        "last_confirmed": last_confirmed,
                    },
                }
            )
            self.pending_events.set()
        for spec in self.config.instances:
            self._start(spec)
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
        arguments = command.get("arguments", {})
        mutation = command["action"] not in READ_ACTIONS
        recorded = False
        detached_observation = None
        try:
            if command.get("resource"):
                self.refresh_config()
            if mutation:
                original_id = arguments.get("operation_id")
                driver = None
                if not command.get("resource"):
                    driver = (
                        self.operations.get(original_id)
                        if original_id and command["action"] in ORIGINAL_CONTROLS
                        else self.drivers.get(command["instance_id"])
                    )
                source = instance_source(driver.spec) if driver else None
                if source is None and original_id and command["action"] in {"queue", "watch"}:
                    stored_origin = self.journal.original(original_id)
                    if stored_origin is not None and stored_origin[0].get(
                        "instance_id"
                    ) == command.get("instance_id"):
                        source = stored_origin[1]
                previous = self.journal.begin(command, owner_source=source)
                if previous is not None:
                    return previous
                recorded = True
                if (
                    original_id
                    and command["action"] in {"queue", "watch"}
                    and not any(
                        spec.id == command.get("instance_id") for spec in self.config.instances
                    )
                ):
                    raise DriverError(
                        "instance_unavailable", "The configured native instance is unavailable"
                    )
            if command.get("resource"):
                result = await self._device_command(command)
            elif command["action"] == "get" and arguments.get("operation_id"):
                result = self.journal.operation(arguments["operation_id"])
                if result.get("status") == "unknown":
                    observation = self.journal.observation(arguments["operation_id"])
                    driver = self._original_driver(
                        command["instance_id"], arguments["operation_id"]
                    )
                    if observation and driver:
                        original, receipt = observation
                        await driver.recover(original, receipt)
                        result = self.journal.operation(arguments["operation_id"])
            elif command["action"] == "detach":
                detached_observation = self.journal.observation(arguments["operation_id"])
                driver = self._original_driver(command["instance_id"], arguments["operation_id"])
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
                original_id = arguments.get("operation_id")
                targeting_original = bool(original_id and command["action"] in ORIGINAL_CONTROLS)
                driver = (
                    self._original_driver(command["instance_id"], original_id)
                    if targeting_original
                    else self.drivers.get(command["instance_id"])
                )
                if driver is None or (
                    not targeting_original and command["instance_id"] in self.errors
                ):
                    raise DriverError(
                        "instance_unavailable", "The configured native instance is unavailable"
                    )
                self.operations[key] = driver
                self.dispatching.add(key)
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
        self.dispatching.discard(key)
        self._release_operation(key)
        if command["action"] == "detach" and arguments.get("operation_id"):
            original_id = arguments["operation_id"]
            if (
                detached_observation is not None
                and detached_observation[0]["action"] in {"prompt", "queue"}
                and response.get("result", {}).get("detached") is True
                and original_id in self.operations
                and self.operations[original_id].spec.driver != "codex"
            ):
                # Detach ends observation; the SDK owner still holds the work
                # until its native completion signal arrives.
                self.detached_work.add(original_id)
            else:
                self._release_operation(original_id)
        elif command["action"] == "cancel" and arguments.get("operation_id"):
            original_id = arguments["operation_id"]
            result = response.get("result", {})
            if result.get("pending") is False and result.get("status") in {
                "completed",
                "succeeded",
                "cancelled",
            }:
                self.detached_work.discard(original_id)
                self._release_operation(original_id)
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

    async def connection(self, websocket: ClientConnection) -> None:
        self.stream = websocket
        self.refresh_config()
        await websocket.send(json.dumps(self.hello(), ensure_ascii=False))
        self._transition("connected")
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
            self._spawn(self.desk.watch())
            async for websocket in connect(
                self.config.url,
                additional_headers={"Authorization": f"Bearer {self.config.token}"},
                max_size=None,
                process_exception=self._retry,
            ):
                try:
                    await self.connection(websocket)
                except ConnectionClosed as exc:
                    if (
                        exc.rcvd
                        and exc.rcvd.code == 1008
                        and exc.rcvd.reason == "unsupported_connector_protocol"
                    ):
                        self._transition("protocol_unsupported")
                        logger.error(
                            "unsupported_connector_protocol: Connector and Yuki protocol versions "
                            "are incompatible; update the older installation"
                        )
                        await self.stopped.wait()
                        break
                    closed = exc.rcvd
                    self._transition(
                        "disconnected",
                        "connection_lost"
                        if closed is None
                        else f"close_{closed.code}:{closed.reason}"
                        if closed.reason
                        else f"close_{closed.code}",
                    )
                    logger.info("Server disconnected; native executions continue")
                if self.stopped.is_set():
                    break
        except InvalidStatus:
            # _retry lets only handshake 401/403 reach here.
            self._transition("authorization_lost")
            await self.authorization_lost()
        finally:
            # Application/service shutdown is distinct from a socket reconnect.
            # Codex closes only its client/proxy; SDK/ACP processes are our own.
            original = sys.exception()

            async def close_owners() -> None:
                await self.terminal.close()
                await self.files.close()
                await self.desk.close()
                owners = (*self._startup, *self.tasks)
                for task in owners:
                    task.cancel()
                await asyncio.gather(*owners, return_exceptions=True)
                drivers = {
                    id(driver): driver
                    for driver in (*self.drivers.values(), *self.operations.values())
                }
                await asyncio.gather(*(driver.stop() for driver in drivers.values()))

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
    with path.open("a+b", buffering=0) as stream:
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
