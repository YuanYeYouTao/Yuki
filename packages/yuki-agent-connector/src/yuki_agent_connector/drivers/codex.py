"""Official app-server RPC against the existing owner, never another Codex runtime."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, cast
from uuid import uuid4

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from .. import __version__
from ..config import Instance
from ..discovery import creation_flags, profile_environment
from ..scope import Scope
from . import DriverError, Emit

APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
}
# An owner's answer applies to this one request (Q1); session caches and policy
# amendments would also answer later requests.
ONE_TIME_DECISIONS = ("accept", "decline", "cancel")
# Narrowed thread settings apply to the thread's next turns; a turn that starts
# before Yuki's queued input (for example the user's) also runs with them.
SCOPE_RACE = "turn_starting_before_yuki_input_uses_narrowed_settings"


def thread_settings(resumed: dict[str, Any]) -> dict[str, Any]:
    """The thread's own settings as thread/resume or thread/start reports them."""
    profile = resumed.get("activePermissionProfile") or {}
    return {
        "approvalPolicy": resumed.get("approvalPolicy"),
        "approvalsReviewer": resumed.get("approvalsReviewer"),
        "sandboxPolicy": resumed.get("sandbox"),
        "permissions": profile.get("id"),
        "cwd": resumed.get("cwd"),
    }


