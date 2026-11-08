"""Personal-device terminals and byte transfer over the original Connector connection."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import func, select, update

from yuki.client_api.db_models import ClientDeviceModel
from yuki.client_api.errors import ClientError
from yuki.external_agents.transport import DeviceOffline
from yuki.identity.credentials import require_person
from yuki.identity.person_identity import person_family_condition, same_person_id
from yuki.sandbox.db_models import SandboxTaskRunModel
from yuki.sandbox.task_repository import canonical_json
from yuki.workspace.store import WorkspaceError

if TYPE_CHECKING:
    from yuki.external_agents.service import ExternalAgentService
    from yuki.external_agents.transport import DeviceConnection


def terminal_target(row: SandboxTaskRunModel) -> dict[str, Any] | None:
    return cast(dict[str, Any] | None, json.loads(row.source_json).get("terminal_target"))


class LocalDeviceService:
    def __init__(self, agents: ExternalAgentService) -> None:
        self.agents = agents
        self.database = agents.database
        self.tasks = agents.tasks

    async def authorize(self, device_id: str, actor: str) -> None:
        async with self.database.read_session() as reader:
            principal = await self.agents.identity.require_device(reader, device_id)
            await require_person(reader, actor)
            if not await same_person_id(reader, principal.source_person_id, actor):
                raise ClientError("not_found")

    async def connection(self, device_id: str, actor: str) -> DeviceConnection:
        await self.authorize(device_id, actor)
        return self.agents.connections.get(device_id)

    @staticmethod
    def actor(source: dict[str, Any] | None) -> str:
        if source is None or not isinstance(source.get("actor_person_id"), str):
            raise ValueError("local_device_person_source_required")
        return cast(str, source["actor_person_id"])

    async def status(self, device_id: str, actor: str) -> dict[str, Any]:
        connection = await self.connection(device_id, actor)
        result = await connection.request(
            {
                "type": "command",
                "resource": "terminal",
                "action": "status",
                "id": f"device-status:{uuid4()}",
                "arguments": {},
            }
        )
        connection.device_capabilities = {
            key: result[key]
            for key in ("full_access", "platform", "shell", "tty", "interrupt_supported")
            if key in result
        }
        return {"device_id": device_id, **result}

    async def original_run(self, run_id: str, actor: str) -> SandboxTaskRunModel:
        row = await self.tasks.by_run(run_id)
        target = terminal_target(row) if row is not None else None
        if row is None or target is None:
            raise ClientError("not_found")
        await self.authorize_run(row, actor)
        return row

    async def authorize_run(self, row: SandboxTaskRunModel, actor: str) -> None:
        source = json.loads(row.source_json)
        async with self.database.read_session() as reader:
            await require_person(reader, actor)
            device = await reader.get(ClientDeviceModel, source["terminal_target"]["device_id"])
            if (
                device is None
                or device.purpose != "connector"
                or not await same_person_id(reader, device.person_id, actor)
                or not await same_person_id(reader, source["actor_person_id"], actor)
            ):
                raise ClientError("not_found")

    async def _record(self, row: SandboxTaskRunModel, result: dict[str, Any]) -> None:
        completion = result.get("completion")
        if isinstance(completion, dict):
            if row.completion_json == canonical_json(completion, limit=240000):
                return
            await self.tasks.receive(
                {"request_id": row.request_id, "run_id": row.run_id, "result": completion}
            )
            return
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
        if row.status == "completed" or row.progress_json == encoded:
            return
        async with self.database.immediate_session() as writer:
            await writer.execute(
                update(SandboxTaskRunModel)
                .where(
                    SandboxTaskRunModel.request_id == row.request_id,
                    SandboxTaskRunModel.status == "waiting",
                    SandboxTaskRunModel.progress_json != encoded,
                )
                .values(progress_json=encoded, updated_at=datetime.now(UTC))
            )

    @staticmethod
    def unknown(row: SandboxTaskRunModel, reason: str) -> dict[str, Any]:
        return {
            "request_id": row.request_id,
            "run_id": row.run_id,
            "status": "unknown",
            "pending": True,
            "uncertain": True,
            "error": reason,
            "retryable": False,
        }

    @classmethod
    def unavailable(cls, row: SandboxTaskRunModel, reason: str) -> dict[str, Any]:
        if row.status == "completed":
            target = terminal_target(row)
            assert target is not None
            return {
                **json.loads(row.completion_json or "{}"),
                "request_id": row.request_id,
                "device_id": target["device_id"],
                "output_unavailable": True,
                "observation_error": reason,
            }
        return cls.unknown(row, reason)

    async def execute(
        self,
        name: str,
        args: dict[str, Any],
        *,
        request_id: str,
        source: dict[str, Any] | None,
    ) -> dict[str, Any]:
        actor = self.actor(source)
        command: dict[str, Any]
        if name == "environment_status":
            return await self.status(args["device_id"], actor)
        if name == "terminal_exec":
            device_id = str(args["device_id"])
            await self.authorize(device_id, actor)
            existing = await self.tasks.get(request_id)
            target = {"device_id": device_id, "command": args["command"], "cwd": args.get("cwd")}
            run_id = str(uuid5(NAMESPACE_URL, f"yuki:local-terminal:{request_id}"))
            # ``name`` is the Manager RPC method; the original row payload keeps it.
            row = await self.tasks.prepare(
                request_id,
                {"tool": name, "arguments": args},
                {**cast(dict[str, Any], source), "terminal_target": target},
                run_id=run_id,
            )
            if row.status == "completed":
                return cast(dict[str, Any], json.loads(row.completion_json or "{}"))
            command = (
                {
                    "type": "command",
                    "resource": "terminal",
                    "action": "get",
                    "id": f"terminal-reconcile:{uuid4()}",
                    "run_id": run_id,
                    "arguments": {"operation_id": request_id},
                }
                if existing is not None
                else {
                    "type": "command",
                    "resource": "terminal",
                    "action": "exec",
                    "id": request_id,
                    "run_id": run_id,
                    "arguments": {key: value for key, value in args.items() if key != "device_id"},
                }
            )
        else:
            candidate = (
                await self.tasks.get(str(args["request_id"]))
                if name == "get_code_run_by_request"
                else await self.tasks.by_run(str(args["run_id"]))
            )
            original_target = terminal_target(candidate) if candidate is not None else None
            if candidate is None or original_target is None:
                raise ClientError("not_found")
            row = candidate
            target = original_target
            await self.authorize_run(row, actor)
            try:
                await self.authorize(target["device_id"], actor)
            except ClientError as authorization_error:
                if name == "terminal_control" or authorization_error.code != "unauthenticated":
                    raise
                await self.authorize_run(row, actor)
                return self.unavailable(row, "device_authorization_ended")
            if name == "terminal_control":
                command = {
                    "type": "command",
                    "resource": "terminal",
                    "action": args["action"],
                    "id": request_id,
                    "run_id": row.run_id,
                    "arguments": {"text": args.get("text", "")},
                }
            elif name == "get_code_run_by_request":
                command = {
                    "type": "command",
                    "resource": "terminal",
                    "action": "get",
                    "id": request_id,
                    "run_id": row.run_id,
                    "arguments": {"operation_id": row.request_id},
                }
            else:
                command = {
                    "type": "command",
                    "resource": "terminal",
                    "action": "read",
                    "id": request_id,
                    "run_id": row.run_id,
                    "arguments": {
                        key: args[key] for key in ("cursor", "wait_seconds") if key in args
                    },
                }
        try:
            connection = self.agents.connections.get(target["device_id"])
            result = await connection.request(command)
        except (DeviceOffline, OSError) as exc:
            if name != "terminal_control":
                return self.unavailable(row, str(exc))
            return self.unknown(row, str(exc))
        if result.get("status") == "not_found":
            return self.unknown(row, "original_terminal_not_confirmed")
        if result.get("run_id") not in (None, row.run_id):
            raise ValueError("local_terminal_run_mismatch")
        error = result.get("error")
        if error and (
            result.get("executed") is False
            or (isinstance(error, dict) and error.get("executed") is False)
        ):
            code = error["code"] if isinstance(error, dict) else str(error)
            result = {
                **result,
                "status": "failed",
                "pending": False,
                "error": code,
                "error_details": error,
            }
            if name == "terminal_exec" or command["action"] == "get":
                result["completion"] = {
                    "run_id": row.run_id,
                    "status": "failed",
                    "pending": False,
                    "executed": False,
                    "error": code,
                }
        if result.get("status") == "unknown":
            result = {**result, "pending": True, "uncertain": True}
        if name in {"terminal_exec", "get_code_run_by_request"} or isinstance(
            result.get("completion"), dict
        ):
            await self._record(row, result)
        return {
            "device_id": target["device_id"],
            "request_id": row.request_id,
            "run_id": row.run_id,
            **result,
        }

    async def receive(self, device_id: str, event: dict[str, Any]) -> None:
        row = await self.tasks.get(event["request_id"])
        target = terminal_target(row) if row is not None else None
        if target is None or target["device_id"] != device_id:
            raise ValueError("local_terminal_event_device_mismatch")
        if row is None or row.run_id != event["run_id"]:
            raise ValueError("local_terminal_event_run_mismatch")
        await self.tasks.receive(event)

    @staticmethod
    def view(row: SandboxTaskRunModel) -> dict[str, Any]:
        target = terminal_target(row)
        assert target is not None
        receipt = json.loads(row.completion_json or row.progress_json)
        return {
            "request_id": row.request_id,
            "run_id": row.run_id,
            **target,
            "status": receipt.get("status", "unknown"),
            "pending": receipt.get("pending", row.status == "waiting"),
            "uncertain": receipt.get("uncertain", not bool(receipt)),
            **{key: receipt[key] for key in ("exit_code", "error") if key in receipt},
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }

    async def list_runs(
        self,
        actor: str,
        *,
        device_id: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        target = func.json_extract(SandboxTaskRunModel.source_json, "$.terminal_target.device_id")
        async with self.database.read_session() as reader:
            person = await require_person(reader, actor)
            if device_id is not None:
                device = await reader.get(ClientDeviceModel, device_id)
                if (
                    device is None
                    or device.purpose != "connector"
                    or not await same_person_id(reader, device.person_id, actor)
                ):
                    raise ClientError("not_found")
                target_condition = target == device_id
            else:
                devices = select(ClientDeviceModel.id).where(
                    ClientDeviceModel.purpose == "connector",
                    person_family_condition(ClientDeviceModel.person_id, person),
                )
                target_condition = target.in_(devices)
            statement = select(SandboxTaskRunModel).where(
                target_condition,
                person_family_condition(
                    func.json_extract(SandboxTaskRunModel.source_json, "$.actor_person_id"), person
                ),
            )
            if cursor is not None:
                statement = statement.where(SandboxTaskRunModel.request_id > cursor)
            rows = list(
                await reader.scalars(
                    statement.order_by(SandboxTaskRunModel.request_id).limit(limit + 1)
                )
            )
        return {
            "items": [self.view(row) for row in rows[:limit]],
            "next_cursor": rows[limit - 1].request_id if len(rows) > limit else None,
        }

    async def read(self, actor: str, run_id: str, cursor: int = 0) -> dict[str, Any]:
        result = await self.execute(
            "terminal_read",
            {"run_id": run_id, "cursor": cursor},
            request_id=f"desktop-terminal-read:{uuid4()}",
            source={"actor_person_id": actor},
        )
        row = await self.tasks.by_run(run_id)
        assert row is not None
        return {**self.view(row), **result}

    async def control(
        self,
        actor: str,
        run_id: str,
        request_id: str,
        action: str,
    ) -> dict[str, Any]:
        try:
            result = await self.execute(
                "terminal_control",
                {"run_id": run_id, "action": action},
                request_id=f"device-control:{actor}:{request_id}",
                source={"actor_person_id": actor},
            )
        except ValueError as exc:
            if str(exc) == "connector_request_idempotency_conflict":
                raise ClientError("idempotency_conflict") from None
            raise
        return {**result, "request_id": request_id, "run_id": run_id}

    async def control_receipt(
        self,
        actor: str,
        run_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        row = await self.original_run(run_id, actor)
        target = terminal_target(row)
        assert target is not None

        try:
            connection = await self.connection(target["device_id"], actor)
            result = await connection.request(
                {
                    "type": "command",
                    "resource": "terminal",
                    "action": "get",
                    "id": f"control-read:{uuid4()}",
                    "run_id": run_id,
                    "arguments": {"operation_id": f"device-control:{actor}:{request_id}"},
                }
            )
        except (DeviceOffline, OSError):
            result = {
                "status": "unknown",
                "pending": True,
                "uncertain": True,
                "error": "device_offline",
            }
        except ClientError as error:
            if error.code != "unauthenticated":
                raise
            await self.authorize_run(row, actor)
            result = {
                "status": "unknown",
                "pending": True,
                "uncertain": True,
                "error": "device_authorization_ended",
            }
        if result.get("status") == "not_found":
            result = {
                **result,
                "status": "unknown",
                "pending": True,
                "uncertain": True,
                "error": "original_control_not_confirmed",
            }
        if result.get("run_id") not in (None, run_id):
            raise ClientError("not_found")
        return {**result, "request_id": request_id, "run_id": run_id}

    async def file_request(
        self,
        device_id: str,
        actor: str,
        action: str,
        arguments: dict[str, Any],
        *,
        identity: str,
    ) -> dict[str, Any]:
        try:
            connection = await self.connection(device_id, actor)
            result = await connection.request(
                {
                    "type": "command",
                    "resource": "file",
                    "action": action,
                    "id": identity,
                    "arguments": arguments,
                }
            )
        except (DeviceOffline, OSError):
            if action not in {"write", "get"}:
                raise WorkspaceError("device_file_read_unavailable") from None
            return {
                "status": "unknown",
                "pending": True,
                "uncertain": True,
                "error": "device_file_write_not_confirmed",
                "request_id": identity,
                "transfer_id": arguments["transfer_id"],
                "path": arguments["path"],
            }
        if result.get("error"):
            error = result["error"]
            raise WorkspaceError(str(error.get("code") if isinstance(error, dict) else error))
        if result.get("status") == "unknown":
            if action not in {"write", "get"}:
                raise WorkspaceError("device_file_read_unavailable")
            return {**result, "pending": True, "uncertain": True}
        return result

    @asynccontextmanager
    async def download(
        self,
        device_id: str,
        actor: str,
        path: str,
        *,
        request_id: str,
        expected_version: str | None = None,
        max_bytes: int | None = None,
    ) -> AsyncIterator[tuple[Path, dict[str, Any]]]:
        metadata = await self.file_request(
            device_id,
            actor,
            "stat",
            {"path": path},
            identity=f"{request_id}:stat",
        )
        transfer_id = metadata["transfer_id"]
        temporary = tempfile.TemporaryDirectory(prefix="yuki-device-file-")
        local = Path(temporary.name) / "content"
        digest = hashlib.sha256()
        try:
            if max_bytes is not None and metadata["size"] > max_bytes:
                raise WorkspaceError("workspace_media_too_large")
            if expected_version is not None and expected_version not in (
                metadata["version"],
                metadata["sha256"],
            ):
                raise WorkspaceError("version_conflict")
            offset = 0
            with local.open("wb") as output:
                while True:
                    part = await self.file_request(
                        device_id,
                        actor,
                        "read",
                        {
                            "transfer_id": transfer_id,
                            "version": metadata["version"],
                            "offset": offset,
                        },
                        identity=f"{request_id}:read:{offset}",
                    )
                    data = base64.b64decode(part["data_base64"], validate=True)
                    await asyncio.to_thread(output.write, data)
                    digest.update(data)
                    offset += len(data)
                    if part["eof"]:
                        break
            if offset != metadata["size"] or digest.hexdigest() != metadata["sha256"]:
                raise WorkspaceError("file_changed_during_transfer")
            yield (
                local,
                {
                    **metadata,
                    "device_id": device_id,
                    "name": metadata.get("name") or path.replace("\\", "/").rsplit("/", 1)[-1],
                },
            )
        finally:
            try:
                await self.file_request(
                    device_id,
                    actor,
                    "release",
                    {"transfer_id": transfer_id},
                    identity=f"{request_id}:release",
                )
            except (ConnectionError, OSError, WorkspaceError) as exc:
                logging.getLogger(__name__).warning(
                    "device_file_release_failed transfer=%s category=%s",
                    transfer_id,
                    type(exc).__name__,
                )
            finally:
                temporary.cleanup()

    async def upload(
        self,
        device_id: str,
        actor: str,
        stream: Callable[[int], Awaitable[bytes]],
        size: int,
        sha256: str,
        destination: str,
        *,
        request_id: str,
    ) -> dict[str, Any]:
        """Write one source of known size and hash; the Connector checks the whole file."""
        offset = 0
        while True:
            wanted = min(65536, size - offset)
            data = await stream(wanted)
            if len(data) != wanted:
                raise WorkspaceError("source_stream_incomplete")
            final = offset + len(data) == size
            result = await self.file_request(
                device_id,
                actor,
                "write",
                {
                    "path": destination,
                    "transfer_id": request_id,
                    "offset": offset,
                    "data_base64": base64.b64encode(data).decode(),
                    "final": final,
                    "size": size,
                    "sha256": sha256,
                },
                identity=f"{request_id}:write:{offset}",
            )
            offset += len(data)
            # The Connector folds an earlier part's receipt into its journal state,
            # which keeps status "unknown"; the recorded next offset confirms it.
            if final or result.get("next_offset") != offset:
                return result

    async def upload_result(
        self,
        device_id: str,
        actor: str,
        destination: str,
        *,
        request_id: str,
    ) -> dict[str, Any] | None:
        result = await self.file_request(
            device_id,
            actor,
            "get",
            {"transfer_id": request_id, "path": destination},
            identity=f"{request_id}:get:{uuid4()}",
        )
        return None if result.get("status") == "not_found" else result
