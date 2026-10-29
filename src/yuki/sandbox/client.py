"""Bounded Unix-socket client and deployment-stable tool contracts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from yuki.capabilities.executions import is_execution
from yuki.domain.messages import ChatTool
from yuki.sandbox.environment_tools import (
    READ_TOOLS,
    SANDBOX_TOOLS,
    environment_tools,
)
from yuki.workspace.tools import WORKSPACE_READ_TOOLS, WORKSPACE_TOOLS

if TYPE_CHECKING:
    from yuki.external_agents.local_device import LocalDeviceService
    from yuki.sandbox.task_repository import SandboxTaskRepository


def sandbox_tools() -> tuple[ChatTool, ...]:
    return environment_tools()


class SandboxClient:
    def __init__(self, socket: Path, *, tasks: SandboxTaskRepository | None = None) -> None:
        self.socket = socket
        self.tasks = tasks
        self.local_devices: LocalDeviceService | None = None

    @asynccontextmanager
    async def stream(
        self, method: str, args: dict[str, Any]
    ) -> AsyncIterator[tuple[dict[str, Any], asyncio.StreamReader, asyncio.StreamWriter]]:
        if method not in {"display_stream", "files_read", "control_watch"}:
            raise ValueError("unknown_stream_method")
        connect = getattr(asyncio, "open_unix_connection", None)
        if connect is None:
            raise NotImplementedError("unix_stream_unavailable")
        reader, writer = await connect(str(self.socket))
        try:
            writer.write(json.dumps({"method": method, "args": args}).encode() + b"\n")
            await writer.drain()
            from yuki.sandbox.protocol import read_protocol_line

            header = json.loads(await read_protocol_line(reader))
            if not isinstance(header, dict):
                raise ValueError("invalid_stream_response")
            if header.get("error"):
                raise ValueError(str(header["error"]))
            yield header, reader, writer
        finally:
            writer.close()
            await writer.wait_closed()

    async def execute(
        self,
        name: str,
        args: dict[str, Any],
        *,
        request_id: str,
        source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.local_devices is not None:
            if name in {"terminal_exec", "environment_status"} and "device_id" in args:
                return await self.local_devices.execute(
                    name,
                    args,
                    request_id=request_id,
                    source=source,
                )
            if name in {
                "terminal_read",
                "terminal_control",
                "get_code_run",
                "get_code_run_by_request",
            }:
                assert self.tasks is not None
                row = (
                    await self.tasks.get(str(args["request_id"]))
                    if name == "get_code_run_by_request"
                    else await self.tasks.by_run(str(args["run_id"]))
                )
                if row is None:
                    return {
                        **{key: args[key] for key in ("run_id", "request_id") if key in args},
                        "status": "unknown",
                        "pending": True,
                        "uncertain": True,
                        "error": "original_execution_target_not_confirmed",
                        "retryable": False,
                    }
                from yuki.external_agents.local_device import terminal_target

                if terminal_target(row) is not None:
                    return await self.local_devices.execute(
                        name,
                        args,
                        request_id=request_id,
                        source=source,
                    )
                if json.loads(row.progress_json).get("kind") == "external_agent":
                    return await self._native_terminal(name, args, request_id, source)
                # Existing host-owned rows from before protocol 4 belong to the Manager.
        execution = is_execution(name, args)
        wire_args = args
        if name == "terminal_exec" and source is not None and source.get("work_id"):
            wire_args = {**args, "env": {"YUKI_WORK_ID": source["work_id"]}}
        message = (
            json.dumps({"method": name, "args": wire_args, "request_id": request_id}).encode()
            + b"\n"
        )
        if len(message) > 262144:
            return {"error": "request_too_large", "retryable": False}
        if execution and self.tasks is not None:
            if source is None:
                return {"error": "missing_task_source", "retryable": False}
            # Must commit before any socket write, including uncertain submissions.
            # Source never crosses into the execution container/Manager payload.
            prepared = await self.tasks.prepare(
                request_id,
                {"tool": name, "arguments": args},
                source,
            )
            if prepared is not None and prepared.status == "completed" and prepared.run_id is None:
                return cast(dict[str, Any], json.loads(prepared.completion_json or "{}"))
        submitted = False
        try:
            observation_wait = args.get("wait_seconds", 0) if name == "terminal_read" else 0
            async with asyncio.timeout(
                None
                if name
                in {
                    "sticker_index",
                    "workspace_publish",
                    "workspace_copy",
                    "workspace_copy_result",
                    "workspace_media_read",
                    "workspace_media_validate",
                }
                else 7 + observation_wait
            ):
                connect = getattr(asyncio, "open_unix_connection", None)
                if connect is None:
                    return {"error": "sandbox_unavailable", "retryable": False}
                reader, writer = await connect(
                    str(self.socket),
                    limit=(28 * 1024 * 1024 if name == "workspace_media_read" else 262144),
                )
                try:
                    submitted = True
                    writer.write(message)
                    await writer.drain()
                    from yuki.sandbox.protocol import read_protocol_line

                    response = await read_protocol_line(reader)
                    result = json.loads(response)
                    if not isinstance(result, dict):
                        raise ValueError("invalid_response")
                    if (
                        execution
                        and self.tasks is not None
                        and not result.get("run_id")
                        and result.get("error")
                        in {
                            "environment_unavailable",
                            "sandbox_unavailable",
                            "invalid_command",
                            "invalid_tty",
                            "invalid_cwd",
                            "invalid_timeout",
                        }
                    ):
                        await self.tasks.reject(request_id, result)
                    if execution and self.tasks is not None and result.get("run_id"):
                        await self.tasks.bind_run(request_id, result["run_id"])
                    await self._stage_result(name, request_id, result)
                    return result
                finally:
                    writer.close()
                    await writer.wait_closed()
        except (OSError, ValueError, TimeoutError, NotImplementedError):
            if execution:
                recovered = await self.execute(
                    "get_code_run_by_request",
                    {"request_id": request_id},
                    request_id=request_id,
                )
                if recovered.get("run_id"):
                    if self.tasks is not None:
                        await self.tasks.bind_run(request_id, recovered["run_id"])
                    await self._stage_result(name, request_id, recovered)
                    return recovered
                return {
                    "error": "sandbox_submission_unknown",
                    "uncertain": True,
                    "retryable": False,
                    "request_id": request_id,
                }
            return {
                "error": "sandbox_unavailable",
                "retryable": False,
                "uncertain": submitted
                and (
                    name.startswith("acp_")
                    or name
                    in (
                        (SANDBOX_TOOLS - READ_TOOLS)
                        | (WORKSPACE_TOOLS - WORKSPACE_READ_TOOLS)
                        | {"workspace_copy", "workspace_publish"}
                    )
                ),
            }

    async def _native_terminal(
        self,
        name: str,
        args: dict[str, Any],
        request_id: str,
        source: dict[str, Any] | None,
    ) -> dict[str, Any]:
        assert self.local_devices is not None
        if name == "terminal_control":
            if args["action"] not in {"cancel", "close"}:
                return {"error": "native_agent_control_requires_external_agent", "retryable": False}
            actor = self.local_devices.actor(source)
            operation = await self.local_devices.agents.repository.operation(args["run_id"], actor)
            action = "detach" if operation.action == "watch" else "cancel"
        else:
            action = "get"
        return await self.local_devices.agents.execute(
            {
                "action": action,
                **{key: args[key] for key in ("run_id", "request_id", "cursor") if key in args},
            },
            request_id=request_id,
            source=source,
        )

    async def _stage_result(self, name: str, request_id: str, result: dict[str, Any]) -> None:
        if (
            self.tasks is None
            or not result.get("run_id")
            or (name not in SANDBOX_TOOLS and not name.startswith("acp_"))
        ):
            return
        # Cursor views must stage the same terminal receipt as the durable outbox.
        if isinstance(result.get("completion"), dict):
            result = result["completion"]
        if result.get("pending") is not False or result.get("status") not in {
            "succeeded",
            "failed",
            "cancelled",
        }:
            return
        row = (
            await self.tasks.get(request_id)
            if name == "terminal_exec"
            else await self.tasks.by_run(str(result.get("run_id")))
        )
        if row is None:
            return
        await self.tasks.receive(
            {"request_id": row.request_id, "run_id": result["run_id"], "result": result}
        )
        from yuki.runtime.execution_receipts import current_receipts
        from yuki.runtime.work_activation import current_work_control

        receipts = current_receipts.get()
        control = current_work_control.get()
        if receipts is not None and control is not None and control.current is not None:
            if json.loads(row.source_json).get("work_id") == control.current["id"]:
                receipts.staged[row.request_id] = self.tasks
