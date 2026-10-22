"""Official app-server RPC against the existing owner, never another Codex runtime."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from typing import Any, ClassVar
from uuid import uuid4

from websockets.asyncio.client import ClientConnection, connect

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

    async def start(self) -> None:
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
                await self.stop()
                raise
        self.reader = asyncio.create_task(self._receive())
        await self.call(
            "initialize",
            {
                "clientInfo": {"name": "yuki_agent_connector", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
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
        await self.ws.send(json.dumps(message, ensure_ascii=False))

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
        try:
            async for raw in self.ws:
                message = json.loads(raw)
                future = self.pending.get(str(message.get("id")))
                if future is not None and "method" not in message:
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
            await self.notify(
                {
                    "method": "connector/ownerDisconnected",
                    "params": {
                        "error_type": type(exc).__name__,
                        "reason": "native_event_stream_lost",
                    },
                }
            )
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

    async def stop(self) -> None:
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


def item_view(item: dict[str, Any]) -> dict[str, Any] | None:
    kind = item.get("type")
    fields = {
        "agentMessage": ("id", "type", "text", "phase"),
        "userMessage": ("id", "type", "clientId", "content"),
        "commandExecution": ("id", "type", "command", "cwd", "status", "exitCode", "durationMs"),
        "fileChange": ("id", "type", "status"),
        "mcpToolCall": ("id", "type", "server", "tool", "status"),
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

    async def start(self) -> None:
        await self.rpc.start()

    async def stop(self) -> None:
        await self.rpc.stop()

    async def recover(self, command: dict[str, Any], result: dict[str, Any]) -> None:
        session, execution = result["session_id"], result.get("execution_id")
        operation = command["id"]
        try:
            if command["action"] == "watch" and not execution:
                raise DriverError(
                    "observation_gap", "Original first execution cannot be proven after restart"
                )
            thread = (
                await self.rpc.call(
                    "thread/read",
                    {
                        "threadId": session,
                        "includeTurns": True,
                    },
                )
            )["thread"]
            original = next(
                (turn for turn in thread.get("turns", []) if turn["id"] == execution), None
            )
            state = "unknown"
            if command["action"] == "queue" and not execution:
                matches = [
                    turn
                    for turn in thread.get("turns", [])
                    if any(
                        item.get("type") == "userMessage" and item.get("clientId") == operation
                        for item in turn.get("items", [])
                    )
                ]
                if len(matches) == 1:
                    original = matches[0]
                    execution = original["id"]
                elif not matches:
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
                            ),
                            None,
                        )
                        if queued:
                            self.queue_operations[operation] = queued["id"]
                            state = "queued"
                            break
                        cursor = page.get("nextCursor")
                        if not cursor:
                            break
            if original:
                execution = original["id"]
                if command["action"] == "watch":
                    self.watches[operation] = session
                    self.watch_turns[operation] = execution
                else:
                    self.turn_operations[execution] = operation
                    self.turn_sessions[execution] = session
                state = {
                    "inProgress": "running",
                    "completed": "completed",
                    "failed": "failed",
                    "interrupted": "cancelled",
                }.get(original["status"], "unknown")
            if original and original["status"] in {"completed", "failed", "interrupted"}:
                await self._notification(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": session,
                            "turn": original,
                        },
                    }
                )
                return
            loaded = await self.rpc.call("thread/loaded/list", {})
            live = session in loaded.get("data", [])
            if live and state in {"running", "queued"}:
                await self.rpc.call("thread/resume", {"threadId": session, "excludeTurns": True})
            elif state == "running":
                state = "unknown"
            await self.emit(
                "progress",
                {
                    "session_id": session,
                    "execution_id": execution,
                    "operation_id": operation,
                    "payload": {
                        "status": state,
                        "reason": "connector_restarted_native_snapshot_restored",
                        "stream_gap": True,
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
                    "payload": {
                        "status": "unknown",
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
            for operation, session_id in self.watches.items():
                await self.emit(
                    "progress",
                    {
                        "session_id": session_id,
                        "operation_id": operation,
                        "payload": {"status": "unknown", **params},
                    },
                )
            for turn_id, operation in self.turn_operations.items():
                await self.emit(
                    "progress",
                    {
                        "execution_id": turn_id,
                        "session_id": self.turn_sessions.get(turn_id),
                        "operation_id": operation,
                        "payload": {"status": "unknown", **params},
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
            if sid == session and self.watch_turns.get(op) in {None, turn}
        }
        if turn:
            for operation in operations:
                self.watch_turns[operation] = turn
        item = params.get("item", {})
        if item.get("type") == "userMessage" and item.get("clientId"):
            client_id = item["clientId"]
            if client_id in self.queue_operations or client_id == self.pending_prompts.get(session):
                self.turn_operations[turn] = client_id
                self.turn_sessions[turn] = session
                self.queue_operations.pop(client_id, None)
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
                        "turn/diff/updated",
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
                permission_id=key, native_request_id=message["id"], native_request=params
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
            if item.get("type") not in {
                "agentMessage",
                "commandExecution",
                "fileChange",
                "mcpToolCall",
                "webSearch",
                "plan",
            }:
                return
            payload.update(item=item_view(item))
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
            "turn/diff/updated",
            "serverRequest/resolved",
        }:
            payload["native"] = params
            if "diff" in params:
                payload["diff"] = params["diff"]
        elif method == "connector/ownerDisconnected":
            payload.update(params)
        else:
            return  # deltas and duplicate status are not durable milestones
        for operation in operations:
            await self.emit(
                kind,
                {
                    "session_id": session,
                    "execution_id": turn,
                    "operation_id": operation,
                    "payload": payload,
                },
            )
        if method == "turn/completed":
            self.turn_operations.pop(turn, None)
            self.turn_sessions.pop(turn, None)
            for operation in operations:
                if self.watch_turns.get(operation) == turn:
                    self.watches.pop(operation, None)
                    self.watch_turns.pop(operation, None)

    async def execute(self, command: dict[str, Any]) -> dict[str, Any]:
        action, arguments = command["action"], command.get("arguments", {})
        session, execution = command.get("session_id"), command.get("execution_id")
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
            self.watches.pop(arguments["operation_id"], None)
            self.watch_turns.pop(arguments["operation_id"], None)
            return {
                "status": "completed",
                "session_id": session,
                "detached": True,
                "admitted": True,
            }
        if action == "get":
            original = (
                await self.rpc.call(
                    "thread/read",
                    {
                        "threadId": session,
                        "includeTurns": True,
                    },
                )
            )["thread"]
            turns = original.get("turns", [])
            selected = (
                next((item for item in turns if item["id"] == execution), None)
                if execution
                else next(
                    (item for item in reversed(turns) if item.get("status") == "inProgress"),
                    None,
                )
            )
            state = native_status(original)
            if selected:
                state = {
                    "inProgress": "running",
                    "completed": "completed",
                    "failed": "failed",
                    "interrupted": "cancelled",
                }.get(selected.get("status"), "unknown")
            if not arguments.get("include_history"):
                original.pop("turns", None)
            else:
                original["turns"] = [turn_view(turn) for turn in turns]
            result = {
                "session_id": session,
                "execution_id": selected["id"] if selected else execution,
                "status": state,
                "native": original,
                "pending": state == "running",
            }
            return result
        if action == "watch":
            # read does not load a thread. resume attaches this client to the
            # same daemon-owned thread; it does not create a new thread/turn.
            loaded = await self.rpc.call("thread/loaded/list", {})
            if session not in loaded.get("data", []):
                raise DriverError(
                    "not_live", "Session is stored but not loaded in this native owner"
                )
            assert isinstance(session, str)
            self.watches[operation] = session
            self.watch_turns[operation] = execution
            try:
                thread = (
                    await self.rpc.call(
                        "thread/resume",
                        {
                            "threadId": session,
                            "excludeTurns": False,
                        },
                    )
                )["thread"]
            except BaseException:
                self.watches.pop(operation, None)
                self.watch_turns.pop(operation, None)
                raise
            active = next(
                (
                    turn
                    for turn in reversed(thread.get("turns", []))
                    if turn.get("status") == "inProgress"
                ),
                None,
            )
            selected = (
                next((turn for turn in thread.get("turns", []) if turn["id"] == execution), None)
                if execution
                else active
            )
            if execution and selected is None:
                self.watches.pop(operation, None)
                self.watch_turns.pop(operation, None)
                raise DriverError(
                    "execution_not_found", "Original native execution is not in this session"
                )
            terminal = selected is not None and selected.get("status") in {
                "completed",
                "failed",
                "interrupted",
            }
            if terminal:
                self.watches.pop(operation, None)
                self.watch_turns.pop(operation, None)
            elif selected:
                self.watch_turns[operation] = selected["id"]
            return {
                "session_id": session,
                "execution_id": selected["id"] if selected else None,
                "status": (
                    {"completed": "completed", "failed": "failed", "interrupted": "cancelled"}.get(
                        selected.get("status"), "running"
                    )
                    if selected
                    else "unknown"
                ),
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
