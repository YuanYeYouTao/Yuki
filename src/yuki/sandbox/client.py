"""Bounded Unix-socket client and deployment-stable tool contracts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from yuki.domain.messages import ChatTool
from yuki.sandbox.environment_tools import (
    READ_TOOLS,
    SANDBOX_TOOLS,
    environment_tools,
    is_execution,
)
from yuki.workspace.tools import WORKSPACE_READ_TOOLS, WORKSPACE_TOOLS

if TYPE_CHECKING:
    from yuki.sandbox.task_repository import SandboxTaskRepository


def sandbox_tools() -> tuple[ChatTool, ...]:
    return environment_tools()


class SandboxClient:
    def __init__(self, socket: Path, *, tasks: SandboxTaskRepository | None = None) -> None:
        self.socket = socket
        self.tasks = tasks
        self.before_computer_person_forget: Callable[[tuple[str, ...]], Awaitable[None]] | None = (
            None
        )

    @asynccontextmanager
    async def stream(
        self, method: str, args: dict[str, Any]
    ) -> AsyncIterator[tuple[dict[str, Any], asyncio.StreamReader, asyncio.StreamWriter]]:
        if method not in {"display_stream", "files_read", "control_watch"}:
            raise ValueError("unknown_stream_method")
        reader, writer = await asyncio.open_unix_connection(str(self.socket))
        try:
            writer.write(json.dumps({"method": method, "args": args}).encode() + b"\n")
            await writer.drain()
            from yuki.sandbox.acp_peer import read_protocol_line

            header = json.loads(await read_protocol_line(reader))
            if not isinstance(header, dict) or header.get("error"):
                raise ValueError("sandbox_stream_unavailable")
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
        if name == "external_agent":
            from yuki.sandbox.external_agents import execute

            return await execute(self, args, request_id, source)
        execution = is_execution(name, args)
        message = (
            json.dumps({"method": name, "args": args, "request_id": request_id}).encode() + b"\n"
        )
        if not name.startswith("acp_") and len(message) > 262144:
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
            if name == "terminal_exec" and prepared is not None:
                work_id = json.loads(prepared.source_json).get("work_id")
                # Display provenance comes from the already validated original
                # execution source, never a model-supplied environment variable.
                wire_args = {**args, "env": {}}
                if work_id:
                    wire_args["env"] = {"YUKI_WORK_ID": work_id, "YUKI_STEP_LABEL": ""}
                message = (
                    json.dumps(
                        {"method": name, "args": wire_args, "request_id": request_id}
                    ).encode()
                    + b"\n"
                )
        submitted = False
        try:
            observation_wait = args.get("wait_seconds", 0) if name == "terminal_read" else 0
            async with asyncio.timeout(
                None
                if name
                in {
                    "sticker_index",
                    "workspace_publish",
                    "workspace_checkout",
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
                    from yuki.sandbox.acp_peer import read_protocol_line

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
                        | {"workspace_checkout"}
                    )
                ),
            }

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
            if name in {"terminal_exec", "external_agent"}
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

    async def release_computer_for_people(
        self, person_ids: tuple[str, ...], reason: str
    ) -> dict[str, Any]:
        from yuki.sandbox.computer_keys import release_key

        if self.before_computer_person_forget:
            await self.before_computer_person_forget(person_ids)
        snapshot = await self.execute("control_get", {}, request_id=str(uuid4()))
        control = snapshot.get("control", {})
        if control.get("holder") != "human" or control.get("person_id") not in person_ids:
            return snapshot
        key = release_key(control["lease_id"])
        saved = await self.execute("control_request_get", {"key": key}, request_id=key)
        if saved.get("error") != "unknown_request":
            return saved
        return await self.execute(
            "control_set",
            {
                "action": "return",
                "lease_id": control["lease_id"],
                "actor": {"system": True},
                "reason": reason,
            },
            request_id=key,
        )

    async def close_peers_for_requests(
        self, request_ids: Iterable[str], operation_id: str, *, purge: bool = True
    ) -> list[dict[str, Any]]:
        """Hand precise original resource revocations to Manager before deleting their anchors."""
        if self.tasks is None:
            raise ValueError("external_agent_cleanup_unconfirmed")
        closed: dict[str, dict[str, Any]] = {}
        for request_id in request_ids:
            row = await self.tasks.get(request_id)
            if row is None:
                continue
            progress = json.loads(row.progress_json)
            if progress.get("kind") != "acp_prompt":
                continue
            resource = progress.get("resource_request_id", f"{request_id}:peer")
            peer_id = progress.get("peer_id")
            identity = peer_id or resource
            if identity in closed:
                continue
            result = await self.execute(
                "acp_peer_close",
                {
                    **({"peer_id": peer_id} if peer_id else {"resource_request_id": resource}),
                    "purge": purge,
                },
                request_id=sha256(f"{operation_id}:{identity}".encode()).hexdigest(),
            )
            if result.get("admitted") is not True:
                raise ValueError("external_agent_cleanup_unconfirmed")
            closed[identity] = result
        return list(closed.values())
