"""Official SDK manages explicit local CLI sessions; no arbitrary TUI takeover."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
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
from ..discovery import profile_environment, refresh
from . import DriverError, Emit


@dataclass
class ManagedSession:
    id: str
    client: ClaudeSDKClient
    ready: asyncio.Future[None]
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    operation: str | None = None
    active_operation: str | None = None
    execution: str | None = None
    active: bool = False
    status: str = "unknown"
    watches: dict[str, str | None] = field(default_factory=dict)
    # Inputs written to the CLI but not yet taken up: input UUID -> operation, or None
    # once that operation was detached. The CLI replays an input's own UUID when it
    # takes it up (replay-user-messages).
    inputs: dict[str, str | None] = field(default_factory=dict)
    connection_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass
class PendingRequest:
    """A native permission/question callback still waiting on this SDK owner."""

    session: ManagedSession
    answer: asyncio.Future[dict[str, Any]]
    consumed: asyncio.Future[bool]
    kind: str
    tool: str
    # Operations (owner, watches) that received its full request.
    recipients: set[str] = field(default_factory=set)
    # No Yuki operation or watch received it, or all that did were detached; only
    # metadata left this computer, so the only answer accepted for it is deny.
    unowned: bool = False


# Admission is the CLI replaying an input's own UUID; verified on this Claude Code.
REPLAYED_INPUTS_VERSION = (2, 1, 311)
_INPUTS = frozenset({"prompt", "queue"})
CAPABILITIES = (
    "list",
    "get",
    "watch",
    "prompt",
    "queue",
    "respond",
    "cancel",
    "detach",
)


class ClaudeDriver:
    # `queue` appends an input whose landing the CLI decides (next tool boundary of
    # the running turn, or a new turn). It is not a precise steer of a chosen turn.

    def __init__(self, spec: Instance, emit: Emit) -> None:
        self.spec, self.emit = spec, emit
        # Declared per instance: a CLI that cannot replay inputs offers no prompt/queue.
        self.capabilities = list(CAPABILITIES)
        self.sessions: dict[str, ManagedSession] = {}
        self.requests: dict[str, PendingRequest] = {}
        self._opening: dict[str, asyncio.Lock] = {}
        # Why prompt/queue are not offered by this CLI, if they are not.
        self._inputs_refused: str | None = None

    async def start(self) -> None:
        if self.spec.installation:
            self.spec = refresh(self.spec)
        executable = shutil.which(self.spec.command) or self.spec.command
        if not await asyncio.to_thread(Path(executable).is_file):
            raise DriverError("cli_not_found", "Configure the installed Claude executable")
        if Path(executable).suffix.lower() in {".cmd", ".bat"}:
            raise DriverError(
                "cli_not_executable", "Choose the native Claude executable, not .cmd/.bat"
            )
        version = await self._version(executable)
        if version is not None and version < REPLAYED_INPUTS_VERSION:
            # Without UUID replay an input's admission cannot be observed: keep the
            # read and control actions, refuse new input with the reason.
            self.capabilities = [item for item in CAPABILITIES if item not in _INPUTS]
            needed = ".".join(map(str, REPLAYED_INPUTS_VERSION))
            self._inputs_refused = (
                f"Claude Code {'.'.join(map(str, version))} cannot confirm which input it took "
                f"up; update Claude Code to {needed} or newer"
            )

    @staticmethod
    async def _version(executable: str) -> tuple[int, ...] | None:
        """The CLI's own `-v` answer (the SDK asks the same before each session)."""
        process = await asyncio.create_subprocess_exec(
            executable,
            "-v",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 10)
        except TimeoutError:
            process.kill()
            await process.wait()
            return None
        match = re.match(r"\s*(\d+)\.(\d+)\.(\d+)", output.decode(errors="replace"))
        return tuple(int(part) for part in match.groups()) if match else None

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

    async def _publish(
        self, session: ManagedSession, kind: str, payload: dict[str, Any]
    ) -> set[str]:
        """Send to the turn's owner and matching watches; return the operations reached."""
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
        return operations

    async def _session_event(
        self, session: ManagedSession, kind: str, payload: dict[str, Any]
    ) -> None:
        """A session fact no operation owns. Metadata only: never commands, paths or text."""
        await self.emit(
            kind,
            {
                "session_id": session.id,
                "execution_id": session.execution,
                "operation_id": None,
                "payload": payload,
            },
        )

    async def _unowned(self, key: str, request: PendingRequest) -> None:
        """Make a wait no live operation receives discoverable, without its content."""
        request.unowned = True
        await self._session_event(
            request.session,
            request.kind,
            {
                "permission_id": key,
                "native_request_id": key,
                "connection_id": request.session.connection_id,
                "tool": request.tool,
                "options": ["deny"],
                "unowned": True,
                "status": "requires_action",
                "pending": True,
            },
        )

    async def _permission(
        self,
        session: ManagedSession,
        tool: str,
        arguments: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        key = context.tool_use_id or str(uuid4())
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        consumed: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        question = tool == "AskUserQuestion"
        kind = "question" if question else "permission"
        request = PendingRequest(session, future, consumed, kind, tool)
        self.requests[key] = request
        try:
            request.recipients = await self._publish(
                session,
                kind,
                {
                    "permission_id": key,
                    "native_request_id": key,
                    "connection_id": session.connection_id,
                    "tool": tool,
                    "native_request": arguments,
                    "options": ["allow", "deny"],
                },
            )
            if not request.recipients:
                # For example a turn the CLI started for a finished background task.
                # Make the wait discoverable without its content; deny unblocks it.
                await self._unowned(key, request)
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
            if self.requests.get(key) is request:
                self.requests.pop(key)
            if request.unowned:
                await self._session_event(
                    session,
                    "progress",
                    {
                        "permission_resolved": key,
                        "outcome": "answered" if consumed.result() else "withdrawn",
                        "status": "running" if session.active else session.status,
                        "pending": True,
                    },
                )

    async def _open_once(
        self, session_id: str, *, resume: bool, cwd: str | None = None
    ) -> ManagedSession:
        """Concurrent resumes of one session share a single SDK owner."""
        lock = self._opening.setdefault(session_id, asyncio.Lock())
        try:
            async with lock:
                current = self.sessions.get(session_id)
                if current is not None and not current.stopped.is_set():
                    return current
                return await self._open(session_id, resume=resume, cwd=cwd)
        finally:
            # A caller still waiting holds this lock object; a later one finds the
            # session already registered under its own lock.
            if not lock.locked() and self._opening.get(session_id) is lock:
                del self._opening[session_id]

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

        owner: list[ManagedSession] = []

        async def permission(
            tool: str,
            arguments: dict[str, Any],
            context: ToolPermissionContext,
        ) -> PermissionResultAllow | PermissionResultDeny:
            # Bound to this SDK owner, never to whichever owner later reuses the ID.
            return await self._permission(owner[0], tool, arguments, context)

        environment = dict(self.spec.env)
        profile = profile_environment(self.spec, "CLAUDE_CONFIG_DIR").get("CLAUDE_CONFIG_DIR")
        if profile is not None:
            environment["CLAUDE_CONFIG_DIR"] = profile
        options = ClaudeAgentOptions(
            cli_path=self.spec.command,
            cwd=cwd,
            env=environment,
            system_prompt={"type": "preset", "preset": "claude_code"},
            tools={"type": "preset", "preset": "claude_code"},
            setting_sources=["user", "project", "local"],
            resume=session_id if resume else None,
            session_id=None if resume else session_id,
            can_use_tool=permission,
            # The CLI echoes each input's own UUID when it takes the input up.
            extra_args={"replay-user-messages": None},
        )
        native = ManagedSession(
            session_id,
            ClaudeSDKClient(options=options),
            asyncio.get_running_loop().create_future(),
        )
        owner.append(native)
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
            # Inputs never taken up stay unknown on their own operations; only this
            # owner's callbacks are withdrawn, never a newer owner's for the same ID.
            for request in list(self.requests.values()):
                if request.session is session and not request.answer.done():
                    request.answer.cancel()
            for identity, operation in list(session.inputs.items()):
                if operation is None:
                    continue
                # Written, never seen taken up before the owner ended: unknown, not failed.
                await self.emit(
                    "progress",
                    {
                        "session_id": session.id,
                        "execution_id": identity,
                        "operation_id": operation,
                        "payload": {
                            "status": "unknown",
                            "pending": True,
                            "reason": "native_sdk_connection_lost",
                            # Only what the driver saw: the CLI never replayed it.
                            "replay_observed": False,
                        },
                    },
                )
            session.inputs.clear()

    async def _taken_up(self, session: ManagedSession, identity: str) -> None:
        """The CLI took up a written input (its UUID was replayed)."""
        operation = session.inputs.pop(identity)
        if session.active and operation is not None and session.operation is None:
            # Absorbed into a turn no operation owns (the CLI started it by itself just
            # before taking this input up): the turn now also answers this input, so
            # its remaining results and questions go to the operation that wrote it.
            session.execution = identity
            session.operation = session.active_operation = operation
            await self._publish(
                session,
                "progress",
                {"status": "running", "pending": True, "disposition": "merged_into_unowned_turn"},
            )
            return
        if session.active:
            if operation is None:
                return
            # Absorbed at a tool boundary of the turn already running. The input's
            # operation gets only its own disposition, never that turn's results,
            # questions or controls, which stay with the turn's owner.
            await self.emit(
                "completed",
                {
                    "session_id": session.id,
                    "execution_id": identity,
                    "operation_id": operation,
                    "payload": {
                        "status": "completed",
                        "pending": False,
                        "admitted": True,
                        "disposition": "merged_into_running_turn",
                        "merged_into_execution": session.execution,
                    },
                },
            )
            await self.emit("work_finished", {"operation_id": operation})
            return
        # It starts a new turn, owned by the operation that wrote it (none once detached).
        session.active, session.status = True, "running"
        session.execution = identity
        session.operation = session.active_operation = operation
        await self._publish(
            session,
            "progress",
            {"status": "running", "pending": True, "disposition": "started_new_turn"},
        )

    async def _receive(self, session: ManagedSession) -> None:
        async for message in session.client.receive_messages():
            if isinstance(message, UserMessage) and message.uuid in session.inputs:
                await self._taken_up(session, message.uuid)
                continue
            if (
                isinstance(message, (UserMessage, AssistantMessage))
                and message.parent_tool_use_id is None
                and not session.active
            ):
                # A turn the CLI started by itself (for example a background-task
                # notification). No Yuki operation owns it, but the session is busy:
                # a new prompt must conflict now instead of queueing behind it.
                session.active, session.status = True, "running"
                session.execution = f"self-started:{uuid4()}"
                session.operation = session.active_operation = None
            if isinstance(message, ResultMessage):
                completed_operation = session.active_operation
                session.active_operation = None
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
                if session.operation == completed_operation:
                    session.operation = None
                session.watches = {
                    key: value for key, value in session.watches.items() if value is None
                }
                if completed_operation is not None:
                    await self.emit("work_finished", {"operation_id": completed_operation})
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
            env=profile_environment(self.spec, "CLAUDE_CONFIG_DIR"),
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
            expected = command.get("execution_id")
            result = await self._inspect(
                "get", {**arguments, "session_id": identity, "execution_id": expected}
            )
            if session:
                result.update(
                    status=session.status,
                    execution_id=session.execution,
                    live_attach=not session.stopped.is_set(),
                    pending=session.active or session.status == "unknown",
                    status_scope="execution" if expected else "session",
                )
            if expected and (session is None or session.execution != expected):
                result.update(
                    execution_id=expected,
                    status="unknown",
                    pending=True,
                    status_scope="execution",
                    reason="execution_not_attached",
                )
            return result
        if action in _INPUTS:
            if self._inputs_refused is not None:
                raise DriverError("cli_outdated", self._inputs_refused)
            if session and session.stopped.is_set() and arguments.get("resume"):
                session = None
            if session is None:
                if identity and not arguments.get("resume"):
                    raise DriverError(
                        "explicit_resume_required",
                        "Stored sessions are not live attachments; explicitly authorize resume",
                    )
                session = await self._open_once(
                    identity or str(uuid4()), resume=bool(identity), cwd=arguments.get("cwd")
                )
            # A session another command is still opening is written only once connected.
            await asyncio.shield(session.ready)
            if session.stopped.is_set():
                raise DriverError("owner_disconnected", "Original SDK session owner has exited")
            if action == "prompt" and (session.active or session.inputs):
                raise DriverError(
                    "conflict",
                    "Original Claude session is busy; queue appends an input it takes up later",
                )
            # One UUID names the input and, if it starts a turn, that turn. Its
            # admission is the CLI replaying this UUID, reported as a later event.
            written = str(uuid4())
            session.inputs[written] = command["id"]

            async def user_input() -> AsyncIterator[dict[str, Any]]:
                yield {
                    "type": "user",
                    "uuid": written,
                    "session_id": session.id,
                    "parent_tool_use_id": None,
                    "message": {"role": "user", "content": arguments["prompt"]},
                }

            try:
                await session.client.query(user_input(), session_id=session.id)
            except BaseException:
                session.inputs.pop(written, None)
                raise
            return {
                "session_id": session.id,
                "execution_id": written,
                "status": "submitted",
                "pending": True,
                "admitted": False,
                "landing": "native_decides",
            }
        if action == "steer":
            raise DriverError(
                "unsupported_action",
                "Claude cannot bind input to a chosen running turn; queue appends instead",
            )
        if action == "detach":
            detached = arguments["operation_id"]
            for attached in self.sessions.values():
                attached.watches.pop(detached, None)
                if attached.operation == detached:
                    attached.operation = None
                # An input it wrote that the CLI has not taken up yet starts or joins
                # a turn nobody owns, so its waits stay discoverable.
                for written, writer in attached.inputs.items():
                    if writer == detached:
                        attached.inputs[written] = None
            for key, request in list(self.requests.items()):
                if request.unowned or request.answer.done():
                    continue
                request.recipients.discard(detached)
                if not request.recipients:
                    # Its receivers' events are no longer delivered: metadata only.
                    await self._unowned(key, request)
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
        if action == "cancel":
            if command.get("execution_id") in session.inputs:
                # Interrupt stops the running turn, not one queued input, which the CLI
                # still takes up afterwards; there is no native per-input withdrawal.
                raise DriverError(
                    "queued_input_not_withdrawable",
                    "This input is still queued in Claude; interrupt would stop another turn",
                )
            if command.get("execution_id") != session.execution:
                raise DriverError(
                    "execution_mismatch", "Control must target the original execution"
                )
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
            # The original callback is the fact: this SDK owner (connection), this
            # request ID, still pending. Its turn need not be the current turn.
            if arguments.get("connection_id") != session.connection_id:
                raise DriverError(
                    "request_resolved", "Original SDK connection is no longer current"
                )
            pending = self.requests.get(str(arguments["permission_id"]))
            if not pending or pending.session is not session or pending.answer.done():
                raise DriverError(
                    "request_resolved", "Original native question is no longer pending"
                )
            formal = arguments.get("response", {})
            choice = formal.get("behavior") or arguments.get("option_id")
            if choice not in {"allow", "deny"}:
                raise DriverError("invalid_option", "Select the original allow/deny option")
            if pending.unowned and choice != "deny":
                raise DriverError(
                    "deny_only",
                    "Only its metadata left this computer; allow it in Claude itself",
                )
            pending.answer.set_result(arguments)
            if not await pending.consumed:
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
