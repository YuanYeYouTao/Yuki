"""Official app-server RPC against the existing owner, never another Codex runtime."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncIterator
from typing import Any, ClassVar, cast
from uuid import uuid4

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from .. import __version__
from ..config import Instance
from . import DriverError, Emit


class CodexRPC:
    def __init__(self, spec: Instance, notify: Any) -> None:
        self.spec, self.notify = spec, notify
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
            self.ws = await connect(self.spec.endpoint, max_size=None, proxy=None)
        else:
            # socketpair is private to this process. No listening TCP endpoint is
            # exposed. websockets owns the handshake, masking, control frames,
            # fragmentation and close; official proxy alone owns daemon transport.
            client, relay = socket.socketpair()
            client.setblocking(False)
            relay.setblocking(False)
            try:
                proxy_args = ["app-server", "proxy"]
                if self.spec.socket:
                    proxy_args.extend(["--sock", self.spec.socket])
                self.proxy = await asyncio.create_subprocess_exec(
                    self.spec.command,
                    *self.spec.args,
                    *proxy_args,
                    env=self.spec.environment(),
                    cwd=self.spec.cwd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                incoming, self.pipe = await asyncio.open_connection(sock=relay)
                assert self.proxy.stdin is not None and self.proxy.stdout is not None
                self.relays = [
                    asyncio.create_task(self._copy(incoming, self.proxy.stdin)),
                    asyncio.create_task(self._copy(self.proxy.stdout, self.pipe)),
                ]
                self.ws = await connect("ws://localhost/", sock=client, max_size=None, proxy=None)
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
    return {"active": "running", "idle": "completed", "systemError": "failed"}.get(
        str(kind), "unknown"
    )


def turn_status(turn: dict[str, Any]) -> str:
    return {
        "inProgress": "running",
        "completed": "completed",
        "failed": "failed",
        "interrupted": "cancelled",
    }.get(turn.get("status", ""), "unknown")


def item_view(item: dict[str, Any]) -> dict[str, Any] | None:
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
    if fields is None:
        return None
    result = {key: item[key] for key in fields if key in item}
    if kind == "fileChange":
        result["changes"] = [
            {key: change[key] for key in ("path", "kind") if key in change}
            for change in item.get("changes", [])
        ]
    return result


def turn_view(turn: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: turn[key]
        for key in ("id", "status", "error", "startedAt", "completedAt")
        if key in turn
    }
    result["items"] = [
        view for item in turn.get("items", []) if (view := item_view(item)) is not None
    ]
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
    ]

    def __init__(self, spec: Instance, emit: Emit) -> None:
        self.spec, self.emit = spec, emit
        self.rpc = CodexRPC(spec, self._notification)
        self.watches: dict[str, str] = {}  # original watch operation -> native session
        self.watch_turns: dict[str, str | None] = {}
        self.turn_operations: dict[str, str] = {}
        self.turn_sessions: dict[str, str] = {}
        self.queue_operations: dict[str, str] = {}
        self.pending_prompts: dict[str, str] = {}
        self.buffered: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.requests: dict[str, dict[str, Any]] = {}
        self.queue_sessions: dict[str, str] = {}
        self.owner: asyncio.Task[None] | None = None
        self.ready: asyncio.Future[None] | None = None
        self.recoveries: dict[str, asyncio.Task[None]] = {}

    async def start(self) -> None:
        self.ready = asyncio.get_running_loop().create_future()
        self.owner = asyncio.create_task(self._serve())
        await self.ready

    async def _serve(self) -> None:
        assert self.ready is not None
        while True:
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
                await self.emit(
                    "connection", {"status": "unavailable", "error_type": type(exc).__name__}
                )
            finally:
                await self.rpc.stop()
            await asyncio.sleep(1)
            self.rpc = CodexRPC(self.spec, self._notification)

    async def stop(self) -> None:
        self.rpc.stopping = True
        recoveries = list(self.recoveries.values())
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
                        return
            elif command["action"] == "queue":
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

    async def _notification(self, message: dict[str, Any]) -> None:
        method = message.get("method", "")
        params = message.get("params", {})
        session = params.get("threadId")
        turn = params.get("turnId") or params.get("turn", {}).get("id")
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
            if client_id in self.queue_operations or client_id == self.pending_prompts.get(session):
                self.turn_operations[turn] = client_id
                self.turn_sessions[turn] = session
                self.queue_operations.pop(client_id, None)
                self.queue_sessions.pop(client_id, None)
                for earlier in self.buffered.pop((session, turn), []):
                    await self._notification(earlier)
        if turn in self.turn_operations:
            operations.add(self.turn_operations[turn])
        if not operations:
            if (
                session in self.pending_prompts
                and turn
                and (
                    request
                    or method
                    in {
                        "turn/started",
                        "turn/completed",
                        "item/completed",
                        "turn/plan/updated",
                    }
                )
            ):
                self.buffered.setdefault((session, turn), []).append(message)
            return  # unobserved native sessions never enter the remote event stream
        kind = "progress"
        payload: dict[str, Any] = {"native_method": method, "admitted": True}
        if request:
            key = str(message["id"])
            self.requests[key] = message
            kind = "question" if "UserInput" in method or "elicitation" in method else "permission"
            payload.update(
                permission_id=key,
                native_request_id=message["id"],
                native_request=params,
                connection_id=self.rpc.connection_id,
            )
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
            for operation in operations:
                if self.pending_prompts.get(session) == operation:
                    self.pending_prompts.pop(session)
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
                    }
                    for item in result.get("data", [])
                ],
                "next_cursor": result.get("nextCursor"),
            }
        if action == "detach":
            detached = arguments["operation_id"]
            recovery = self.recoveries.pop(detached, None)
            if recovery is not None:
                recovery.cancel()
                await asyncio.gather(recovery, return_exceptions=True)
            self.watches.pop(detached, None)
            self.watch_turns.pop(detached, None)
            self.queue_operations.pop(detached, None)
            self.queue_sessions.pop(detached, None)
            for turn_id, owner in list(self.turn_operations.items()):
                if owner == detached:
                    self.turn_operations.pop(turn_id)
                    self.turn_sessions.pop(turn_id, None)
            for session_id, owner in list(self.pending_prompts.items()):
                if owner == detached:
                    self.pending_prompts.pop(session_id)
            return {
                "status": "completed",
                "session_id": session,
                "detached": True,
                "admitted": True,
            }
        if action == "get":
            original = (
                await self.rpc.call("thread/read", {"threadId": session, "includeTurns": False})
            )["thread"]
            selected = (
                await self._find_turn(session, execution)
                if execution or native_status(original) == "running"
                else None
            )
            state = (
                turn_status(selected)
                if selected
                else ("unknown" if execution else native_status(original))
            )
            if arguments.get("include_history"):
                original["turns"] = [
                    turn_view(turn) async for turn in self._turns(session, items="full")
                ]
            result = {
                "session_id": session,
                "execution_id": selected["id"] if selected else execution,
                "status": state,
                "native": original,
                "pending": state in {"running", "unknown"},
            }
            if execution and selected is None:
                result["reason"] = "execution_not_found"
            return result
        if action == "watch":
            if not await self._loaded(session):
                raise DriverError(
                    "not_live", "Session is stored but not loaded in this native owner"
                )
            session_scope = command.get("observation_scope") == "session"
            self.watches[operation] = session
            self.watch_turns[operation] = None if session_scope else execution or ""
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
        if action == "prompt":
            if session:
                original = (
                    await self.rpc.call(
                        "thread/read",
                        {
                            "threadId": session,
                            "includeTurns": False,
                        },
                    )
                )["thread"]
                if native_status(original) == "running":
                    raise DriverError("conflict", "Native session is active; choose steer or queue")
                await self.rpc.call("thread/resume", {"threadId": session, "excludeTurns": True})
            else:
                session = (
                    await self.rpc.call(
                        "thread/start",
                        {
                            "cwd": arguments.get("cwd") or self.spec.cwd,
                        },
                    )
                )["thread"]["id"]
            self.pending_prompts[session] = operation
            try:
                result = await self.rpc.call(
                    "turn/start",
                    {
                        "threadId": session,
                        "clientUserMessageId": operation,
                        "input": [{"type": "text", "text": arguments["prompt"]}],
                    },
                )
                execution = result["turn"]["id"]
                if self.pending_prompts.get(session) == operation:
                    self.turn_operations[execution] = operation
                    self.turn_sessions[execution] = session
                    for earlier in self.buffered.pop((session, execution), []):
                        await self._notification(earlier)
            finally:
                self.pending_prompts.pop(session, None)
                for candidate in [key for key in self.buffered if key[0] == session]:
                    self.buffered.pop(candidate)
            return {
                "session_id": session,
                "execution_id": execution,
                "status": "running",
                "pending": True,
                "admitted": True,
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
        if action == "queue":
            self.queue_operations[operation] = ""
            self.queue_sessions[operation] = session
            result = await self.rpc.call(
                "thread/queue/add",
                {
                    "threadId": session,
                    "clientUserMessageId": operation,
                    "input": [{"type": "text", "text": arguments["prompt"]}],
                },
            )
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
            response = arguments.get("response")
            if response is None and arguments.get("option_id"):
                response = {"decision": arguments["option_id"]}
            if response is None:
                raise DriverError(
                    "response_required", "Provide the original native response object"
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