def restored_settings(thread: str, original: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {"threadId": thread, "approvalPolicy": original["approvalPolicy"]}
    if original["approvalsReviewer"] is not None:
        params["approvalsReviewer"] = original["approvalsReviewer"]
    # A named permissions profile cannot be combined with a sandbox policy.
    if original["permissions"]:
        params["permissions"] = original["permissions"]
    else:
        params["sandboxPolicy"] = original["sandboxPolicy"]
    return params


class CodexRPC:
    def __init__(self, spec: Instance, notify: Any, *, prepare_daemon: bool = True) -> None:
        self.spec, self.notify = spec, notify
        self.prepare_daemon = prepare_daemon
        self.ws: ClientConnection | None = None
        self.proxy: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task[None] | None = None
        self.relays: list[asyncio.Task[None]] = []
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.pipe: asyncio.StreamWriter | None = None
        self.connection_id = str(uuid4())
        self.stopping = False
        self.initialized = False

    async def start(self) -> None:
        if self.spec.installation:
            from ..discovery import refresh

            self.spec = refresh(self.spec)
        if self.spec.endpoint:
            try:
                self.ws = await connect(self.spec.endpoint, max_size=None, proxy=None)
            except (InvalidHandshake, OSError) as exc:
                raise DriverError(
                    "native_owner_unavailable", "Cannot connect to the selected owner"
                ) from exc
        else:
            environment = profile_environment(self.spec, "CODEX_HOME")
            start_failure: DriverError | None = None
            if self.prepare_daemon and not self.spec.socket:
                try:
                    launch = await asyncio.create_subprocess_exec(
                        self.spec.command,
                        *self.spec.args,
                        "app-server",
                        "daemon",
                        "start",
                        env=environment,
                        cwd=str(Path.home()),
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        creationflags=creation_flags,
                    )
                except FileNotFoundError as exc:
                    raise DriverError(
                        "native_not_installed", "Selected Codex program is missing"
                    ) from exc
                except OSError as exc:
                    raise DriverError(
                        "native_daemon_start_failed", "Could not start the selected Codex service"
                    ) from exc
                try:
                    status = await launch.wait()
                except BaseException:
                    if launch.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            launch.terminate()
                    await launch.wait()
                    raise
                if status:
                    start_failure = DriverError(
                        "native_daemon_start_failed",
                        "Could not start the selected Codex service",
                        {"exit_code": status},
                    )
            # An unavailable lifecycle command need not prevent connecting an
            # already-running owner through this same profile's existing proxy.
            # socketpair is private to this process. No listening TCP endpoint is
            # exposed. websockets owns the handshake, masking, control frames,
            # fragmentation and close; official proxy alone owns daemon transport.
            client, relay = socket.socketpair()
            client.setblocking(False)
            relay.setblocking(False)
            try:
                proxy_args = ["app-server", "proxy"]
                # Preserve the selected native profile/owner when its original
                # configuration used paths relative to the default task directory.
                if self.spec.socket:
                    owner_socket = Path(self.spec.socket)
                    if not owner_socket.is_absolute():
                        owner_socket = (Path(self.spec.cwd) / owner_socket).resolve()
                    proxy_args.extend(["--sock", str(owner_socket)])
                self.proxy = await asyncio.create_subprocess_exec(
                    self.spec.command,
                    *self.spec.args,
                    *proxy_args,
                    env=environment,
                    cwd=str(Path.home()),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=creation_flags,
                )
                incoming, self.pipe = await asyncio.open_connection(sock=relay)
                assert self.proxy.stdin is not None and self.proxy.stdout is not None
                self.relays = [
                    asyncio.create_task(self._copy(incoming, self.proxy.stdin)),
                    asyncio.create_task(self._copy(self.proxy.stdout, self.pipe)),
                ]
                self.ws = await connect("ws://localhost/", sock=client, max_size=None, proxy=None)
            except FileNotFoundError as exc:
                client.close()
                relay.close()
                raise DriverError(
                    "native_not_installed", "Selected Codex program is missing"
                ) from exc
            except (InvalidHandshake, OSError) as exc:
                client.close()
                relay.close()
                if start_failure is not None:
                    raise start_failure from exc
                raise DriverError(
                    "native_owner_unavailable", "Cannot connect to the selected owner"
                ) from exc
            except BaseException:
                client.close()
                relay.close()
                raise
        self.reader = asyncio.create_task(self._receive())
        await self.call(
            "initialize",
            {
                "clientInfo": {"name": "yuki_agent_connector", "version": __version__},
                "capabilities": {"experimentalApi": True},
            },
        )
        self.initialized = True
        await self.send({"method": "initialized"})

    @staticmethod
    async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while chunk := await reader.read(65536):
                writer.write(chunk)
                await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            writer.close()

    async def send(self, message: dict[str, Any]) -> None:
        if self.ws is None:
            raise DriverError("owner_disconnected", "Native owner is not connected")
        try:
            await self.ws.send(json.dumps(message, ensure_ascii=False))
        except ConnectionClosed as exc:
            raise DriverError(
                "unknown", "Native connection lost; query the original execution", uncertain=True
            ) from exc

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        key = str(uuid4())
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            await self.send({"id": key, "method": method, "params": params})
            return await future
        finally:
            self.pending.pop(key, None)

    async def _receive(self) -> None:
        assert self.ws is not None
        error_type = "EOF"
        try:
            async for raw in self.ws:
                message = json.loads(raw)
                future = self.pending.get(str(message.get("id")))
                if future is not None and "method" not in message:
                    if future.done():
                        continue  # The caller cancelled this RPC while its response was in flight.
                    if "error" in message:
                        future.set_exception(
                            DriverError(
                                "native_error",
                                message["error"]["message"],
                                message["error"],
                                uncertain=message["error"].get("code")
                                not in {-32600, -32601, -32602},
                            )
                        )
                    else:
                        future.set_result(message.get("result", {}))
                else:
                    # Callbacks may answer native RPCs, so run outside the reader.
                    await self.notify(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_type = type(exc).__name__
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(
                        DriverError(
                            "unknown",
                            "Native connection lost; query the original execution",
                            uncertain=True,
                        )
                    )
            if not self.stopping and self.initialized:
                await self.notify(
                    {
                        "method": "connector/ownerDisconnected",
                        "params": {
                            "error_type": error_type,
                            "reason": "native_event_stream_lost",
                            "stream_gap": True,
                        },
                    }
                )

    async def stop(self) -> None:
        self.stopping = True
        if self.ws is not None:
            await self.ws.close()
        if self.reader is not None:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        for task in self.relays:
            task.cancel()
        await asyncio.gather(*self.relays, return_exceptions=True)
        if self.pipe is not None:
            self.pipe.close()
        if self.proxy is not None:
            # This is only our byte proxy, never the user's daemon process.
            if self.proxy.returncode is None:
                self.proxy.terminate()
            await self.proxy.wait()


def native_status(thread: dict[str, Any]) -> str:
    state = thread.get("status", {})
    kind = state.get("type") if isinstance(state, dict) else state
    return {"active": "running", "idle": "idle", "systemError": "failed"}.get(str(kind), "unknown")


def turn_status(turn: dict[str, Any]) -> str:
    return {
        "inProgress": "running",
        "completed": "completed",
        "failed": "failed",
        "interrupted": "cancelled",
    }.get(turn.get("status", ""), "unknown")


def item_view(item: dict[str, Any], *, details: bool = False) -> dict[str, Any] | None:
    kind = item.get("type")
    fields = {
        "agentMessage": ("id", "type", "text", "phase"),
        "userMessage": ("id", "type", "clientId", "content"),
        "commandExecution": ("id", "type", "command", "cwd", "status", "exitCode", "durationMs"),
        "fileChange": ("id", "type", "status"),
        "mcpToolCall": ("id", "type", "server", "tool", "status"),
        "dynamicToolCall": ("id", "type", "namespace", "tool", "status", "success"),
        "collabAgentToolCall": (
            "id",
            "type",
            "tool",
            "status",
            "senderThreadId",
            "receiverThreadIds",
            "agentsStates",
        ),
        "webSearch": ("id", "type", "query", "status"),
        "plan": ("id", "type", "text"),
    }.get(str(kind))
    if kind == "reasoning":
        return None
    if fields is None:
        return {
            **{key: item[key] for key in ("id", "type", "status") if key in item},
            "content_omitted": True,
        }
    result = {key: item[key] for key in fields if key in item}
    if details:
        result.update(
            {
                key: item[key]
                for key in (
                    "arguments",
                    "input",
                    "result",
                    "error",
                    "aggregatedOutput",
                    "contentItems",
                )
                if key in item
            }
        )
    if kind == "fileChange":
        result["changes"] = [
            {key: change[key] for key in ("path", "kind") if key in change}
            for change in item.get("changes", [])
        ]
    if any(key not in result for key in item) or (
        kind == "fileChange" and any("diff" in change for change in item.get("changes", []))
    ):
        result["content_omitted"] = True
    return result


def turn_view(turn: dict[str, Any], *, details: bool = False) -> dict[str, Any]:
    result = {
        key: turn[key]
        for key in ("id", "status", "error", "startedAt", "completedAt", "itemsView")
        if key in turn
    }
    result["items"] = [
        view
        for item in turn.get("items", [])
        if (view := item_view(item, details=details)) is not None
    ]
    result["content_projection"] = "public_actions"
    result["items_order"] = "desc" if turn.get("itemsView") == "full_page" else "asc"
    result["items_omitted"] = len(result["items"]) != len(turn.get("items", []))
    return result


class CodexDriver:
    capabilities: ClassVar[list[str]] = [
        "list",
        "get",
        "watch",
        "prompt",
        "steer",
        "queue",
        "respond",
        "cancel",
        "detach",
        "scope",
        "observe",
        "release",
    ]

    def __init__(self, spec: Instance, emit: Emit) -> None:
        self.spec, self.emit = spec, emit
        self.rpc = CodexRPC(spec, self._notification)
        self.watches: dict[str, str] = {}  # original watch operation -> native session
        self.watch_turns: dict[str, str | None] = {}
        self.turn_operations: dict[str, str] = {}
        self.turn_sessions: dict[str, str] = {}
        self.queue_operations: dict[str, str] = {}
        self.buffered: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.requests: dict[str, dict[str, Any]] = {}
        self.queue_sessions: dict[str, str] = {}
        self.owner: asyncio.Task[None] | None = None
        self.ready: asyncio.Future[None] | None = None
        self.recoveries: dict[str, asyncio.Task[None]] = {}
        # Observe watches: metadata only, never an answerer (§4.3).
        self.observers: set[str] = set()
        self.scopes: dict[str, Scope] = {}
        # Thread settings before Yuki first narrowed them; they stick to the thread
        # and would leak into the user's own turns (§15 E6), so they are restored.
        self.original_settings: dict[str, dict[str, Any]] = {}
        self.narrowed: dict[str, set[str]] = {}
        self.thread_cwd: dict[str, str] = {}
        # Turn -> fileChange item -> paths, for approvals that name only the item.
        self.item_paths: dict[str, dict[str, list[str]]] = {}
        # Thread -> Yuki turn after which Yuki's subscription is released.
        self.releasing: dict[str, str] = {}
        self.background: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self.ready = asyncio.get_running_loop().create_future()
        self.owner = asyncio.create_task(self._serve())
        await self.ready

    async def _serve(self) -> None:
        assert self.ready is not None
        while True:
            failure: Exception | None = None
            try:
                await self.rpc.start()
                if not self.ready.done():
                    self.ready.set_result(None)
                else:
                    await self.emit("connection", {"status": "available"})
                assert self.rpc.reader is not None
                await self.rpc.reader
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self.ready.done():
                    failure = exc
                else:
                    await self.emit(
                        "connection",
                        {
                            "status": "unavailable",
                            "reason": exc.code
                            if isinstance(exc, DriverError)
                            else type(exc).__name__,
                        },
                    )
            finally:
                await self.rpc.stop()
            if failure is not None:
                if not self.ready.done():
                    self.ready.set_exception(failure)
                return
            await asyncio.sleep(1)
            self.rpc = CodexRPC(self.spec, self._notification, prepare_daemon=False)

    async def stop(self) -> None:
        self.rpc.stopping = True
        recoveries = [*self.recoveries.values(), *self.background]
        for task in recoveries:
            task.cancel()
        await asyncio.gather(*recoveries, return_exceptions=True)
        if self.owner is not None:
            self.owner.cancel()
            await asyncio.gather(self.owner, return_exceptions=True)
        else:
            await self.rpc.stop()

    async def _loaded(self, session: str) -> bool:
        cursor = None
        while True:
            page = await self.rpc.call("thread/loaded/list", {"cursor": cursor})
            if session in page.get("data", []):
                return True
            cursor = page.get("nextCursor")
            if not cursor:
                return False

    async def _turns(
        self, session: str, *, items: str = "notLoaded"
    ) -> AsyncIterator[dict[str, Any]]:
        cursor = None
        while True:
            page = await self.rpc.call(
                "thread/turns/list",
                {
                    "threadId": session,
                    "cursor": cursor,
                    "itemsView": items,
                    "sortDirection": "desc",
                },
            )
            for turn in page.get("data", []):
                yield turn
            cursor = page.get("nextCursor")
            if not cursor:
                return

    async def _turn_page(
        self, session: str, *, cursor: str | None = None, limit: int = 30
    ) -> dict[str, Any]:
        return await self.rpc.call(
            "thread/turns/list",
            {
                "threadId": session,
                "cursor": cursor,
                "limit": limit,
                "itemsView": "summary",
                "sortDirection": "desc",
            },
        )

    async def _find_turn(
        self, session: str, execution: str | None = None, *, client_id: str | None = None
    ) -> dict[str, Any] | None:
        async for turn in self._turns(session, items="full" if client_id else "notLoaded"):
            if (
                turn["id"] == execution
                if execution
                else any(
                    item.get("type") == "userMessage" and item.get("clientId") == client_id
                    for item in turn.get("items", [])
                )
                if client_id
                else turn.get("status") == "inProgress"
            ):
                return turn
        return None

    async def recover(self, command: dict[str, Any], result: dict[str, Any]) -> None:
        operation = command["id"]
        task = self.recoveries.get(operation)
        if task is None:
            task = asyncio.create_task(self._recover(command, result))
            self.recoveries[operation] = task
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            assert current is not None
            if current.cancelling():
                raise
        finally:
            if self.recoveries.get(operation) is task:
                self.recoveries.pop(operation)

    async def _recover(self, command: dict[str, Any], result: dict[str, Any]) -> None:
        session = result.get("session_id") or command.get("session_id")
        execution, operation = (
            result.get("execution_id") or command.get("execution_id"),
            command["id"],
        )
        if not session:
            return
        session_scope = (
            command["action"] == "watch" and command.get("observation_scope") == "session"
        )
        scope = command.get("arguments", {}).get("scope")
        if scope is not None and result.get("thread_settings_before") is not None:
            self._rescope(session, operation, scope, result["thread_settings_before"])
        try:
            if command["action"] == "watch":
                if not session_scope and not execution:
                    raise DriverError(
                        "observation_gap", "Original first execution cannot be proven"
                    )
                self.watches[operation] = session
                self.watch_turns[operation] = None if session_scope else execution
            if session_scope:
                original = None
                async for latest in self._turns(session):
                    original = latest
                    break
            else:
                original = await self._find_turn(
                    session,
                    execution,
                    client_id=operation
                    if not execution and command["action"] in {"prompt", "queue"}
                    else None,
                )
            state = "unknown"
            if original:
                execution = original["id"]
                state = turn_status(original)
                if command["action"] != "watch":
                    self.turn_operations[execution] = operation
                    self.turn_sessions[execution] = session
                if state in {"completed", "failed", "cancelled"}:
                    if not session_scope:
                        await self.emit(
                            "completed",
                            {
                                "session_id": session,
                                "execution_id": execution,
                                "operation_id": operation,
                                "snapshot_sequence": result.get("state_sequence", 0),
                                "payload": {"status": state, "pending": False, "admitted": True},
                            },
                        )
                        self.turn_operations.pop(execution, None)
                        self.turn_sessions.pop(execution, None)
                        self.watches.pop(operation, None)
                        self.watch_turns.pop(operation, None)
                        self._unscope(operation)
                        return
            elif command["action"] in {"prompt", "queue"}:
                # Yuki's prompts are queue items too; either may still be queued.
                cursor = None
                while True:
                    page = await self.rpc.call(
                        "thread/queue/list", {"threadId": session, "cursor": cursor}
                    )
                    queued = next(
                        (
                            item
                            for item in page.get("data", [])
                            if item["id"] == result.get("queued_submission_id")
                            or item.get("clientUserMessageId") == operation
                        ),
                        None,
                    )
                    if queued:
                        self.queue_operations[operation] = queued["id"]
                        self.queue_sessions[operation] = session
                        state = "queued"
                        break
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
            live = await self._loaded(session)
            if live:
                await self.rpc.call("thread/resume", {"threadId": session, "excludeTurns": True})
                if session_scope and state != "running":
                    state = "idle"
            elif state in {"running", "queued"} or session_scope:
                state = "unknown"
            await self.emit(
                "progress",
                {
                    "session_id": session,
                    "execution_id": execution,
                    "operation_id": operation,
                    "snapshot_sequence": result.get("state_sequence", 0),
                    "payload": {
                        "status": state,
                        "pending": True,
                        **(
                            {"execution_status": turn_status(original)}
                            if session_scope and original
                            else {}
                        ),
                        "reason": "native_snapshot_restored",
                        "stream_gap": not live,
                        **(
                            {"admitted": True}
                            if original or state == "queued" or (session_scope and live)
                            else {}
                        ),
                    },
                },
            )
        except DriverError as exc:
            await self.emit(
                "progress",
                {
                    "session_id": session,
                    "execution_id": execution,
                    "operation_id": operation,
                    "snapshot_sequence": result.get("state_sequence", 0),
                    "payload": {
                        "status": "unknown",
                        "pending": True,
                        "reason": "native_reconciliation_unavailable",
                        "error_code": exc.code,
                        "stream_gap": True,
                    },
                },
            )

    def _spawn(self, coroutine: Any) -> None:
        # RPC calls wait for the reader that delivers this notification.
        task = asyncio.create_task(coroutine)
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    async def _restore(self, thread: str, operation: str) -> None:
        original = self.original_settings.pop(thread, None)
        if original is None:
            return
        try:
            await self.rpc.call("thread/settings/update", restored_settings(thread, original))
        except DriverError as exc:
            # Kept for the next scoped end or release on this thread to retry.
            self.original_settings.setdefault(thread, original)
            await self.emit(
                "progress",
                {
                    "session_id": thread,
                    "operation_id": operation,
                    "payload": {
                        "scope_restore_failed": True,
                        "error_code": exc.code,
                        "thread_settings": original,
                    },
                },
            )

    def _unscope(self, operation: str) -> None:
        """A scoped operation ended: restore its thread once no scoped operation remains."""
        if self.scopes.pop(operation, None) is None:
            return
        for thread, operations in list(self.narrowed.items()):
            operations.discard(operation)
            if not operations:
                del self.narrowed[thread]
                self._spawn(self._restore(thread, operation))

    def _rescope(
        self, thread: str, operation: str, value: dict[str, Any], before: dict[str, Any]
    ) -> None:
        """After a restart: the scope and the settings to restore, from the receipt."""
        self.original_settings.setdefault(thread, before)
        cwd = str(before.get("cwd") or self.spec.cwd)
        scope = Scope.parse(value, cwd)
        assert scope is not None
        self.scopes[operation] = scope
        self.narrowed.setdefault(thread, set()).add(operation)
        self.thread_cwd[thread] = cwd

    async def _narrow(
        self, thread: str, operation: str, value: dict[str, Any], resumed: dict[str, Any]
    ) -> None:
        """Set the scope as thread settings for Yuki's turn; never wider than asked."""
        cwd = str(resumed.get("cwd") or self.spec.cwd)
        scope = Scope.parse(value, cwd)
        assert scope is not None
        if not scope.covers(cwd):
            # workspaceWrite always makes the thread's cwd writable; it cannot be
            # excluded, so a scope narrower than the cwd cannot be enforced.
            raise DriverError(
                "scope_unsupported",
                "Codex workspace writes always include the thread's working directory",
                {"cwd": cwd},
            )
        first = thread not in self.original_settings
        if first:
            self.original_settings[thread] = thread_settings(resumed)
        try:
            await self.rpc.call(
                "thread/settings/update",
                {
                    "threadId": thread,
                    "approvalPolicy": "on-request",
                    # Approvals reach a person (via Yuki), not an automatic reviewer.
                    "approvalsReviewer": "user",
                    "sandboxPolicy": {
                        "type": "workspaceWrite",
                        "writableRoots": [str(path) for path in scope.write_paths],
                        "networkAccess": scope.network,
                        # Otherwise /tmp and $TMPDIR are writable as well.
                        "excludeSlashTmp": True,
                        "excludeTmpdirEnvVar": True,
                    },
                },
            )
        except DriverError as exc:
            if first:
                self.original_settings.pop(thread, None)
            if exc.uncertain:
                raise
            # Input carries no policy of its own (thread/queue/add): without settings
            # that can be set and restored, the scope cannot be enforced.
            raise DriverError(
                "scope_unsupported",
                "This Codex owner cannot set and restore thread settings",
                exc.details,
            ) from exc
        self.scopes[operation] = scope
        self.narrowed.setdefault(thread, set()).add(operation)
        self.thread_cwd[thread] = cwd

    def _inside(self, method: str, params: dict[str, Any], scope: Scope, thread: str) -> bool:
        cwd = str(params.get("cwd") or self.thread_cwd.get(thread) or self.spec.cwd)
        if method == "item/commandExecution/requestApproval":
            command = params.get("command")
            return (
                not params.get("additionalPermissions")
                and (scope.network or not params.get("networkApprovalContext"))
                and isinstance(command, (str, list))
                and scope.runs(command)
            )
        if method == "item/fileChange/requestApproval":
            paths = self.item_paths.get(str(params.get("turnId")), {}).get(
                str(params.get("itemId"))
            )
            grant = params.get("grantRoot")
            return (
                bool(paths)
                and all(scope.writes(path, cwd) for path in paths or [])
                and (grant is None or scope.writes(str(grant), cwd))
            )
        return False

    async def _unsubscribe(self, thread: str) -> None:
        # Only Yuki's own client subscription; the daemon and its thread go on.
        await self.rpc.call("thread/unsubscribe", {"threadId": thread})

    def _held(self, thread: str) -> bool:
        return (
            thread in self.watches.values()
            or thread in self.turn_sessions.values()
            or any(
                self.queue_sessions.get(operation) == thread for operation in self.queue_operations
            )
        )

    async def _notification(self, message: dict[str, Any]) -> None:
        method = message.get("method", "")
        params = message.get("params", {})
        session = params.get("threadId")
        turn = params.get("turnId") or params.get("turn", {}).get("id")
        started_item = params.get("item", {})
        if method == "item/started" and started_item.get("type") == "fileChange":
            self.item_paths.setdefault(str(turn), {})[started_item["id"]] = [
                change["path"] for change in started_item.get("changes", []) if "path" in change
            ]
        if method == "turn/completed":
            self.item_paths.pop(str(turn), None)
        if method == "turn/completed" and turn and self.releasing.get(session) == turn:
            del self.releasing[session]
            if not self._held(session):
                self._spawn(self._unsubscribe(session))
        if method == "connector/ownerDisconnected":
            self.requests.clear()
            await self.emit("connection", {"status": "unavailable", **params})
            for operation, session_id in self.watches.items():
                await self.emit(
                    "progress",
                    {
                        "session_id": session_id,
                        "operation_id": operation,
                        "payload": {"status": "unknown", "pending": True, **params},
                    },
                )
            for turn_id, operation in self.turn_operations.items():
                await self.emit(
                    "progress",
                    {
                        "execution_id": turn_id,
                        "session_id": self.turn_sessions.get(turn_id),
                        "operation_id": operation,
                        "payload": {"status": "unknown", "pending": True, **params},
                    },
                )
            for operation, session_id in self.queue_sessions.items():
                if operation in self.queue_operations:
                    await self.emit(
                        "progress",
                        {
                            "session_id": session_id,
                            "operation_id": operation,
                            "payload": {"status": "unknown", "pending": True, **params},
                        },
                    )
            return
        if method == "serverRequest/resolved":
            resolved = self.requests.pop(str(params.get("requestId")), None)
            if resolved:
                turn = turn or resolved.get("params", {}).get("turnId")
        request = "id" in message and "method" in message
        operations = {
            op
            for op, sid in self.watches.items()
            if sid == session and self.watch_turns.get(op) in {None, "", turn}
        }
        if turn:
            for operation in operations:
                if self.watch_turns.get(operation) == "":
                    self.watch_turns[operation] = turn
        item = params.get("item", {})
        if item.get("type") == "userMessage" and item.get("clientId"):
            client_id = item["clientId"]
            # Yuki's input is a queue item; its turn is Yuki's by clientUserMessageId.
            if client_id in self.queue_operations:
                self.turn_operations[turn] = client_id
                self.turn_sessions[turn] = session
                self.queue_operations.pop(client_id, None)
                self.queue_sessions.pop(client_id, None)
                earlier_messages = self.buffered.pop((session, turn), [])
                if session not in self.queue_sessions.values():
                    # No Yuki input is pending here: other held turns are the user's.
                    for candidate in [key for key in self.buffered if key[0] == session]:
                        self.buffered.pop(candidate)
                for earlier in earlier_messages:
                    await self._notification(earlier)
        owner = self.turn_operations.get(turn) if turn else None
        if owner is not None:
            operations.add(owner)
        if not operations:
            if method == "turn/completed":
                # A turn that never became Yuki's: what was held for it is dropped.
                self.buffered.pop((session, turn), None)
            elif (
                turn
                and any(
                    self.queue_sessions.get(operation) == session
                    for operation in self.queue_operations
                )
                and (request or method in {"turn/started", "item/completed", "turn/plan/updated"})
            ):
                # Possibly Yuki's queued turn before its userMessage names it.
                self.buffered.setdefault((session, turn), []).append(message)
            return  # unobserved native sessions never enter the remote event stream
        kind = "progress"
        payload: dict[str, Any] = {"native_method": method, "admitted": True}
        metadata: dict[str, Any] = {}
        if request:
            key = str(message["id"])
            self.requests[key] = message
            scope = self.scopes.get(owner) if owner else None
            if (
                method in APPROVAL_METHODS
                and scope
                and self._inside(method, params, scope, session)
            ):
                # Yuki's own turn, inside the delegation's fixed scope (D2).
                self.requests.pop(key)
                await self.rpc.send({"id": message["id"], "result": {"decision": "accept"}})
                payload = {"permission_auto_approved": message["id"], "native_method": method}
                request = False
            else:
                kind = (
                    "question" if "UserInput" in method or "elicitation" in method else "permission"
                )
                payload.update(
                    permission_id=key,
                    native_request_id=message["id"],
                    native_request=params,
                    connection_id=self.rpc.connection_id,
                )
                metadata = {
                    "native_method": method,
                    "tool": method,
                    "native_request_id": message["id"],
                }
        elif method == "turn/completed":
            kind = "completed"
            original = params["turn"]
            payload.update(
                status={
                    "completed": "completed",
                    "failed": "failed",
                    "interrupted": "cancelled",
                }.get(original.get("status"), "unknown"),
                text="\n".join(
                    item["text"]
                    for item in original.get("items", [])
                    if item.get("type") == "agentMessage"
                ),
                error=original.get("error"),
                **{
                    name: original[name]
                    for name in ("startedAt", "completedAt")
                    if name in original
                },
            )
        elif method == "item/started" and item.get("type") == "userMessage":
            payload.update(
                status="running",
                native={key: item[key] for key in ("id", "type", "clientId") if key in item},
            )
        elif method == "item/completed":
            item = params.get("item", {})
            # Reasoning is neither a milestone nor a public report.
            view = item_view(item)
            if view is None or item.get("type") == "userMessage":
                return
            payload.update(item=view)
            if "text" in item:
                payload["text"] = item["text"]
        elif method == "turn/started":
            original = params["turn"]
            payload.update(
                status="running",
                **({"startedAt": original["startedAt"]} if "startedAt" in original else {}),
                native={
                    "threadId": session,
                    "turn": {
                        key: original[key]
                        for key in ("id", "status", "startedAt")
                        if key in original
                    },
                },
            )
        elif method in {
            "turn/plan/updated",
            "serverRequest/resolved",
        }:
            payload["native"] = params
        else:
            return  # deltas and duplicate status are not durable milestones
        for operation in operations:
            continuous = operation in self.watches and self.watch_turns.get(operation) is None
            event_payload = payload
            event_kind = kind
            if method == "turn/completed" and continuous:
                event_kind = "progress"
                event_payload = {
                    **payload,
                    "execution_status": payload["status"],
                    "status": "idle",
                    "pending": True,
                }
            if request and operation in self.observers:
                event_payload = {**metadata, "observed": True}
            elif request and owner is None:
                # The user's own TUI turn: app-server enforces no turn ownership, so
                # Yuki never answers it; it waits for whoever answers natively (§15 E1).
                event_payload = {**metadata, "answer_at": "native_terminal", "pending": True}
            await self.emit(
                event_kind,
                {
                    "session_id": session,
                    "execution_id": turn,
                    "operation_id": operation,
                    "payload": event_payload,
                },
            )
        if method == "turn/completed":
            self.turn_operations.pop(turn, None)
            self.turn_sessions.pop(turn, None)
            if owner is not None:
                self._unscope(owner)
            for operation in operations:
                if self.watch_turns.get(operation) == turn:
                    self.watches.pop(operation, None)
                    self.watch_turns.pop(operation, None)

    async def execute(self, command: dict[str, Any]) -> dict[str, Any]:
        action, arguments = command["action"], command.get("arguments", {})
        session, execution = cast(str, command.get("session_id")), command.get("execution_id")
        operation = command["id"]
        if action == "list":
            result = await self.rpc.call(
                "thread/list",
                {
                    "cursor": arguments.get("cursor"),
                    "limit": arguments.get("limit", 30),
                    "sortKey": "updated_at",
                    "sortDirection": "desc",
                    "useStateDbOnly": True,
                },
            )
            return {
                "sessions": [
                    {
                        "id": item["id"],
                        "label": item.get("name") or item.get("preview"),
                        "status": native_status(item),
                        "cwd": item.get("cwd"),
                        **{
                            key: item[key]
                            for key in (
                                "createdAt",
                                "updatedAt",
                                "recencyAt",
                                "source",
                                "forkedFromId",
                                "parentThreadId",
                            )
                            if key in item
                        },
                        "native_status": item.get("status"),
                    }
                    for item in result.get("data", [])
                ],
                "next_cursor": result.get("nextCursor"),
                "cursor_parameter": "native_cursor",
                "history_order": "updated_at_desc",
                "source_scope": "native_default_interactive_sources",
            }
        if action == "detach":
            detached = arguments["operation_id"]
            recovery = self.recoveries.pop(detached, None)
            if recovery is not None:
                recovery.cancel()
                await asyncio.gather(recovery, return_exceptions=True)
            threads = {session} if session else set()
            threads |= {thread for op, thread in self.watches.items() if op == detached}
            threads |= {thread for op, thread in self.queue_sessions.items() if op == detached}
            running = {
                turn_id: self.turn_sessions.get(turn_id)
                for turn_id, owner in self.turn_operations.items()
                if owner == detached
            }
            threads |= {thread for thread in running.values() if thread}
            self.observers.discard(detached)
            self._unscope(detached)
            self.watches.pop(detached, None)
            self.watch_turns.pop(detached, None)
            self.queue_operations.pop(detached, None)
            self.queue_sessions.pop(detached, None)
            for turn_id in running:
                self.turn_operations.pop(turn_id)
                self.turn_sessions.pop(turn_id, None)
            if not arguments.get("release"):
                return {
                    "status": "completed",
                    "session_id": session,
                    "detached": True,
                    "admitted": True,
                }
            closed: str | None = "now"
            for thread in threads:
                if self._held(thread):
                    closed = None
                    continue
                ending = next((key for key, value in running.items() if value == thread), None)
                if ending is not None:
                    # Yuki's turn ends first; then only Yuki's subscription is dropped.
                    self.releasing[thread] = ending
                    closed = "after_turn" if closed == "now" else closed
                else:
                    await self._unsubscribe(thread)
            return {
                "status": "detached",
                "session_id": session,
                "detached": True,
                "pending": False,
                "admitted": True,
                "closed": closed,
            }
        if action == "get":
            original = (
                await self.rpc.call("thread/read", {"threadId": session, "includeTurns": False})
            )["thread"]
            page = None
            if execution:
                selected = await self._find_turn(session, execution)
            else:
                page = await self._turn_page(
                    session,
                    cursor=arguments.get("cursor") if arguments.get("include_history") else None,
                    limit=arguments.get("limit", 30) if arguments.get("include_history") else 1,
                )
                current_page = (
                    await self._turn_page(session, limit=1) if arguments.get("cursor") else page
                )
                selected = next(iter(current_page.get("data", [])), None)
            state = (
                turn_status(selected)
                if selected
                else ("unknown" if execution else native_status(original))
            )
            result = {
                "session_id": session,
                "execution_id": selected["id"] if selected else execution,
                "status": state,
                "native": original,
                "pending": state in {"running", "unknown"},
                "query_scope": "execution" if execution else "session",
                "status_scope": "turn" if execution else "session",
                "observed_at": datetime.now(UTC).isoformat(),
                "native_status": original.get("status"),
            }
            if selected is not None:
                result["selected_turn" if execution else "latest_turn"] = turn_view(
                    selected, details=True
                )
            if not execution:
                result["status"] = native_status(original)
                result["pending"] = result["status"] in {"running", "unknown"}
            if (execution and selected is not None) or (
                arguments.get("include_history") and not execution
            ):
                if execution:
                    assert selected is not None
                    item_page = await self.rpc.call(
                        "thread/items/list",
                        {
                            "threadId": session,
                            "turnId": execution,
                            "cursor": arguments.get("cursor"),
                            "limit": arguments.get("limit", 30),
                            "sortDirection": "desc",
                        },
                    )
                    selected = {
                        **selected,
                        "items": [row["item"] for row in item_page.get("data", [])],
                        "itemsView": "full_page",
                    }
                    result["selected_turn"] = turn_view(selected, details=True)
                    page = item_page
                    result["history_items_view"] = "full_item_page"
                else:
                    assert page is not None
                    original["turns"] = [
                        turn_view(turn, details=True) for turn in page.get("data", [])
                    ]
                    result["history_items_view"] = "summary"
                result.update(
                    history_order="desc",
                    cursor_parameter="native_cursor",
                    next_cursor=page.get("nextCursor"),
                    backwards_cursor=page.get("backwardsCursor"),
                    history_scope="execution_items" if execution else "session_turns",
                )
            if execution and selected is None:
                result["reason"] = "execution_not_found"
            if not execution:
                queued_inputs: list[dict[str, Any]] = []
                cursor = None
                while True:
                    listed = await self.rpc.call(
                        "thread/queue/list", {"threadId": session, "cursor": cursor}
                    )
                    queued_inputs.extend(
                        {
                            "queued_submission_id": item["id"],
                            "operation_id": item.get("clientUserMessageId"),
                        }
                        for item in listed.get("data", [])
                    )
                    cursor = listed.get("nextCursor")
                    if not cursor:
                        break
                result["queued_inputs"] = queued_inputs
            return result
        if action == "watch":
            if not await self._loaded(session):
                raise DriverError(
                    "not_live", "Session is stored but not loaded in this native owner"
                )
            session_scope = command.get("observation_scope") == "session"
            self.watches[operation] = session
            self.watch_turns[operation] = None if session_scope else execution or ""
            if arguments.get("observation") == "observe":
                self.observers.add(operation)
            try:
                thread = (
                    await self.rpc.call(
                        "thread/resume", {"threadId": session, "excludeTurns": True}
                    )
                )["thread"]
                selected = (
                    await self._find_turn(session, execution)
                    if execution or native_status(thread) == "running"
                    else None
                )
            except BaseException:
                self.watches.pop(operation, None)
                self.watch_turns.pop(operation, None)
                raise
            if execution and selected is None:
                self.watches.pop(operation, None)
                self.watch_turns.pop(operation, None)
                raise DriverError(
                    "execution_not_found", "Original native execution is not in this session"
                )
            state = turn_status(selected) if selected else "idle" if session_scope else "unknown"
            terminal = state in {"completed", "failed", "cancelled"} and not session_scope
            if terminal:
                self.watches.pop(operation, None)
                self.watch_turns.pop(operation, None)
            elif selected and not session_scope:
                self.watch_turns[operation] = selected["id"]
            return {
                "session_id": session,
                "execution_id": selected["id"] if selected else None,
                "status": "idle"
                if session_scope and state in {"completed", "failed", "cancelled"}
                else state,
                "pending": not terminal,
                "admitted": True,
            }
        if action in {"prompt", "queue"}:
            # Yuki's input is always a queue item (§4.11, §15 E2): turn/start on a
            # running thread merges into the user's turn and returns its ID.
            resumed: dict[str, Any] | None = None
            if action == "prompt" and session:
                # A running (user) turn is no conflict: the queue item waits behind it.
                resumed = await self.rpc.call(
                    "thread/resume", {"threadId": session, "excludeTurns": True}
                )
            elif action == "prompt":
                resumed = await self.rpc.call(
                    "thread/start", {"cwd": arguments.get("cwd") or self.spec.cwd}
                )
                session = resumed["thread"]["id"]
            if arguments.get("scope") is not None:
                if resumed is None:
                    resumed = await self.rpc.call(
                        "thread/resume", {"threadId": session, "excludeTurns": True}
                    )
                await self._narrow(session, operation, arguments["scope"], resumed)
            self.queue_operations[operation] = ""
            self.queue_sessions[operation] = session
            try:
                result = await self.rpc.call(
                    "thread/queue/add",
                    {
                        "threadId": session,
                        "clientUserMessageId": operation,
                        "input": [{"type": "text", "text": arguments["prompt"]}],
                    },
                )
            except BaseException:
                self._unscope(operation)
                raise
            queue_id = result["queuedSubmission"]["id"]
            if operation in self.queue_operations:
                self.queue_operations[operation] = queue_id
            active_turn = next(
                (key for key, value in self.turn_operations.items() if value == operation), None
            )
            return {
                "session_id": session,
                "execution_id": active_turn,
                "queued_submission_id": queue_id,
                "status": "running"
                if active_turn
                else ("queued" if operation in self.queue_operations else "unknown"),
                "pending": True,
                "admitted": True,
                # Persisted with this receipt so a restarted connector still restores.
                **(
                    {
                        "scope_settings": "thread",
                        "scope_race": SCOPE_RACE,
                        "thread_settings_before": self.original_settings[session],
                    }
                    if operation in self.scopes
                    else {}
                ),
            }
        if action == "steer":
            expected = execution or arguments.get("expected_execution_id")
            if not expected:
                raise DriverError("missing_execution", "Steer requires the original active turn ID")
            await self.rpc.call(
                "turn/steer",
                {
                    "threadId": session,
                    "expectedTurnId": expected,
                    "input": [{"type": "text", "text": arguments["prompt"]}],
                },
            )
            return {
                "session_id": session,
                "execution_id": expected,
                "status": "running",
                "pending": True,
                "admitted": True,
            }
        if action == "cancel":
            queued = (
                None
                if execution
                else (
                    arguments.get("queued_submission_id")
                    or self.queue_operations.get(arguments.get("operation_id", ""))
                )
            )
            if queued:
                await self.rpc.call(
                    "thread/queue/delete",
                    {
                        "threadId": session,
                        "queuedSubmissionId": queued,
                    },
                )
                original_operation = arguments.get("operation_id") or next(
                    (key for key, value in self.queue_operations.items() if value == queued),
                    None,
                )
                if original_operation:
                    self.queue_operations.pop(original_operation, None)
                    self.queue_sessions.pop(original_operation, None)
                    self._unscope(original_operation)
                    await self.emit(
                        "completed",
                        {
                            "session_id": session,
                            "operation_id": original_operation,
                            "payload": {
                                "status": "cancelled",
                                "pending": False,
                                "admitted": True,
                                "queued_submission_id": queued,
                            },
                        },
                    )
                return {
                    "session_id": session,
                    "status": "cancelled",
                    "pending": False,
                    "admitted": True,
                }
            if not execution:
                raise DriverError("missing_execution", "Cancel requires the original execution ID")
            await self.rpc.call("turn/interrupt", {"threadId": session, "turnId": execution})
            return {
                "session_id": session,
                "execution_id": execution,
                "status": "running",
                "pending": True,
                "cancel_requested": True,
                "admitted": True,
            }
        if action == "respond":
            if arguments.get("connection_id") != self.rpc.connection_id:
                raise DriverError(
                    "request_resolved", "Original native connection is no longer current"
                )
            key = str(arguments["permission_id"])
            request = self.requests.get(key)
            if request is None:
                raise DriverError(
                    "request_resolved", "Native request is absent or already resolved"
                )
            params = request.get("params", {})
            if params.get("threadId") != session or (
                execution and params.get("turnId") != execution
            ):
                raise DriverError(
                    "request_mismatch", "Response does not target the original request"
                )
            if params.get("turnId") not in self.turn_operations:
                # Not a turn Yuki started: it waits for its own user in Codex.
                raise DriverError("not_owner", "This request belongs to a turn Yuki did not start")
            response = arguments.get("response")
            if response is None and arguments.get("option_id"):
                response = {"decision": arguments["option_id"]}
            if response is None:
                raise DriverError(
                    "response_required", "Provide the original native response object"
                )
            if "decision" in response and response["decision"] not in ONE_TIME_DECISIONS:
                offered = params.get("availableDecisions") or ONE_TIME_DECISIONS
                raise DriverError(
                    "one_time_only",
                    "Choose a decision for this one request",
                    {"options": [item for item in offered if item in ONE_TIME_DECISIONS]},
                )
            self.requests.pop(key, None)
            await self.rpc.send({"id": request["id"], "result": response})
            return {
                "session_id": session,
                "execution_id": execution,
                "status": "response_submitted",
                "pending": False,
                "response_submitted": True,
                "admitted": True,
            }
        raise DriverError("unsupported_action", f"Codex does not support {action}")
