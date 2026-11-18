"""Official SDK manages explicit local CLI sessions; no arbitrary TUI takeover."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
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
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
    UserMessage,
)

from ..config import Instance
from ..discovery import profile_environment, refresh
from ..journal import followup_id
from ..scope import Scope
from . import DriverError, Emit

# Side-effect tools a scoped delegation sends through can_use_tool. `ask` wins over
# the user's own allow rules while setting sources stay loaded (§15 E6).
SCOPE_ASK = ("Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch")
# Cross-session messages reach the user's other sessions: always reported (§15 E4).
SCOPE_REPORTED = ("SendMessage",)
_WRITE_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path"}
_REGISTRY_STATUS = {"idle": "idle", "busy": "running", "waiting": "requires_action"}


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        try:
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and (
                code.value == 259  # STILL_ACTIVE
            )
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def registry_rows(directory: Path) -> list[dict[str, Any]]:
    """Live rows of Claude Code's session registry (the source of `claude agents`)."""
    rows = []
    for path in directory.glob("*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # The CLI removed or is rewriting this row right now.
        if isinstance(row, dict) and isinstance(row.get("pid"), int) and pid_alive(row["pid"]):
            rows.append(row)
    return rows


def observed(payload: dict[str, Any]) -> dict[str, Any]:
    """What an observe watch learns of a request: never its content or a way to answer."""
    return {
        "native_request_id": payload["native_request_id"],
        "tool": payload["tool"],
        "observed": True,
    }


def _waiting(row: dict[str, Any]) -> str | None:
    if row.get("status") != "waiting":
        return None
    return f"{row.get('sessionId')}:{row.get('statusUpdatedAt')}"


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
    cwd: str = ""
    # Opened with the scope's `ask` override; only such an owner can serve a scope.
    scoped: bool = False
    # The latest Yuki operation whose input this owner took up. The process exists
    # for it, so turns the CLI starts by itself belong to it until it is detached.
    owner_operation: str | None = None
    # The native ID of a self-started turn attributed to owner_operation.
    followup: str | None = None
    release_after_turn: bool = False


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
_INPUTS = frozenset({"prompt", "queue", "scope"})
CAPABILITIES = (
    "list",
    "get",
    "watch",
    "prompt",
    "queue",
    "respond",
    "cancel",
    "detach",
    "scope",
    "observe",
    "release",
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
        self.scopes: dict[str, Scope] = {}
        # Observe watches: metadata only, never an answerer (§4.3).
        self.observers: set[str] = set()
        # Observe watches of sessions no SDK owner of ours holds (terminal sessions).
        self.terminal_watches: dict[str, asyncio.Task[None]] = {}
        # How often the session registry is read; a local mechanism, not a gate.
        self.registry_interval = 0.5

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
        for watcher in self.terminal_watches.values():
            watcher.cancel()
        for item in self.sessions.values():
            item.stopped.set()
        await asyncio.gather(
            *self.terminal_watches.values(),
            *(item.task for item in self.sessions.values() if item.task),
            return_exceptions=True,
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
        """Send to the turn's owner and matching watches; return who may answer it."""
        if payload.get("status") != "unknown":
            payload.setdefault("admitted", True)
        operations = {
            operation
            for operation, execution in session.watches.items()
            if execution in {None, "", session.execution}
        } | ({session.operation} if session.operation else set())
        request = kind in {"permission", "question"}
        answerers = set()
        if session.followup is not None and session.owner_operation is not None:
            # A turn the CLI started by itself inside this held owner (§4.8).
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
                event_payload = observed(payload)
            elif request:
                answerers.add(operation)
            await self.emit(
                event_kind,
                {
                    "session_id": session.id,
                    "execution_id": session.execution,
                    "operation_id": operation,
                    "payload": event_payload,
                },
            )
        return answerers

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

    def _inside_scope(self, session: ManagedSession, tool: str, arguments: dict[str, Any]) -> bool:
        owner = session.operation or (session.owner_operation if session.followup else None)
        scope = self.scopes.get(owner) if owner else None
        if scope is None:
            return False
        if tool in _WRITE_TOOLS or tool == "NotebookEdit":
            path = arguments.get(_WRITE_TOOLS.get(tool, "notebook_path"))
            return isinstance(path, str) and scope.writes(path, session.cwd)
        if tool == "Bash":
            command = arguments.get("command")
            return isinstance(command, str) and scope.runs(command)
        if tool in {"WebFetch", "WebSearch"}:
            return scope.network
        return False

    async def _permission(
        self,
        session: ManagedSession,
        tool: str,
        arguments: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        key = context.tool_use_id or str(uuid4())
        if self._inside_scope(session, tool, arguments):
            # Inside the delegation's fixed scope (D2): no confirmation.
            await self._publish(
                session, "progress", {"permission_auto_approved": key, "tool": tool}
            )
            return PermissionResultAllow(updated_input=arguments)
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
        self,
        session_id: str,
        *,
        resume: bool,
        cwd: str | None = None,
        scope: dict[str, Any] | None = None,
        fork: bool = False,
    ) -> ManagedSession:
        """Concurrent resumes of one session share a single SDK owner."""
        lock = self._opening.setdefault(session_id, asyncio.Lock())
        try:
            async with lock:
                current = self.sessions.get(session_id)
                if current is not None and not current.stopped.is_set() and not fork:
                    return current
                return await self._open(session_id, resume=resume, cwd=cwd, scope=scope, fork=fork)
        finally:
            # A caller still waiting holds this lock object; a later one finds the
            # session already registered under its own lock.
            if not lock.locked() and self._opening.get(session_id) is lock:
                del self._opening[session_id]

    def _registry(self) -> Path:
        profile = profile_environment(self.spec, "CLAUDE_CONFIG_DIR").get("CLAUDE_CONFIG_DIR")
        if profile:
            return Path(profile) / "sessions"
        environment = self.spec.environment()
        home = environment.get("USERPROFILE" if sys.platform == "win32" else "HOME")
        return Path(home or Path.home()) / ".claude" / "sessions"

    async def _live_row(self, session_id: str) -> dict[str, Any] | None:
        """A live Claude process (not an SDK owner of ours) holding this session."""
        rows = await asyncio.to_thread(registry_rows, self._registry())
        owned = self.sessions.get(session_id)
        if owned is not None and not owned.stopped.is_set():
            return None
        return next((row for row in rows if row.get("sessionId") == session_id), None)

    async def _open(
        self,
        session_id: str,
        *,
        resume: bool,
        cwd: str | None = None,
        scope: dict[str, Any] | None = None,
        fork: bool = False,
    ) -> ManagedSession:
        cwd = cwd or self.spec.cwd
        if resume:
            # Two processes on one session silently fork its JSONL (§4.11, §15 E8).
            # Checked here and opened right after: a process starting in between is
            # a race no file lock of ours could close without touching user files.
            if not fork and await self._live_row(session_id) is not None:
                raise DriverError(
                    "session_live_elsewhere",
                    "This Claude session is running in another process; fork it or hand it off",
                    {"options": ["fork", "handoff"]},
                )
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
        bounds = Scope.parse(scope, cwd)
        identity = str(uuid4()) if fork else session_id
        options = ClaudeAgentOptions(
            cli_path=self.spec.command,
            cwd=cwd,
            env=environment,
            system_prompt={"type": "preset", "preset": "claude_code"},
            tools={"type": "preset", "preset": "claude_code"},
            # The user's settings, CLAUDE.md, hooks and MCP stay loaded.
            setting_sources=["user", "project", "local"],
            resume=session_id if resume else None,
            # A fork names its new session; the original transcript is untouched.
            session_id=identity if fork or not resume else None,
            fork_session=fork,
            add_dirs=list(bounds.write_paths) if bounds else [],
            settings=json.dumps({"permissions": {"ask": [*SCOPE_ASK, *SCOPE_REPORTED]}})
            if bounds
            else None,
            can_use_tool=permission,
            # The CLI echoes each input's own UUID when it takes the input up.
            extra_args={"replay-user-messages": None},
        )
        native = ManagedSession(
            identity,
            ClaudeSDKClient(options=options),
            asyncio.get_running_loop().create_future(),
            cwd=cwd,
            scoped=bounds is not None,
        )
        owner.append(native)
        self.sessions[identity] = native
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
        if operation is not None:
            session.owner_operation = operation
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

    async def _self_started(self, session: ManagedSession, native: str) -> None:
        """A turn the CLI started by itself (background-task notification, wakeup).

        The session is busy: a new prompt must conflict now instead of queueing
        behind it. With no input of ours outstanding it belongs to the operation
        this SDK owner is held for (§4.8, §15 E7); with none it has no owner.
        """
        session.active, session.status = True, "running"
        session.execution = native
        session.operation = session.active_operation = None
        if session.owner_operation is not None and not session.inputs:
            session.followup = native
            await self._publish(
                session, "progress", {"status": "running", "pending": True, "self_started": True}
            )

    async def _receive(self, session: ManagedSession) -> None:
        async for message in session.client.receive_messages():
            if isinstance(message, UserMessage) and message.uuid in session.inputs:
                await self._taken_up(session, message.uuid)
                continue
            if (
                isinstance(message, SystemMessage)
                and message.subtype == "session_state_changed"
                and message.data.get("state") == "running"
                and not session.active
                and not session.inputs
            ):
                await self._self_started(session, message.data.get("uuid") or str(uuid4()))
                continue
            if (
                isinstance(message, (UserMessage, AssistantMessage))
                and message.parent_tool_use_id is None
                and not session.active
            ):
                await self._self_started(session, message.uuid or str(uuid4()))
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
                        "pending": False,
                        "text": message.result,
                        "usage": message.usage,
                        "subtype": message.subtype,
                        "stop_reason": message.stop_reason,
                        "errors": message.errors,
                        "total_cost_usd": message.total_cost_usd,
                    },
                )
                session.followup = None
                if session.operation == completed_operation:
                    session.operation = None
                session.watches = {
                    key: value for key, value in session.watches.items() if value is None
                }
                if completed_operation is not None:
                    await self.emit("work_finished", {"operation_id": completed_operation})
                if session.release_after_turn and not self._held(session):
                    # Released at the turn boundary: the session is free for the
                    # user's `claude --resume` without a fork (§4.11, §15 E8).
                    session.stopped.set()
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

    def _held(self, session: ManagedSession) -> bool:
        """Whether any Yuki operation of this connector still holds this SDK owner."""
        return bool(
            session.operation
            or session.owner_operation
            or session.watches
            or any(writer is not None for writer in session.inputs.values())
        )

    async def _release(self, sessions: list[ManagedSession]) -> str | None:
        """Close Yuki's SDK owner at the turn boundary once no operation holds it."""
        closed: str | None = "now"
        for session in sessions:
            if session.stopped.is_set():
                continue
            if self._held(session):
                closed = None
            elif session.active:
                session.release_after_turn = True
                closed = "after_turn" if closed == "now" else closed
            else:
                session.stopped.set()
                if session.task is not None:
                    await session.task
        return closed

    async def _observe_terminal(self, operation: str, session_id: str, row: dict[str, Any]) -> None:
        """Follow a session no SDK owner of ours holds through the session registry.

        Only facts the registry states leave this computer: the session is busy,
        idle or waiting for an approval. Nothing is written, answered or denied.
        """

        async def emit(kind: str, payload: dict[str, Any]) -> None:
            await self.emit(
                kind,
                {
                    "session_id": session_id,
                    "execution_id": None,
                    "operation_id": operation,
                    "payload": payload,
                },
            )

        async def waiting(current: dict[str, Any], key: str) -> None:
            await emit(
                "permission",
                {
                    "native_request_id": key,
                    "waiting_for": current.get("waitingFor"),
                    "observed": True,
                    "status": "requires_action",
                    "pending": True,
                },
            )

        previous = row
        if key := _waiting(row):
            await waiting(row, key)
        directory = self._registry()
        while True:
            await asyncio.sleep(self.registry_interval)
            rows = await asyncio.to_thread(registry_rows, directory)
            current = next((item for item in rows if item.get("sessionId") == session_id), None)
            if current is None:
                await emit("progress", {"status": "unknown", "reason": "not_live", "pending": True})
                return
            before, now = previous.get("status"), current.get("status")
            status = _REGISTRY_STATUS.get(str(now), "unknown")
            was, key = _waiting(previous), _waiting(current)
            if was and was != key:
                await emit(
                    "progress", {"permission_resolved": was, "status": status, "pending": True}
                )
            if key and key != was:
                await waiting(current, key)
            elif now == "idle" and before != "idle":
                await emit(
                    "progress", {"status": "idle", "execution_status": "completed", "pending": True}
                )
            elif now == "busy" and before == "idle":
                await emit("progress", {"status": "running", "pending": True})
            previous = current

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
            if session:
                result["queued_inputs"] = [
                    {"execution_id": written, "operation_id": writer}
                    for written, writer in session.inputs.items()
                ]
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
            if session is None or arguments.get("fork"):
                if identity and not arguments.get("resume"):
                    raise DriverError(
                        "explicit_resume_required",
                        "Stored sessions are not live attachments; explicitly authorize resume",
                    )
                session = await self._open_once(
                    identity or str(uuid4()),
                    resume=bool(identity),
                    cwd=arguments.get("cwd"),
                    scope=arguments.get("scope"),
                    fork=bool(identity and arguments.get("fork")),
                )
            # A session another command is still opening is written only once connected.
            await asyncio.shield(session.ready)
            if session.stopped.is_set():
                raise DriverError("owner_disconnected", "Original SDK session owner has exited")
            scope = Scope.parse(arguments.get("scope"), session.cwd)
            if scope is not None and not session.scoped:
                # Its process was opened without the `ask` override: the user's allow
                # rules would bypass this scope. Release it, then delegate again.
                raise DriverError(
                    "scope_conflict",
                    "This Claude session is held without a scope; release it first",
                )
            if action == "prompt" and (session.active or session.inputs):
                raise DriverError(
                    "conflict",
                    "Original Claude session is busy; queue appends an input it takes up later",
                )
            # One UUID names the input and, if it starts a turn, that turn. Its
            # admission is the CLI replaying this UUID, reported as a later event.
            written = str(uuid4())
            session.inputs[written] = command["id"]
            if scope is not None:
                self.scopes[command["id"]] = scope

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
                **({"forked_from": identity} if session.id != identity and identity else {}),
            }
        if action == "steer":
            raise DriverError(
                "unsupported_action",
                "Claude cannot bind input to a chosen running turn; queue appends instead",
            )
        if action == "detach":
            detached = arguments["operation_id"]
            self.scopes.pop(detached, None)
            self.observers.discard(detached)
            watcher = self.terminal_watches.pop(detached, None)
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            held = [
                attached
                for attached in self.sessions.values()
                if detached in attached.watches
                or detached in {attached.operation, attached.owner_operation}
                or detached in attached.inputs.values()
                or attached is session
            ]
            for attached in self.sessions.values():
                attached.watches.pop(detached, None)
                if attached.operation == detached:
                    attached.operation = None
                if attached.owner_operation == detached:
                    # Its self-started turns from now on are nobody's (deny-only).
                    attached.owner_operation = attached.followup = None
                # An input it wrote that the CLI has not taken up yet starts or joins
                # a turn nobody owns, so its waits stay discoverable.
                for written, writer in attached.inputs.items():
                    if writer == detached:
                        attached.inputs[written] = None
            for key, request in list(self.requests.items()):
                if request.unowned or request.answer.done():
                    continue
                request.recipients = {
                    recipient
                    for recipient in request.recipients
                    if recipient != detached and not recipient.startswith(f"followup:{detached}:")
                }
                if not request.recipients:
                    # Its receivers' events are no longer delivered: metadata only.
                    await self._unowned(key, request)
            if arguments.get("release"):
                return {
                    "session_id": identity,
                    "status": "detached",
                    "detached": True,
                    "pending": False,
                    "admitted": True,
                    "closed": await self._release(held),
                }
            return {
                "session_id": identity,
                "status": "completed",
                "detached": True,
                "admitted": True,
            }
        observe = action == "watch" and arguments.get("observation") == "observe"
        if observe and (session is None or session.stopped.is_set()):
            assert identity is not None
            row = await self._live_row(identity)
            if row is None:
                raise DriverError("not_live", "No live Claude process holds this session")
            self.observers.add(command["id"])
            self.terminal_watches[command["id"]] = asyncio.create_task(
                self._observe_terminal(command["id"], identity, row)
            )
            return {
                "session_id": identity,
                "execution_id": None,
                "status": _REGISTRY_STATUS.get(str(row.get("status")), "unknown"),
                "pending": True,
                "admitted": True,
                "observed_via": "session_registry",
            }
        if session is None:
            raise DriverError(
                "not_live", "Independent Claude terminals are readonly, not SDK-owned"
            )
        if action == "watch":
            if observe:
                self.observers.add(command["id"])
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
