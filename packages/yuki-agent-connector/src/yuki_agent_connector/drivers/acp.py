"""ACP on the actual host, with capabilities negotiated from the original agent."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from acp import Client
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

from ..config import Instance
from . import DriverError, Emit


def dump(value: Any) -> Any:
    return value.model_dump(mode="json", by_alias=True, exclude_none=True)


@dataclass
class Session:
    id: str
    cwd: str
    status: str = "unknown"
    operation: str | None = None
    execution: str | None = None
    task: asyncio.Task[None] | None = None
    watches: set[str] = field(default_factory=set)
    text: list[str] = field(default_factory=list)
    dispatched: asyncio.Event = field(default_factory=asyncio.Event)
    admitted: asyncio.Future[bool] | None = None


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
        self.capabilities = ["get", "watch", "prompt", "respond", "cancel", "detach"]
        self.connection: Any = None
        self.native_capabilities: dict[str, Any] = {}
        self.sessions: dict[str, Session] = {}
        self.requests: dict[str, tuple[str, list[Any], asyncio.Future[Any]]] = {}
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
                    client_info=Implementation(name="Yuki Agent Connector", version="0.1.0"),
                )
                self.native_capabilities = dump(initialized.agent_capabilities)
                if self.native_capabilities.get("sessionCapabilities", {}).get("list") is not None:
                    self.capabilities.insert(0, "list")
                self.ready.set_result(None)
                stopped = asyncio.create_task(self.stopped.wait())
                exited = asyncio.create_task(process.wait())
                done, _ = await asyncio.wait((stopped, exited), return_when=asyncio.FIRST_COMPLETED)
                stopped.cancel()
                exited.cancel()
                if exited in done and not self.stopped.is_set():
                    for session in self.sessions.values():
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
                for session in self.sessions.values():
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
            self.connection = None
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
            await self.worker
        # Only subprocesses this ACP driver created. Network/detach never calls stop.
        for terminal_id, terminal in list(self.terminals.items()):
            await self.release_terminal(terminal.session_id, terminal_id)

    async def recover(self, command: dict[str, Any], result: dict[str, Any]) -> None:
        await self.emit(
            "progress",
            {
                "session_id": result["session_id"],
                "execution_id": result.get("execution_id"),
                "operation_id": command["id"],
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
            session.dispatched.set()

    async def _publish(self, session: Session, kind: str, payload: dict[str, Any]) -> None:
        if payload.get("status") != "unknown":
            payload.setdefault("admitted", True)
        operations = session.watches | ({session.operation} if session.operation else set())
        for operation in operations:
            await self.emit(
                kind,
                {
                    "session_id": session.id,
                    "execution_id": session.execution,
                    "operation_id": operation,
                    "payload": payload,
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
        payload = dump(update)
        if session.admitted and not session.admitted.done():
            session.admitted.set_result(True)
        kind = payload.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = payload.get("content", {})
            if content.get("type") == "text":
                session.text.append(content["text"])
        elif kind in {"tool_call", "tool_call_update", "plan", "usage_update"}:
            await self._publish(session, "progress", {"native": payload})

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
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.requests[identity] = (session_id, options, future)
        try:
            await self._publish(
                session,
                "permission",
                {
                    "permission_id": identity,
                    "native_request_id": original_request,
                    "tool": dump(tool_call),
                    "options": [dump(item) for item in options],
                },
            )
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

    async def _prompt(self, session: Session, prompt: str) -> None:
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
            session.watches.clear()
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
                session.admitted.set_result(False)
            session.text.clear()
            session.operation = None
            session.dispatched.set()

    async def execute(self, command: dict[str, Any]) -> dict[str, Any]:
        action, arguments = command["action"], command.get("arguments", {})
        identity = command.get("session_id")
        if self.connection is None:
            raise DriverError("owner_disconnected", "Original ACP process is not connected")
        if action == "list":
            if action not in self.capabilities:
                raise DriverError("unsupported_action", "Agent does not advertise session/list")
            result = await self.connection.list_sessions(cursor=arguments.get("cursor"))
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
                created = await self.connection.new_session(cwd=cwd, mcp_servers=[])
                identity = created.session_id
                self.sessions[identity] = Session(identity, cwd)
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
                    await self.connection.resume_session(
                        session_id=identity, cwd=cwd, mcp_servers=[]
                    )
                elif self.native_capabilities.get("loadSession"):
                    await self.connection.load_session(session_id=identity, cwd=cwd, mcp_servers=[])
                else:
                    raise DriverError("unsupported_action", "Agent does not advertise load/resume")
                self.sessions[identity] = Session(identity, cwd)
            session = self._session(identity)
            if session.task and not session.task.done():
                raise DriverError("conflict", "Original ACP session has an active prompt")
            session.operation, session.status = command["id"], "running"
            session.admitted = asyncio.get_running_loop().create_future()
            session.dispatched.clear()
            session.task = asyncio.create_task(self._prompt(session, arguments["prompt"]))
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
            return {
                "session_id": identity,
                "execution_id": session.execution,
                "status": session.status,
                "pending": bool(session.task and not session.task.done()),
            }
        if action == "watch":
            active = bool(session.task and not session.task.done())
            expected = command.get("execution_id")
            if expected and expected != session.execution:
                raise DriverError(
                    "execution_mismatch", "Watch must target the original native prompt"
                )
            if active or not expected:
                session.watches.add(command["id"])
            return {
                "session_id": identity,
                "execution_id": session.execution if active or expected else None,
                "status": session.status if active or expected else "unknown",
                "pending": active or not expected,
                "admitted": True,
            }
        if action == "detach":
            session.watches.discard(arguments["operation_id"])
            return {
                "session_id": identity,
                "status": "completed",
                "detached": True,
                "admitted": True,
            }
        if command.get("execution_id") != session.execution:
            raise DriverError("execution_mismatch", "Control must target the original ACP prompt")
        if action == "cancel":
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
            request = self.requests.get(str(arguments["permission_id"]))
            if not request or request[0] != identity or request[2].done():
                raise DriverError(
                    "request_resolved", "Original ACP permission is no longer pending"
                )
            choice = arguments.get("option_id")
            if "response" in arguments:
                formal = arguments["response"]["outcome"]
                choice = formal.get("optionId") if formal["outcome"] == "selected" else None
            if choice is not None and choice not in {item.option_id for item in request[1]}:
                raise DriverError("invalid_option", "Answer must select an original native option")
            key = str(arguments["permission_id"])
            delivered: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self.responses[key] = delivered
            try:
                request[2].set_result(choice)
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
        self._session(session_id)
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
