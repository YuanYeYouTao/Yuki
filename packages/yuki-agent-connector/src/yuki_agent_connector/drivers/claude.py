"""Official SDK manages explicit local CLI sessions; no arbitrary TUI takeover."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, cast
from uuid import uuid4

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
    UserMessage,
)

from ..config import Instance
from . import DriverError, Emit


@dataclass
class ManagedSession:
    id: str
    client: ClaudeSDKClient
    ready: asyncio.Future[None]
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    operation: str | None = None
    execution: str | None = None
    active: bool = False
    status: str = "unknown"
    watches: dict[str, str | None] = field(default_factory=dict)
    admitted: asyncio.Future[bool] | None = None
    connection_id: str = field(default_factory=lambda: str(uuid4()))


class ClaudeDriver:
    capabilities: ClassVar[list[str]] = [
        "list",
        "get",
        "watch",
        "prompt",
        "respond",
        "cancel",
        "detach",
    ]

    def __init__(self, spec: Instance, emit: Emit) -> None:
        self.spec, self.emit = spec, emit
        self.sessions: dict[str, ManagedSession] = {}
        self.requests: dict[
            str, tuple[str, asyncio.Future[dict[str, Any]], asyncio.Future[bool]]
        ] = {}

    async def start(self) -> None:
        executable = shutil.which(self.spec.command) or self.spec.command
        if not await asyncio.to_thread(Path(executable).is_file):
            raise DriverError("cli_not_found", "Configure the installed Claude executable")
        if Path(executable).suffix.lower() in {".cmd", ".bat"}:
            raise DriverError(
                "cli_not_executable", "Choose the native Claude executable, not .cmd/.bat"
            )

    async def stop(self) -> None:
        for item in self.sessions.values():
            item.stopped.set()
        await asyncio.gather(
            *(item.task for item in self.sessions.values() if item.task), return_exceptions=True
        )

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
                    "reason": "previous_sdk_owner_not_attached",
                    "stream_gap": True,
                    "readonly_history_available": True,
                },
            },
        )

    async def _publish(self, session: ManagedSession, kind: str, payload: dict[str, Any]) -> None:
        if payload.get("status") != "unknown":
            payload.setdefault("admitted", True)
        operations = {
            operation
            for operation, execution in session.watches.items()
            if execution in {None, "", session.execution}
        } | ({session.operation} if session.operation else set())
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
            await self.emit(
                event_kind,
                {
                    "session_id": session.id,
                    "execution_id": session.execution,
                    "operation_id": operation,
                    "payload": event_payload,
                },
            )

    async def _permission(
        self,
        session_id: str,
        tool: str,
        arguments: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        session = self.sessions[session_id]
        if session.admitted and not session.admitted.done():
            session.admitted.set_result(True)
        key = context.tool_use_id or str(uuid4())
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        consumed: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.requests[key] = (session_id, future, consumed)
        try:
            question = tool == "AskUserQuestion"
            await self._publish(
                session,
                "question" if question else "permission",
                {
                    "permission_id": key,
                    "native_request_id": key,
                    "connection_id": session.connection_id,
                    "tool": tool,
                    "native_request": arguments,
                    "options": ["allow", "deny"],
                },
            )
            response = await future
            formal = response.get("response", {})
            choice = formal.get("behavior") or response.get("option_id")
            if choice == "allow":
                updated = formal.get("updatedInput", dict(arguments))
                if question and "answers" in formal:
                    updated["answers"] = formal["answers"]
                decision: PermissionResultAllow | PermissionResultDeny = PermissionResultAllow(
                    updated_input=updated
                )
            else:
                decision = PermissionResultDeny(message="User declined the original native request")
            consumed.set_result(True)
            return decision
        finally:
            if not consumed.done():
                consumed.set_result(False)
            self.requests.pop(key, None)

    async def _open(
        self, session_id: str, *, resume: bool, cwd: str | None = None
    ) -> ManagedSession:
        cwd = cwd or self.spec.cwd
        if resume:
            metadata = await self._inspect("get", {"session_id": session_id})
            original = metadata.get("native")
            if not original or not original.get("cwd"):
                raise DriverError(
                    "cwd_unknown", "Native session did not provide its original working directory"
                )
            cwd = original["cwd"]

        async def permission(
            tool: str,
            arguments: dict[str, Any],
            context: ToolPermissionContext,
        ) -> PermissionResultAllow | PermissionResultDeny:
            return await self._permission(session_id, tool, arguments, context)

        options = ClaudeAgentOptions(
            cli_path=self.spec.command,
            cwd=cwd,
            env=self.spec.env,
            system_prompt={"type": "preset", "preset": "claude_code"},
            tools={"type": "preset", "preset": "claude_code"},
            setting_sources=["user", "project", "local"],
            resume=session_id if resume else None,
            session_id=None if resume else session_id,
            can_use_tool=permission,
        )
        native = ManagedSession(
            session_id,
            ClaudeSDKClient(options=options),
            asyncio.get_running_loop().create_future(),
        )
        self.sessions[session_id] = native
        native.task = asyncio.create_task(self._serve(native))
        await native.ready
        return native

    async def _serve(self, session: ManagedSession) -> None:
        receive: asyncio.Task[None] | None = None
        wait: asyncio.Task[bool] | None = None
        try:
            # The SDK task group remains owned by this long-lived task. Remote
            # socket reconnect never exits it or implicitly kills the CLI.
            async with session.client:
                session.ready.set_result(None)
                receive = asyncio.create_task(self._receive(session))
                wait = asyncio.create_task(session.stopped.wait())
                done, _ = await asyncio.wait((receive, wait), return_when=asyncio.FIRST_COMPLETED)
                wait.cancel()
                if receive in done:
                    await receive
                    if not session.stopped.is_set():
                        if session.status not in {"completed", "failed", "cancelled"}:
                            session.status = "unknown"
                        await self._publish(
                            session,
                            "progress",
                            {
                                "status": "unknown",
                                "pending": True,
                                "reason": "native_sdk_connection_lost",
                                "stream_gap": True,
                            },
                        )
                else:
                    receive.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await receive
        except Exception as exc:
            if session.status not in {"completed", "failed", "cancelled"}:
                session.status = "unknown"
            if not session.ready.done():
                session.ready.set_exception(exc)
            else:
                await self._publish(
                    session,
                    "progress",
                    {
                        "status": "unknown",
                        "reason": "native_sdk_connection_lost",
                        "error_type": type(exc).__name__,
                    },
                )
        finally:
            tasks = [task for task in (receive, wait) if task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            session.active = False
            session.stopped.set()
            if session.admitted and not session.admitted.done():
                session.admitted.set_exception(
                    DriverError("unknown", "Native admission was not confirmed", uncertain=True)
                )
            for owner, future, _ in list(self.requests.values()):
                if owner == session.id and not future.done():
                    future.cancel()

    async def _receive(self, session: ManagedSession) -> None:
        async for message in session.client.receive_messages():
            if isinstance(message, (UserMessage, AssistantMessage, ResultMessage)):
                if session.admitted and not session.admitted.done():
                    session.admitted.set_result(True)
            if isinstance(message, ResultMessage):
                session.active = False
                session.status = "failed" if message.is_error else "completed"
                await self._publish(
                    session,
                    "completed",
                    {
                        "status": session.status,
                        "text": message.result,
                        "usage": message.usage,
                        "subtype": message.subtype,
                        "stop_reason": message.stop_reason,
                        "errors": message.errors,
                        "total_cost_usd": message.total_cost_usd,
                    },
                )
                session.operation = None
                session.watches = {
                    key: value for key, value in session.watches.items() if value is None
                }
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    # Only completed assistant/tool blocks, never thinking/deltas.
                    if isinstance(block, (TextBlock, ToolUseBlock)):
                        payload = (
                            {"text": block.text}
                            if isinstance(block, TextBlock)
                            else {
                                "tool": block.name,
                                "native_item_id": block.id,
                            }
                        )
                        await self._publish(session, "progress", payload)

    async def _inspect(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            *(["-I", "-B"] if sys.flags.isolated else []),
            "-m",
            "yuki_agent_connector.claude_inspect",
            env=self.spec.environment(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await process.communicate(
            json.dumps(
                {
                    **arguments,
                    "action": action,
                    "cwd": self.spec.cwd,
                }
            ).encode()
        )
        if process.returncode:
            raise DriverError("discovery_failed", "Native readonly discovery failed")
        return cast(dict[str, Any], json.loads(output))

    async def execute(self, command: dict[str, Any]) -> dict[str, Any]:
        action, arguments = command["action"], command.get("arguments", {})
        identity = command.get("session_id")
        session = self.sessions.get(identity) if identity else None
        if action == "list":
            return await self._inspect("list", arguments)
        if action == "get":
            result = await self._inspect("get", {**arguments, "session_id": identity})
            if session:
                result.update(
                    status=session.status,
                    execution_id=session.execution,
                    live_attach=not session.stopped.is_set(),
                    pending=session.active or session.status == "unknown",
                )
            return result
        if action == "prompt":
            if session and session.stopped.is_set() and arguments.get("resume"):
                session = None
            if session is None:
                if identity and not arguments.get("resume"):
                    raise DriverError(
                        "explicit_resume_required",
                        "Stored sessions are not live attachments; explicitly authorize resume",
                    )
                session = await self._open(
                    identity or str(uuid4()), resume=bool(identity), cwd=arguments.get("cwd")
                )
            if session.stopped.is_set():
                raise DriverError("owner_disconnected", "Original SDK session owner has exited")
            if session.active:
                raise DriverError("conflict", "Original Claude execution is already active")
            session.operation = command["id"]
            session.execution = str(uuid4())
            session.active, session.status = True, "running"
            session.admitted = asyncio.get_running_loop().create_future()

            async def user_input() -> AsyncIterator[dict[str, Any]]:
                yield {
                    "type": "user",
                    "uuid": session.execution,
                    "session_id": session.id,
                    "parent_tool_use_id": None,
                    "message": {"role": "user", "content": arguments["prompt"]},
                }

            await session.client.query(user_input(), session_id=session.id)
            admitted = await session.admitted
            return {
                "session_id": session.id,
                "execution_id": session.execution,
                "status": session.status,
                "pending": session.active,
                "admitted": admitted,
            }
        if action in {"queue", "steer"}:
            raise DriverError(
                "unsupported_action", "This SDK does not provide precise native steer/queue"
            )
        if action == "detach":
            for attached in self.sessions.values():
                attached.watches.pop(arguments["operation_id"], None)
                if attached.operation == arguments["operation_id"]:
                    attached.operation = None
            return {
                "session_id": identity,
                "status": "completed",
                "detached": True,
                "admitted": True,
            }
        if session is None:
            raise DriverError(
                "not_live", "Independent Claude terminals are readonly, not SDK-owned"
            )
        if action == "watch":
            if session.stopped.is_set():
                raise DriverError("owner_disconnected", "Original SDK session owner has exited")
            expected = command.get("execution_id")
            if expected and expected != session.execution:
                raise DriverError(
                    "execution_mismatch", "Watch must target the original native execution"
                )
            if session.active or not expected:
                session.watches[command["id"]] = (
                    None
                    if command.get("observation_scope") == "session"
                    else expected or session.execution or ""
                )
            return {
                "session_id": session.id,
                "execution_id": session.execution if session.active or expected else None,
                "status": session.status
                if session.active or expected
                else "idle"
                if command.get("observation_scope") == "session"
                else "unknown",
                "pending": session.active or not expected,
                "admitted": True,
            }
        if command.get("execution_id") != session.execution:
            raise DriverError("execution_mismatch", "Control must target the original execution")
        if action == "cancel":
            if not session.active:
                return {
                    "session_id": session.id,
                    "execution_id": session.execution,
                    "status": session.status,
                    "pending": False,
                }
            await session.client.interrupt()
            return {
                "session_id": session.id,
                "execution_id": session.execution,
                "status": session.status,
                "pending": True,
                "cancel_requested": True,
                "admitted": True,
            }
        if action == "respond":
            if arguments.get("connection_id") != session.connection_id:
                raise DriverError(
                    "request_resolved", "Original SDK connection is no longer current"
                )
            pending = self.requests.get(str(arguments["permission_id"]))
            if not pending or pending[0] != identity or pending[1].done():
                raise DriverError(
                    "request_resolved", "Original native question is no longer pending"
                )
            formal = arguments.get("response", {})
            if (formal.get("behavior") or arguments.get("option_id")) not in {"allow", "deny"}:
                raise DriverError("invalid_option", "Select the original allow/deny option")
            pending[1].set_result(arguments)
            if not await pending[2]:
                raise DriverError(
                    "unknown", "Original SDK callback did not consume the answer", uncertain=True
                )
            return {
                "session_id": session.id,
                "execution_id": session.execution,
                "status": "response_submitted",
                "pending": False,
                "response_submitted": True,
                "admitted": True,
            }
        raise DriverError("unsupported_action", f"Claude does not support {action}")
