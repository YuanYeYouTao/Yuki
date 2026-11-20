"""ACP on the actual host, with capabilities negotiated from the original agent."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from collections import deque
from collections.abc import Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from acp import Client
from acp.exceptions import RequestError
from acp.schema import (
    AllowedOutcome,
    ClientCapabilities,
    CreateTerminalResponse,
    DeniedOutcome,
    FileSystemCapabilities,
    Implementation,
    KillTerminalResponse,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    TerminalExitStatus,
    TerminalOutputResponse,
    TextContentBlock,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)
from acp.stdio import spawn_agent_process

from .. import __version__
from ..config import Instance
from ..journal import followup_id
from ..scope import Scope
from . import DriverError, Emit

# Agent modes that ask before side effects, strictest first (§15 E5). A mode that
# writes the whole working directory without asking only fits a scope covering it.
ASKING_MODES = ("default", "read-only")
WORKSPACE_MODE = "workspace-write"
# Only turn content starts an agent-initiated turn; mode/command/config updates
# (for example after session/set_mode) and usage reports do not.
TURN_CONTENT = {"agent_message_chunk", "tool_call", "tool_call_update", "plan"}
ONE_TIME_KINDS = {"allow_once", "reject_once"}


def dump(value: Any) -> Any:
    return value.model_dump(mode="json", by_alias=True, exclude_none=True)


def modes(response: Any) -> list[str]:
    state = response.modes if response is not None else None
    return [mode.id for mode in state.available_modes] if state is not None else []


@dataclass
class Session:
    id: str
    cwd: str
    status: str = "unknown"
    operation: str | None = None
    execution: str | None = None
    task: asyncio.Task[None] | None = None
    watches: dict[str, str | None] = field(default_factory=dict)
    text: list[str] = field(default_factory=list)
    dispatched: asyncio.Event = field(default_factory=asyncio.Event)
    admitted: asyncio.Future[bool] | None = None
    modes: list[str] = field(default_factory=list)
    scope_mode: str | None = None
    # Yuki's prompt is in flight until its response arrives (its JSON-RPC ID is
    # known only after the request was written).
    prompting: bool = False
    prompt_request: Any = None
    # Whether Yuki's prompt was in flight when each update/request arrived, in
    # arrival order: the SDK runs their handlers as tasks, after later frames.
    arrivals: deque[bool] = field(default_factory=deque)
    # The latest Yuki operation that prompted this session; agent-initiated turns
    # belong to it until it is detached (§4.8, §15 E5).
    owner_operation: str | None = None
    followup: str | None = None
    release_after_turn: bool = False


@dataclass
class Waiting:
    """A session/request_permission this connector has not answered yet."""

    session_id: str
    options: list[Any]
    answer: asyncio.Future[Any]
    tool_kind: str | None = None
    recipients: set[str] = field(default_factory=set)
    # No operation received it in full: metadata left this computer, deny only.
    unowned: bool = False


@dataclass
class Terminal:
    session_id: str
    process: asyncio.subprocess.Process
    drain: asyncio.Task[None] | None = None
    output: bytearray = field(default_factory=bytearray)
    truncated: bool = False


class ACPDriver:
    def __init__(self, spec: Instance, emit: Emit) -> None:
        self.spec, self.emit = spec, emit
        self.capabilities = [
            "get",
            "watch",
            "prompt",
            "respond",
            "cancel",
            "detach",
            "scope",
            "observe",
            "release",
        ]
        self.connection: Any = None
        self.native_capabilities: dict[str, Any] = {}
        self.sessions: dict[str, Session] = {}
        self.requests: dict[str, Waiting] = {}
        self.scopes: dict[str, Scope] = {}
        # Operations detached through this driver: never an owner again.
        self.detached: set[str] = set()
        # Observe watches: metadata only, never an answerer (§4.3).
        self.observers: set[str] = set()
        self.permission_refs: dict[tuple[str, str], Any] = {}
        self.responses: dict[str, asyncio.Future[None]] = {}
        self.terminals: dict[str, Terminal] = {}
        self.owner = str(uuid4())
        self.ready: asyncio.Future[None] | None = None
        self.stopped = asyncio.Event()
        self.worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.ready = asyncio.get_running_loop().create_future()
        self.worker = asyncio.create_task(self._serve())
        await self.ready

    async def _serve(self) -> None:
        assert self.ready is not None
        waiters: list[asyncio.Task[Any]] = []
        try:
            async with spawn_agent_process(
                cast(Client, self),
                self.spec.command,
                *self.spec.args,
                cwd=self.spec.cwd,
                env=self.spec.environment(),
                observers=[self.observe],
                transport_kwargs={"stderr": asyncio.subprocess.DEVNULL},
            ) as (connection, process):
                self.connection = connection
                initialized = await connection.initialize(
                    protocol_version=1,
                    client_capabilities=ClientCapabilities(
                        fs=FileSystemCapabilities(read_text_file=True, write_text_file=True),
                        terminal=True,
                    ),
                    client_info=Implementation(name="Yuki Agent Connector", version=__version__),
                )
                if initialized.protocol_version != 1:
                    raise DriverError(
                        "native_protocol_unsupported",
                        "ACP agent did not negotiate protocol version 1",
                    )
                self.native_capabilities = dump(initialized.agent_capabilities)
                if (
                    self.native_capabilities.get("sessionCapabilities", {}).get("list") is not None
                    and "list" not in self.capabilities
                ):
                    self.capabilities.insert(0, "list")
                self.ready.set_result(None)
                stopped = asyncio.create_task(self.stopped.wait())
                exited = asyncio.create_task(process.wait())
                waiters = [stopped, exited]
                done, _ = await asyncio.wait((stopped, exited), return_when=asyncio.FIRST_COMPLETED)
                stopped.cancel()
                exited.cancel()
                if exited in done and not self.stopped.is_set():
                    await self.emit(
                        "connection",
                        {"status": "unavailable", "reason": "native_acp_process_exited"},
                    )
                    for session in self.sessions.values():
                        if session.status not in {"completed", "failed", "cancelled"}:
                            session.status = "unknown"
                        await self._publish(
                            session,
                            "progress",
                            {
                                "status": "unknown",
                                "reason": "native_acp_process_exited",
                            },
                        )
        except Exception as exc:
            if not self.ready.done():
                self.ready.set_exception(exc)
            else:
                await self.emit(
                    "connection", {"status": "unavailable", "reason": "native_acp_connection_lost"}
                )
                for session in self.sessions.values():
                    if session.status not in {"completed", "failed", "cancelled"}:
                        session.status = "unknown"
                    await self._publish(
                        session,
                        "progress",
                        {
                            "status": "unknown",
                            "reason": "native_acp_connection_lost",
                            "error_type": type(exc).__name__,
                        },
                    )
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            self.connection = None
            self.permission_refs.clear()
            for waiting in self.requests.values():
                if not waiting.answer.done():
                    waiting.answer.cancel()
            for future in self.responses.values():
                if not future.done():
                    future.set_exception(
                        DriverError(
                            "unknown",
                            "ACP connection lost before formal response was written",
                            uncertain=True,
                        )
                    )

    async def stop(self) -> None:
        self.stopped.set()
        if self.worker:
            if self.ready is None or not self.ready.done() or self.ready.cancelled():
                self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        # Only subprocesses this ACP driver created. Network/detach never calls stop.
        for terminal_id, terminal in list(self.terminals.items()):
            await self.release_terminal(terminal.session_id, terminal_id)

    async def recover(self, command: dict[str, Any], result: dict[str, Any]) -> None:
        await self.emit(
            "progress",
            {
                "session_id": result.get("session_id") or command.get("session_id"),
                "execution_id": result.get("execution_id") or command.get("execution_id"),
                "operation_id": command["id"],
                "snapshot_sequence": result.get("state_sequence", 0),
                "payload": {
                    "status": "unknown",
                    "reason": "previous_acp_owner_not_attached",
                    "stream_gap": True,
                },
            },
        )

    def on_connect(self, connection: Any) -> None:
        self.connection = connection

    def observe(self, event: Any) -> None:
        if event.direction == "incoming" and event.message.get("method") in {
            "session/update",
            "session/request_permission",
        }:
            arrived = self.sessions.get(event.message["params"]["sessionId"])
            if arrived is not None:
                arrived.arrivals.append(arrived.prompting)
        if (
            event.direction == "incoming"
            and "id" in event.message
            and "method" not in event.message
        ):
            # The prompt's response: later updates belong to an agent-initiated turn.
            for session in self.sessions.values():
                if session.prompting and session.prompt_request == event.message["id"]:
                    session.prompting = False
        if (
            event.direction == "outgoing"
            and "id" in event.message
            and "method" not in event.message
        ):
            pending = self.responses.get(str(event.message["id"]))
            if pending and not pending.done():
                if "error" in event.message:
                    pending.set_exception(
                        DriverError("native_response_error", "ACP response was rejected by the SDK")
                    )
                else:
                    pending.set_result(None)
        if (
            event.direction == "incoming"
            and event.message.get("method") == "session/request_permission"
        ):
            original = event.message
            params = original["params"]
            self.permission_refs[(params["sessionId"], params["toolCall"]["toolCallId"])] = (
                original["id"]
            )
        if event.direction == "outgoing" and event.message.get("method") == "session/prompt":
            original = event.message
            session = self.sessions[original["params"]["sessionId"]]
            # ACP has a request ID, not a persisted native turn ID. Qualify it
            # with this actual protocol owner so reconnect cannot alias a new RPC.
            session.execution = f"{self.owner}:{original['id']}"
            session.prompt_request = original["id"]
            session.dispatched.set()

    async def _publish(
        self, session: Session, kind: str, payload: dict[str, Any], *, turn: str = "prompt"
    ) -> set[str]:
        """Send to the turn's receivers; return who may answer it.

        ``turn`` is ``prompt`` (Yuki's prompt in flight), ``followup`` (an
        agent-initiated turn of the held owner) or ``unowned`` (watches only).
        """
        if payload.get("status") != "unknown":
            payload.setdefault("admitted", True)
        execution = session.followup if turn == "followup" else session.execution
        operations = {
            operation
            for operation, bound in session.watches.items()
            if bound in {None, "", execution}
        }
        if turn == "prompt" and session.operation:
            operations.add(session.operation)
        request = kind == "permission"
        answerers = set()
        if turn == "followup" and session.owner_operation and session.followup:
            await self.emit(
                kind,
                {
                    "session_id": session.id,
                    "execution_id": session.followup,
                    "operation_id": None,
                    "owner_operation_id": session.owner_operation,
                    "followup_execution": session.followup,
                    "payload": payload,
                },
            )
            answerers.add(followup_id(session.owner_operation, session.followup))
        for operation in operations:
            if operation in session.watches and session.watches[operation] == "":
                session.watches[operation] = session.execution
            continuous = operation in session.watches and session.watches[operation] is None
            event_kind, event_payload = kind, payload
            if kind == "completed" and continuous:
                event_kind = "progress"
                event_payload = {
                    **payload,
                    "execution_status": payload["status"],
                    "status": "idle",
                    "pending": True,
                }
            if request and operation in self.observers:
                event_payload = {
                    "native_request_id": payload["native_request_id"],
                    "tool": payload["tool"].get("kind"),
                    "observed": True,
                }
            elif request:
                answerers.add(operation)
            await self.emit(
                event_kind,
                {
                    "session_id": session.id,
                    "execution_id": execution,
                    "operation_id": operation,
                    "payload": event_payload,
                },
            )
        return answerers

    async def _turn(self, session: Session, prompting: bool, starts: bool = True) -> str:
        """Which turn a session update or request belongs to."""
        if prompting:
            return "prompt"
        if session.followup is not None:
            return "followup"
        if session.owner_operation is None or not starts:
            return "unowned"
        if session.followup is None:
            session.followup = f"{self.owner}:self:{uuid4()}"
            await self._publish(
                session,
                "progress",
                {"status": "running", "pending": True, "self_started": True},
                turn="followup",
            )
        return "followup"

    async def _end_followup(self, session: Session) -> None:
        if session.followup is None:
            return
        # ACP reports no end of an agent-initiated turn. Yuki ends it here (a new
        # prompt or release); it is not claimed to have completed.
        await self._publish(
            session,
            "completed",
            {"status": "cancelled", "pending": False, "reason": "native_turn_end_not_reported"},
            turn="followup",
        )
        session.followup = None

    def _scope_of(self, session: Session, turn: str) -> Scope | None:
        owner = {"prompt": session.operation, "followup": session.owner_operation}.get(turn)
        return self.scopes.get(owner) if owner else None

    @staticmethod
    def _decide(session: Session, tool_call: Any, options: list[Any], scope: Scope) -> str | None:
        """The agent-offered one-time allow option, only when the scope covers the call."""
        allow = next((item.option_id for item in options if item.kind == "allow_once"), None)
        if allow is None or scope.full:
            return allow
        call = dump(tool_call)
        kind, raw = call.get("kind"), call.get("rawInput") or {}
        cwd = str(raw.get("cwd") or session.cwd)
        if kind in {"edit", "delete", "move"}:
            paths = [item["path"] for item in call.get("locations") or []]
            if not paths and isinstance(raw.get("file_path"), str):
                paths = [raw["file_path"]]
            inside = bool(paths) and all(scope.writes(path, cwd) for path in paths)
        elif kind == "execute":
            command = raw.get("command")
            inside = isinstance(command, (str, list)) and scope.runs(command)
        elif kind == "fetch":
            inside = scope.network
        else:
            inside = False
        return allow if inside else None

    async def _unowned(self, identity: str, waiting: Waiting, session: Session) -> None:
        waiting.unowned = True
        await self.emit(
            "permission",
            {
                "session_id": session.id,
                "execution_id": session.execution,
                "operation_id": None,
                "payload": {
                    "permission_id": identity,
                    "native_request_id": identity,
                    "connection_id": self.owner,
                    "tool_kind": waiting.tool_kind,
                    "options": ["deny"],
                    "unowned": True,
                    "status": "requires_action",
                    "pending": True,
                },
            },
        )

    def _session(self, session_id: str) -> Session:
        try:
            return self.sessions[session_id]
        except KeyError:
            raise DriverError(
                "session_not_loaded", "Session is not attached to this ACP owner"
            ) from None

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        session = self.sessions.get(session_id)
        if session is None:
            return  # session/load history is not a newly observed progress event
        if not session.arrivals:
            return  # Arrived before this owner knew the session: load history.
        prompting = session.arrivals.popleft()
        payload = dump(update)
        if session.admitted and not session.admitted.done():
            session.admitted.set_result(True)
        kind = payload.get("sessionUpdate")
        turn = await self._turn(session, prompting, kind in TURN_CONTENT)
        if kind == "agent_message_chunk":
            content = payload.get("content", {})
            if content.get("type") == "text" and turn == "prompt":
                session.text.append(content["text"])
        elif kind in {"tool_call", "tool_call_update", "plan", "usage_update"}:
            await self._publish(session, "progress", {"native": payload}, turn=turn)

    async def request_permission(
        self,
        session_id: str,
        tool_call: Any,
        options: list[Any],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        session = self._session(session_id)
        if session.admitted and not session.admitted.done():
            session.admitted.set_result(True)
        original_request = self.permission_refs.pop((session_id, tool_call.tool_call_id))
        identity = str(original_request)
        prompting = session.arrivals.popleft() if session.arrivals else session.prompting
        turn = await self._turn(session, prompting)
        scope = self._scope_of(session, turn)
        allowed = self._decide(session, tool_call, options, scope) if scope else None
        if allowed is not None:
            # Inside the delegation's fixed scope (D2): the agent's own allow-once.
            await self._publish(
                session, "progress", {"permission_auto_approved": identity}, turn=turn
            )
            return RequestPermissionResponse(
                outcome=AllowedOutcome(outcome="selected", option_id=allowed)
            )
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        waiting = Waiting(session_id, options, future, dump(tool_call).get("kind"))
        self.requests[identity] = waiting
        payload: dict[str, Any] = {
            "permission_id": identity,
            "native_request_id": original_request,
            "connection_id": self.owner,
            "tool": dump(tool_call),
            "options": [dump(item) for item in options],
        }
        if any(item.option_id == "cancel" and item.kind == "reject_once" for item in options):
            # codex-acp: this reject ends the whole prompt, not just this call (§15 E5).
            payload["reject_ends_prompt"] = True
        try:
            waiting.recipients = await self._publish(session, "permission", payload, turn=turn)
            if not waiting.recipients:
                # The agent process is ours: nobody else can answer it (§15 E5).
                await self._unowned(identity, waiting, session)
            choice = await future
            return RequestPermissionResponse(
                outcome=(
                    DeniedOutcome(outcome="cancelled")
                    if choice is None
                    else AllowedOutcome(outcome="selected", option_id=choice)
                )
            )
        finally:
            self.requests.pop(identity, None)
            if waiting.unowned:
                await self.emit(
                    "progress",
                    {
                        "session_id": session.id,
                        "execution_id": session.execution,
                        "operation_id": None,
                        "payload": {
                            "permission_resolved": identity,
                            "outcome": "answered"
                            if future.done() and not future.cancelled()
                            else "withdrawn",
                            "pending": True,
                        },
                    },
                )

    async def _prompt(self, session: Session, prompt: str, operation_id: str) -> None:
        finished = False
        try:
            result = await self.connection.prompt(
                session_id=session.id,
                prompt=[TextContentBlock(type="text", text=prompt)],
            )
            if session.admitted and not session.admitted.done():
                session.admitted.set_result(True)
            session.status = "cancelled" if result.stop_reason == "cancelled" else "completed"
            await self._publish(
                session,
                "completed",
                {
                    "status": session.status,
                    "text": "".join(session.text),
                    "native": dump(result),
                },
            )
            session.watches = {
                key: value for key, value in session.watches.items() if value is None
            }
            finished = True
        except Exception as exc:
            session.status = "unknown"
            await self._publish(
                session,
                "progress",
                {
                    "status": "unknown",
                    "reason": "native_prompt_result_unknown",
                    "error_type": type(exc).__name__,
                },
            )
        finally:
            if session.admitted and not session.admitted.done():
                session.admitted.set_exception(
                    DriverError("unknown", "Native admission was not confirmed", uncertain=True)
                )
            session.text.clear()
            session.prompting = False
            if session.operation == operation_id:
                session.operation = None
            session.dispatched.set()
        if finished:
            await self.emit("work_finished", {"operation_id": operation_id})
        if session.release_after_turn and not self._held(session):
            await self._close(session)

    def _held(self, session: Session) -> bool:
        owner = session.owner_operation
        return bool(session.operation or (owner and owner not in self.detached) or session.watches)

    async def _close(self, session: Session) -> str | None:
        """End Yuki's hold on a session: close it, or end an agent with no other session."""
        await self._end_followup(session)
        if self.native_capabilities.get("sessionCapabilities", {}).get("close") is not None:
            self.sessions.pop(session.id, None)
            await self._native_request(self.connection.close_session(session_id=session.id))
            return "now"
        if len(self.sessions) == 1:
            self.sessions.pop(session.id, None)
            # Without session/close only ending the process lets another client load it.
            self.stopped.set()
            assert self.worker is not None
            await self.worker
            self.stopped = asyncio.Event()
            try:
                await self.start()
            except Exception as exc:
                # The session is free (its process ended); the instance is not.
                await self.emit(
                    "connection",
                    {
                        "status": "unavailable",
                        "reason": "native_acp_restart_failed",
                        "error_type": type(exc).__name__,
                    },
                )
            return "now"
        return None

    async def _native_request(self, request: Awaitable[Any]) -> Any:
        try:
            return await request
        except RequestError as exc:
            if exc.code == -32000:
                raise DriverError(
                    "native_login_required", "The ACP agent requires authentication"
                ) from exc
            raise DriverError(
                "native_session_error", "The ACP agent rejected the request", {"code": exc.code}
            ) from exc

    async def _set_scope_mode(self, session: Session, scope: Scope) -> None:
        """Leave the user's auto/guardian mode for one that asks (§15 E5)."""
        if session.scope_mode is not None:
            return
        candidates = [
            *(mode for mode in ASKING_MODES if mode in session.modes),
            *(
                [WORKSPACE_MODE]
                if WORKSPACE_MODE in session.modes and scope.covers(session.cwd)
                else []
            ),
        ]
        if not candidates:
            raise DriverError(
                "scope_unsupported",
                "This ACP agent offers no mode that asks before side effects",
                {"modes": session.modes},
            )
        mode = WORKSPACE_MODE if WORKSPACE_MODE in candidates else candidates[0]
        await self._native_request(
            self.connection.set_session_mode(session_id=session.id, mode_id=mode)
        )
        session.scope_mode = mode

    async def execute(self, command: dict[str, Any]) -> dict[str, Any]:
        action, arguments = command["action"], command.get("arguments", {})
        identity = command.get("session_id")
        if action == "detach":
            detached = arguments["operation_id"]
            self.detached.add(detached)
            self.observers.discard(detached)
            self.scopes.pop(detached, None)
            held = []
            for session in self.sessions.values():
                if (
                    detached in session.watches
                    or detached
                    in {
                        session.operation,
                        session.owner_operation,
                    }
                    or session.id == identity
                ):
                    held.append(session)
                session.watches.pop(detached, None)
                if session.operation == detached:
                    session.operation = None
                if session.owner_operation == detached:
                    session.owner_operation = session.followup = None
            for key, waiting in list(self.requests.items()):
                if waiting.unowned or waiting.answer.done():
                    continue
                waiting.recipients = {
                    item
                    for item in waiting.recipients
                    if item != detached and not item.startswith(f"followup:{detached}:")
                }
                if not waiting.recipients:
                    await self._unowned(key, waiting, self.sessions[waiting.session_id])
            if not arguments.get("release"):
                return {
                    "session_id": identity,
                    "status": "completed",
                    "detached": True,
                    "admitted": True,
                }
            closed: str | None = "now"
            for session in held:
                if self._held(session):
                    closed = None
                elif session.task and not session.task.done():
                    session.release_after_turn = True
                    closed = "after_turn" if closed == "now" else closed
                elif await self._close(session) is None:
                    closed = None
            return {
                "session_id": identity,
                "status": "detached",
                "detached": True,
                "pending": False,
                "admitted": True,
                "closed": closed,
            }
        if self.connection is None:
            raise DriverError("owner_disconnected", "Original ACP process is not connected")
        if action == "list":
            if action not in self.capabilities:
                raise DriverError("unsupported_action", "Agent does not advertise session/list")
            result = await self._native_request(
                self.connection.list_sessions(cursor=arguments.get("cursor"))
            )
            return {
                "sessions": [
                    {
                        "id": item.session_id,
                        "label": item.title,
                        "cwd": item.cwd,
                        "status": "unknown",
                    }
                    for item in result.sessions
                ],
                "next_cursor": result.next_cursor,
            }
        if action == "prompt":
            if not identity:
                cwd = arguments.get("cwd") or self.spec.cwd
                created = await self._native_request(
                    self.connection.new_session(cwd=cwd, mcp_servers=[])
                )
                identity = created.session_id
                self.sessions[identity] = Session(identity, cwd, modes=modes(created))
            elif identity not in self.sessions:
                if not arguments.get("resume"):
                    raise DriverError(
                        "explicit_resume_required", "Authorize loading this stored native session"
                    )
                # Recover original cwd from the native list; never replace it
                # with /workspace or assume the configured default is its cwd.
                if "list" not in self.capabilities:
                    raise DriverError(
                        "cwd_unknown", "Agent cannot reveal this stored session's cwd"
                    )
                cursor = None
                original = None
                while True:
                    catalog = await self.execute(
                        {**command, "action": "list", "arguments": {"cursor": cursor}}
                    )
                    original = next((s for s in catalog["sessions"] if s["id"] == identity), None)
                    cursor = catalog.get("next_cursor")
                    if original or not cursor:
                        break
                if not original:
                    raise DriverError(
                        "session_not_found", "Stored session absent from native directory"
                    )
                cwd = original["cwd"]
                capabilities = self.native_capabilities.get("sessionCapabilities", {})
                if capabilities.get("resume") is not None:
                    loaded = await self._native_request(
                        self.connection.resume_session(session_id=identity, cwd=cwd, mcp_servers=[])
                    )
                elif self.native_capabilities.get("loadSession"):
                    loaded = await self._native_request(
                        self.connection.load_session(session_id=identity, cwd=cwd, mcp_servers=[])
                    )
                else:
                    raise DriverError("unsupported_action", "Agent does not advertise load/resume")
                self.sessions[identity] = Session(identity, cwd, modes=modes(loaded))
            session = self._session(identity)
            if session.task and not session.task.done():
                raise DriverError("conflict", "Original ACP session has an active prompt")
            scope = Scope.parse(arguments.get("scope"), session.cwd)
            if scope is not None:
                if not scope.full:
                    # ACP has no standard full-access mode: a full scope keeps the
                    # agent's own mode and approves whatever it still asks.
                    await self._set_scope_mode(session, scope)
                self.scopes[command["id"]] = scope
            await self._end_followup(session)
            session.owner_operation = command["id"]
            session.operation, session.status = command["id"], "running"
            session.prompting = True
            session.admitted = asyncio.get_running_loop().create_future()
            session.dispatched.clear()
            session.task = asyncio.create_task(
                self._prompt(session, arguments["prompt"], command["id"])
            )
            await session.dispatched.wait()
            admitted = await session.admitted
            return {
                "session_id": identity,
                "execution_id": session.execution,
                "status": session.status,
                "pending": not session.task.done(),
                "admitted": admitted,
            }
        if action in {"steer", "queue"}:
            raise DriverError(
                "unsupported_action", "ACP agent has no negotiated precise steer/queue"
            )
        if not isinstance(identity, str):
            raise DriverError("missing_session", "This action requires the original session ID")
        session = self._session(identity)
        if action == "get":
            expected = command.get("execution_id")
            result = {
                "session_id": identity,
                "execution_id": session.execution,
                "status": session.status,
                "pending": bool(session.task and not session.task.done()),
                "query_scope": "execution" if expected else "session",
                "status_scope": "execution" if expected else "session",
                "history_available": False,
                "history_reason": "native_history_not_supported",
            }
            if expected and expected != session.execution:
                result.update(
                    execution_id=expected,
                    status="unknown",
                    pending=True,
                    reason="execution_not_attached",
                )
            return result
        if action == "watch":
            if arguments.get("observation") == "observe":
                self.observers.add(command["id"])
            active = bool(session.task and not session.task.done())
            expected = command.get("execution_id")
            if expected and expected != session.execution:
                raise DriverError(
                    "execution_mismatch", "Watch must target the original native prompt"
                )
            if active or not expected:
                session.watches[command["id"]] = (
                    None
                    if command.get("observation_scope") == "session"
                    else expected or session.execution or ""
                )
            return {
                "session_id": identity,
                "execution_id": session.execution if active or expected else None,
                "status": session.status
                if active or expected
                else "idle"
                if command.get("observation_scope") == "session"
                else "unknown",
                "pending": active or not expected,
                "admitted": True,
            }
        if action == "cancel":
            if command.get("execution_id") != session.execution:
                raise DriverError(
                    "execution_mismatch", "Control must target the original ACP prompt"
                )
            await self.connection.cancel(session_id=identity)
            return {
                "session_id": identity,
                "execution_id": session.execution,
                "status": "cancel_submitted",
                "pending": False,
                "cancel_requested": True,
                "cancel_submitted": True,
                "admitted": True,
            }
        if action == "respond":
            if arguments.get("connection_id") != self.owner:
                raise DriverError(
                    "request_resolved", "Original ACP connection is no longer current"
                )
            # The original request is the fact: this process, this request, pending.
            pending = self.requests.get(str(arguments["permission_id"]))
            if not pending or pending.session_id != identity or pending.answer.done():
                raise DriverError(
                    "request_resolved", "Original ACP permission is no longer pending"
                )
            waiting = pending
            choice = arguments.get("option_id")
            if "response" in arguments:
                formal = arguments["response"]["outcome"]
                choice = formal.get("optionId") if formal["outcome"] == "selected" else None
            kinds = {item.option_id: item.kind for item in waiting.options}
            if choice is not None and choice not in kinds:
                raise DriverError("invalid_option", "Answer must select an original native option")
            if choice is not None and kinds[choice] not in ONE_TIME_KINDS:
                # An answer applies to this one request (Q1); it never widens later ones.
                raise DriverError(
                    "one_time_only",
                    "Choose a one-time option",
                    {"options": [key for key, kind in kinds.items() if kind in ONE_TIME_KINDS]},
                )
            if waiting.unowned and choice is not None and not kinds[choice].startswith("reject"):
                raise DriverError(
                    "deny_only", "Only its metadata left this computer; it can only be denied"
                )
            key = str(arguments["permission_id"])
            delivered: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self.responses[key] = delivered
            try:
                waiting.answer.set_result(choice)
                await delivered
            finally:
                self.responses.pop(key, None)
            return {
                "session_id": identity,
                "execution_id": session.execution,
                "status": "response_submitted",
                "pending": False,
                "response_submitted": True,
                "admitted": True,
            }
        raise DriverError("unsupported_action", f"ACP does not support {action}")

    async def _refuse(self, session: Session, method: str) -> None:
        """A client callback outside the scope is not run (no agent asked first)."""
        await self._publish(
            session,
            "progress",
            {"scope_refused": method},
            turn=await self._turn(session, session.prompting),
        )
        raise RequestError.invalid_params({"reason": "outside_delegated_scope"})

    async def read_text_file(
        self,
        session_id: str,
        path: str,
        line: int | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> ReadTextFileResponse:
        self._session(session_id)
        content = await asyncio.to_thread(Path(path).read_text, encoding="utf-8")
        if line is not None or limit is not None:
            start = (line or 1) - 1
            content = "".join(
                content.splitlines(keepends=True)[start : None if limit is None else start + limit]
            )
        return ReadTextFileResponse(content=content)

    async def write_text_file(
        self,
        session_id: str,
        path: str,
        content: str,
        **kwargs: Any,
    ) -> WriteTextFileResponse:
        session = self._session(session_id)
        scope = self._scope_of(session, "prompt" if session.prompting else "followup")
        if scope is not None and not scope.writes(path, session.cwd):
            await self._refuse(session, "fs/write_text_file")
        await asyncio.to_thread(Path(path).write_text, content, encoding="utf-8")
        return WriteTextFileResponse()

    async def create_terminal(
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        env: list[Any] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> CreateTerminalResponse:
        session = self._session(session_id)
        scope = self._scope_of(session, "prompt" if session.prompting else "followup")
        if scope is not None and not scope.runs([command, *(args or [])]):
            await self._refuse(session, "terminal/create")
        process = await asyncio.create_subprocess_exec(
            command,
            *(args or []),
            cwd=cwd or session.cwd,
            env={**self.spec.environment(), **{item.name: item.value for item in (env or [])}},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
        terminal = Terminal(session_id, process)

        async def drain() -> None:
            assert process.stdout is not None
            while data := await process.stdout.read(65536):
                terminal.output.extend(data)
                if output_byte_limit is not None and len(terminal.output) > output_byte_limit:
                    terminal.truncated = True
                    if output_byte_limit:
                        del terminal.output[:-output_byte_limit]
                    else:
                        terminal.output.clear()

        terminal.drain = asyncio.create_task(drain())
        identity = str(uuid4())
        self.terminals[identity] = terminal
        return CreateTerminalResponse(terminal_id=identity)

    def _terminal(self, session_id: str, terminal_id: str) -> Terminal:
        self._session(session_id)
        terminal = self.terminals[terminal_id]
        if terminal.session_id != session_id:
            raise DriverError("terminal_mismatch", "Terminal belongs to another original session")
        return terminal

    async def terminal_output(
        self,
        session_id: str,
        terminal_id: str,
        **kwargs: Any,
    ) -> TerminalOutputResponse:
        terminal = self._terminal(session_id, terminal_id)
        code = terminal.process.returncode
        return TerminalOutputResponse(
            output=terminal.output.decode("utf-8", errors="replace"),
            truncated=terminal.truncated,
            exit_status=None
            if code is None
            else TerminalExitStatus(
                exit_code=code if code >= 0 else None,
                signal=str(-code) if code < 0 else None,
            ),
        )

    async def wait_for_terminal_exit(
        self,
        session_id: str,
        terminal_id: str,
        **kwargs: Any,
    ) -> WaitForTerminalExitResponse:
        terminal = self._terminal(session_id, terminal_id)
        code = await asyncio.shield(terminal.process.wait())
        assert terminal.drain is not None
        await asyncio.shield(terminal.drain)
        return WaitForTerminalExitResponse(
            exit_code=code if code >= 0 else None,
            signal=str(-code) if code < 0 else None,
        )

    async def kill_terminal(
        self,
        session_id: str,
        terminal_id: str,
        **kwargs: Any,
    ) -> KillTerminalResponse:
        terminal = self._terminal(session_id, terminal_id)
        with contextlib.suppress(ProcessLookupError):
            if sys.platform == "win32":
                if terminal.process.returncode is None:
                    terminal.process.kill()
            else:
                os.killpg(terminal.process.pid, signal.SIGKILL)
        await asyncio.shield(terminal.process.wait())
        return KillTerminalResponse()

    async def release_terminal(
        self,
        session_id: str,
        terminal_id: str,
        **kwargs: Any,
    ) -> ReleaseTerminalResponse:
        await self.kill_terminal(session_id, terminal_id)
        terminal = self.terminals.pop(terminal_id)
        assert terminal.drain is not None
        await asyncio.shield(terminal.drain)
        return ReleaseTerminalResponse()
