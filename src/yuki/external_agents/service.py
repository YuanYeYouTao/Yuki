"""One native-agent application boundary shared by all Main entrypoints."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, literal, or_, select, update

from yuki.client_api.errors import ClientError
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.external_agents.db_models import (
    AgentEventModel,
    AgentInstanceModel,
    AgentOperationModel,
    AgentSessionModel,
)
from yuki.external_agents.errors import ExternalAgentArgumentError
from yuki.external_agents.identity import ConnectorIdentityService
from yuki.external_agents.repository import (
    AgentRepository,
    encode,
    session_observation,
)
from yuki.external_agents.transport import DeviceConnection, DeviceConnections, DeviceOffline
from yuki.persistence.database import Database
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_repository import TERMINAL
from yuki.runtime.work_schema_v1 import work
from yuki.sandbox.db_models import SandboxTaskRunModel
from yuki.sandbox.task_repository import SandboxTaskRepository

CONNECTOR_PROTOCOL = 3


class ExternalAgentService:
    def __init__(self, database: Database, tasks: SandboxTaskRepository) -> None:
        self.database = database
        self.tasks = tasks
        self.repository = AgentRepository(database)
        self.identity = ConnectorIdentityService(database)
        self.connections = DeviceConnections()
        self._retiring: dict[str, asyncio.Task[None]] = {}

    async def close(self) -> None:
        # Closing Yuki's transport never sends native cancel or closes a user daemon.
        errors: list[BaseException] = []
        connections = self.connections.active()
        try:
            await self.connections.close()
        except BaseException as close_error:
            errors.append(close_error)
        for connection in connections:
            try:
                await self.repository.connection_event(connection.device_id, connection.id, False)
            except BaseException as close_error:
                errors.append(close_error)
        results = await asyncio.gather(*self._retiring.values(), return_exceptions=True)
        errors.extend(result for result in results if isinstance(result, BaseException))
        if errors:
            cancellation = next(
                (error for error in errors if not isinstance(error, Exception)), None
            )
            if cancellation is not None:
                for error in errors:
                    if error is not cancellation:
                        cancellation.add_note(f"connector shutdown failed: {type(error).__name__}")
                raise cancellation
            raise BaseExceptionGroup("external agent shutdown failed", errors)

    async def connect(
        self, device_id: str, connection: DeviceConnection, hello: dict[str, Any]
    ) -> None:
        if hello.get("protocol_version") != CONNECTOR_PROTOCOL or not isinstance(
            hello.get("instances"), list
        ):
            raise ValueError("unsupported_connector_protocol")
        if hello.get("device_id", device_id) != device_id:
            raise ValueError("external_agent_device_mismatch")
        await self.update_catalog(device_id, connection, hello["instances"])
        await self.connections.attach(connection)
        self.database.commit_hints.publish()
        await self.repository.connection_event(device_id, connection.id, True)

    async def disconnect(self, connection: DeviceConnection) -> None:
        if self.connections.detach(connection):
            self.database.commit_hints.publish()
            await self.repository.connection_event(connection.device_id, connection.id, False)

    async def update_catalog(
        self, device_id: str, connection: DeviceConnection, instances: list[dict[str, Any]]
    ) -> None:
        await self.repository.register(device_id, instances)
        connection.instances = {
            item["id"] for item in instances if item.get("status") != "unavailable"
        }

    async def _connection(
        self, instance: AgentInstanceModel, *, action: str | None = None
    ) -> DeviceConnection:
        # Recheck the real grant for every native access, including an already-open WSS.
        async with self.database.read_session() as session:
            await self.identity.require_device(session, instance.device_id)
        connection = self.connections.get(instance.device_id)
        if action != "detach" and instance.local_id not in connection.instances:
            raise DeviceOffline("instance_unavailable")
        return connection

    async def _instance(
        self, identity: str, actor: str, device_id: str | None
    ) -> AgentInstanceModel:
        instance = await self.repository.instance(identity, actor)
        if device_id not in (None, instance.device_id):
            raise ValueError("external_agent_target_mismatch")
        return instance

    async def catalog(self, actor: str, device_id: str | None = None) -> dict[str, Any]:
        instances = []
        devices: dict[str, dict[str, Any]] = {}
        for instance, device in await self.repository.catalog(actor):
            if device_id is not None and device.id != device_id:
                continue
            devices[device.id] = {
                "id": device.id,
                "label": device.label,
                "online": self.connections.online(device.id),
            }
            if instance is None:
                continue
            available = (
                self.connections.online(device.id)
                and instance.local_id in self.connections.get(device.id).instances
            )
            instances.append(
                {
                    **json.loads(instance.catalog_json),
                    "id": instance.id,
                    "local_id": instance.local_id,
                    "device_id": device.id,
                    "online": available,
                }
            )
        return {"devices": list(devices.values()), "instances": instances}

    async def _view(self, operation: AgentOperationModel) -> dict[str, Any]:
        async with self.database.read_session() as reader:
            view = await self.repository.operation_view(operation, session=reader)
            instance = (
                await reader.get(AgentInstanceModel, operation.instance_id)
                if view.get("pending")
                else None
            )
        if instance is not None and not self.connections.online(instance.device_id):
            return self._unavailable_view(view, "device_offline")
        return view

    @staticmethod
    def _unavailable_view(
        view: dict[str, Any], reason: str, *, device_online: bool = False
    ) -> dict[str, Any]:
        return {
            **view,
            "last_confirmed": view.get("last_confirmed", dict(view)),
            "status": "unknown",
            "uncertain": True,
            "observation_error": reason,
            "device_online": device_online,
        }

    async def _list(self, args: dict[str, Any], request_id: str, actor: str) -> dict[str, Any]:
        identity = args.get("instance_id")
        if not isinstance(identity, str):
            return await self.catalog(actor, args.get("device_id"))
        instance = await self._instance(identity, actor, args.get("device_id"))
        try:
            connection = await self._connection(instance)
        except DeviceOffline:
            return {"error": "device_offline", "instance_id": identity, "executed": False}
        result = await connection.request(
            {
                "type": "command",
                "id": request_id,
                "action": "list",
                "instance_id": instance.local_id,
                "arguments": {"cursor": args.get("native_cursor")},
            }
        )
        rows = result.get("sessions")
        if not isinstance(rows, list):
            return result
        mapping = await self.repository.remember_sessions(
            identity, actor, [row["id"] for row in rows]
        )
        return {
            **result,
            "instance_id": identity,
            "sessions": [{**row, "id": mapping[row["id"]]} for row in rows],
        }

    async def _get(self, args: dict[str, Any], request_id: str, actor: str) -> dict[str, Any]:
        operation = await self._operation_target(args, actor, include_request=True)
        if operation is not None:
            if args.get("device_id") is not None:
                await self._instance(operation.instance_id, actor, args["device_id"])
            if args.get("instance_id") not in (None, operation.instance_id) or args.get(
                "session_id"
            ) not in (None, operation.session_id):
                raise ValueError("external_agent_target_mismatch")
            if isinstance(args.get("execution_id"), str):
                bound_execution = await self.repository.native_execution(
                    args["execution_id"], actor
                )
                if operation.session_id != bound_execution.session_id or (
                    not session_observation(operation)
                    and operation.native_execution_id != bound_execution.native_id
                ):
                    raise ValueError("external_agent_target_mismatch")
            view = await self._view(operation)
            if view["pending"]:
                instance = await self.repository.instance(operation.instance_id, actor)
                try:
                    connection = await self._connection(instance)
                except (DeviceOffline, ClientError) as error:
                    view = self._unavailable_view(view, str(error))
                else:
                    try:
                        result = await connection.request(
                            {
                                "type": "command",
                                "id": request_id,
                                "action": "get",
                                "instance_id": instance.local_id,
                                "arguments": {"operation_id": operation.id},
                            }
                        )
                    except (DeviceOffline, OSError) as error:
                        view = self._unavailable_view(view, str(error))
                    else:
                        # Absence of a journal result never authorizes another dispatch.
                        status = result.get("status")
                        if isinstance(status, str) and status != "not_found":
                            operation = await self.repository.response(operation.id, result)
                            view = await self._view(operation)
                        else:
                            view = self._unavailable_view(
                                view, status or "query_failed", device_online=True
                            )
                            view["uncertain"] = True
                            view["reconciliation_status"] = status or "query_failed"
            events = await self.repository.event_page(operation.id, args.get("cursor", 0))
            return {
                **view,
                "events": events,
                "next_cursor": events[-1]["cursor"] if events else args.get("cursor", 0),
            }
        session_id = args.get("session_id")
        execution = (
            await self.repository.native_execution(args["execution_id"], actor)
            if isinstance(args.get("execution_id"), str)
            else None
        )
        if execution is not None:
            if session_id not in (None, execution.session_id):
                raise ValueError("external_agent_target_mismatch")
            session_id = execution.session_id
        if not isinstance(session_id, str):
            raise ExternalAgentArgumentError(
                "external_agent_resource_required",
                "get 需要 operation_id/run_id 或 session_id/execution_id；设备/实例用 list。",
            )
        resource = await self.repository.native_session(session_id, actor)
        if args.get("instance_id") not in (None, resource.instance_id):
            raise ValueError("external_agent_target_mismatch")
        instance = await self._instance(resource.instance_id, actor, args.get("device_id"))
        connection = await self._connection(instance)
        result = await connection.request(
            {
                "type": "command",
                "id": request_id,
                "action": "get",
                "instance_id": instance.local_id,
                "session_id": resource.native_id,
                **({"execution_id": execution.native_id} if execution is not None else {}),
                "arguments": {
                    "include_history": args.get("include_history", False),
                    "cursor": args.get("native_cursor"),
                },
            }
        )
        native_execution = result.get("execution_id")
        if execution is not None and native_execution not in (None, execution.native_id):
            raise ValueError("external_agent_response_execution_mismatch")
        execution_id = (
            await self.repository.remember_execution(session_id, native_execution)
            if isinstance(native_execution, str)
            else None
        )
        return {
            **result,
            "session_id": session_id,
            "instance_id": instance.id,
            "execution_id": execution_id,
            "native_reference": {
                "session_id": resource.native_id,
                "execution_id": result.get("execution_id"),
            },
        }

    async def _operation_target(
        self, args: dict[str, Any], actor: str, *, include_request: bool = False
    ) -> AgentOperationModel | None:
        operation = None
        for key in ("operation_id", "run_id", *(("request_id",) if include_request else ())):
            if isinstance(args.get(key), str):
                candidate = await self.repository.operation(args[key], actor)
                if operation is not None and operation.id != candidate.id:
                    raise ValueError("external_agent_target_mismatch")
                operation = candidate
        return operation

    async def execute(
        self, args: dict[str, Any], request_id: str, source: dict[str, Any] | None
    ) -> dict[str, Any]:
        action = args.get("action")
        if action not in {
            "list",
            "get",
            "watch",
            "prompt",
            "steer",
            "queue",
            "respond",
            "cancel",
            "detach",
        }:
            raise ValueError("invalid_external_agent_action")
        control = current_work_control.get()
        trusted = {**(source or {}), **(control.source if control is not None else {})}
        actor = trusted.get("actor_person_id")
        if not isinstance(actor, str):
            raise ValueError("external_agent_person_source_required")
        if action == "list":
            return await self._list(args, request_id, actor)
        if action == "get":
            return await self._get(args, request_id, actor)
        # Recover an already-dispatched call before consulting today's mutable native state.
        try:
            original = await self.repository.operation(request_id, actor)
        except ValueError as error:
            if str(error) != "external_agent_not_found":
                raise
        else:
            from hashlib import sha256

            if original.payload_hash != sha256(encode(args).encode()).hexdigest():
                raise ValueError("external_agent_request_conflict")
            return await self._view(original)
        if control is not None:
            await control.validate()
            if not await control.repository.valid(control.lease):
                raise ValueError("external_agent_source_obsolete")
        if action in {"prompt", "queue", "watch"}:
            if control is None or control.current is None:
                raise ExternalAgentArgumentError(
                    "accept_work_before_execution",
                    "先用 task_control(action=accept) 接纳 Work，"
                    "再调用 prompt/queue/watch。本次未派发。",
                )
            trusted = {
                **trusted,
                "work_id": control.current["id"],
                "conversation_id": control.lease.conversation_id,
                "generation": control.lease.generation,
            }
        owned = await self._operation_target(args, actor)
        execution = (
            await self.repository.native_execution(args["execution_id"], actor)
            if isinstance(args.get("execution_id"), str)
            else None
        )
        instance_id = owned.instance_id if owned is not None else args.get("instance_id")
        session_id = owned.session_id if owned is not None else args.get("session_id")
        if execution is not None:
            if owned is not None and (
                owned.session_id != execution.session_id
                or (
                    not session_observation(owned)
                    and owned.native_execution_id != execution.native_id
                )
            ):
                raise ValueError("external_agent_target_mismatch")
            session_id = execution.session_id
        native_session = (
            await self.repository.native_session(session_id, actor)
            if isinstance(session_id, str)
            else None
        )
        if native_session is not None:
            if instance_id is not None and instance_id != native_session.instance_id:
                raise ValueError("external_agent_target_mismatch")
            instance_id = native_session.instance_id
        if args.get("instance_id") is not None and args["instance_id"] != instance_id:
            raise ValueError("external_agent_target_mismatch")
        if args.get("session_id") is not None and args["session_id"] != session_id:
            raise ValueError("external_agent_target_mismatch")
        if not isinstance(instance_id, str):
            raise ExternalAgentArgumentError(
                "instance_id_required",
                "提供目录返回的 instance_id，或能定位实例的原操作/会话引用。",
            )
        instance = await self._instance(instance_id, actor, args.get("device_id"))
        if action in {"steer", "cancel"} and owned is None and execution is None:
            raise ExternalAgentArgumentError(
                "operation_id_or_execution_id_required",
                "steer/cancel 须提供原 operation_id（或 run_id），或目录返回的 execution_id。",
            )
        if action in {"detach", "respond"} and owned is None:
            raise ExternalAgentArgumentError(
                "operation_id_required",
                "respond/detach 需要原 operation_id 或 run_id；execution_id 不能替代。",
            )
        try:
            connection = await self._connection(instance, action=action)
        except DeviceOffline:
            return {"error": "device_offline", "executed": False, "pending": False}
        arguments = {
            key: value
            for key, value in args.items()
            if key
            not in {
                "action",
                "instance_id",
                "device_id",
                "session_id",
                "execution_id",
                "run_id",
                "operation_id",
                "request_id",
                "permission_id",
            }
        }
        native_execution = (
            json.loads(owned.result_json or "{}").get("execution_id")
            if owned is not None and session_observation(owned)
            else owned.native_execution_id
            if owned is not None
            else None
        )
        if execution is not None:
            native_execution = execution.native_id
        if owned is not None:
            arguments["operation_id"] = owned.id
        if action == "steer":
            arguments["expected_execution_id"] = native_execution
        if action == "respond":
            try:
                permission_key = int(args["permission_id"])
            except (KeyError, ValueError, TypeError) as error:
                raise ExternalAgentArgumentError(
                    "permission_id_required",
                    "respond 须使用服务器事件顶层 permission_id，不使用原生请求 ID。",
                ) from error
            async with self.database.read_session() as session:
                event = await session.get(AgentEventModel, permission_key)
                if owned is None or event is None or event.operation_id != owned.id:
                    raise ExternalAgentArgumentError(
                        "external_agent_question_not_found",
                        "get 原 operation_id 的 permission/question 事件，使用顶层 permission_id。",
                    )
                event_data = json.loads(event.payload_json)
                if event.kind not in {"permission", "question"}:
                    raise ExternalAgentArgumentError(
                        "external_agent_question_not_found",
                        "permission_id 必须指向原 operation_id 的 permission/question 事件。",
                    )
                native_question = event_data["payload"]["native_request_id"]
                question_execution = event_data.get("execution_id")
                if execution is not None and execution.native_id != question_execution:
                    raise ValueError("external_agent_target_mismatch")
                native_execution = question_execution
            arguments["permission_id"] = native_question
            arguments["native_request_id"] = native_question
            arguments["connection_id"] = event_data["payload"].get("connection_id")
        if action == "cancel" and owned is not None:
            queued = json.loads(owned.result_json or "{}").get("queued_submission_id")
            if queued is not None:
                arguments["queued_submission_id"] = queued
        command = {
            "type": "command",
            "id": request_id,
            "action": action,
            "instance_id": instance.local_id,
            "arguments": arguments,
            **({"session_id": native_session.native_id} if native_session else {}),
            **({"execution_id": native_execution} if native_execution else {}),
            **(
                {"observation_scope": "execution" if native_execution else "session"}
                if action == "watch"
                else {}
            ),
        }
        operation, created = await self.repository.prepare(
            identity=request_id,
            instance_id=instance_id,
            actor=actor,
            action=action,
            arguments=args,
            command=command,
            source=trusted,
            session_id=session_id,
            native_execution_id=native_execution,
            lease=control.lease if control is not None and trusted.get("work_id") else None,
        )
        if created:
            if control is not None:
                try:
                    await control.validate()
                    if not await control.repository.valid(control.lease):
                        raise ValueError("external_agent_source_obsolete")
                except Exception as error:
                    # No native RPC was sent. Preserve that fact before the source exits.
                    await self.repository.response(
                        operation.id,
                        {
                            "error": "source_changed_before_dispatch",
                            "error_type": type(error).__name__,
                            "executed": False,
                            "admitted": False,
                            "pending": False,
                        },
                    )
                    raise
            try:
                # A queued writer may outlive the authenticated socket snapshot.
                connection = await self._connection(instance, action=action)
            except (DeviceOffline, ClientError):
                operation = await self.repository.response(
                    operation.id,
                    {
                        "error": "device_authorization_or_connection_changed",
                        "executed": False,
                        "admitted": False,
                        "pending": False,
                    },
                )
                return await self._view(operation)
            try:
                result = await connection.request(command)
            except (DeviceOffline, OSError):
                return {**await self._view(operation), "uncertain": True}
            operation = await self.repository.response(operation.id, result)
            if owned is not None:
                await self._control_result(owned, action, result)
        return await self._view(operation)

    async def _control_result(
        self, operation: AgentOperationModel, action: str, result: dict[str, Any]
    ) -> None:
        if (
            result.get("error")
            or result.get("executed") is False
            or result.get("status") == "unknown"
        ):
            return
        if action == "detach":
            await self._detached(operation)
        elif (
            action == "cancel"
            and result.get("pending") is False
            and result.get("status") in {"completed", "succeeded", "cancelled", "interrupted"}
        ):
            await self.repository.response(
                operation.id, {**json.loads(operation.result_json or "{}"), **result}
            )

    async def _detached(self, operation: AgentOperationModel) -> None:
        async with self.database.immediate_session() as session:
            current = (
                await session.execute(
                    select(
                        AgentOperationModel,
                        func.json_type(AgentOperationModel.result_json, "$.pending"),
                    ).where(AgentOperationModel.id == operation.id)
                )
            ).first()
            row, pending_type = current if current is not None else (None, None)
            if row is not None and row.detached_at is None:
                if pending_type == "false":
                    return
                row.detached_at = datetime.now(UTC)
                original = literal(row.result_json or "{}")
                native_status = func.json(func.coalesce(original.op("->")("$.status"), '"unknown"'))
                result_json = func.json_set(
                    original,
                    "$.status",
                    "detached",
                    "$.operation_status",
                    "detached",
                    "$.native_status",
                    native_status,
                    "$.native_pending",
                    func.json(func.coalesce(original.op("->")("$.pending"), "true")),
                    "$.pending",
                    func.json("false"),
                    "$.admitted",
                    func.json("true"),
                    "$.session_id",
                    func.json_extract(original, "$.session_id"),
                    "$.execution_id",
                    func.json_extract(original, "$.execution_id"),
                )
                # Both operation and owned completion derive from this same
                # original row before its result is updated.
                await self.repository._complete(
                    session,
                    row,
                    {
                        "status": "detached",
                        "native_status": native_status,
                        "admitted": True,
                        "session_id": func.json_extract(original, "$.session_id"),
                    },
                    result_json,
                )
                await session.execute(
                    update(AgentOperationModel)
                    .where(AgentOperationModel.id == row.id)
                    .values(result_json=result_json)
                    .execution_options(synchronize_session=False)
                )

    async def receive_events(self, device_id: str, events: list[dict[str, Any]]) -> None:
        await self.repository.receive_events(device_id, events)

    async def cleanup_tasks(
        self, tasks: Any, operation_id: str, *, purge: bool = False
    ) -> list[dict[str, Any]]:
        results = []
        for task in tasks:
            reference = task if isinstance(task, str) else getattr(task, "request_id", None)
            if reference is None and isinstance(task, dict):
                reference = task.get("request_id")
            if not isinstance(reference, str):
                raise ValueError("invalid_external_agent_cleanup_reference")
            async with self.database.read_session() as session:
                row = await session.get(AgentOperationModel, reference)
                resource = (
                    await session.get(AgentSessionModel, row.session_id)
                    if row is not None and row.session_id is not None
                    else None
                )
            if row is None:
                if getattr(task, "kind", None) == "acp_prompt":
                    results.append(await self._retire_legacy(task, operation_id, purge=purge))
                continue
            if row.detached_at is not None:
                continue
            current = await self._view(row)
            if row.action == "watch":
                action = "detach"
            elif not current.get("pending"):
                continue
            else:
                action = "cancel"
            try:
                instance = await self.repository.instance(row.instance_id, row.actor_person_id)
                connection = await self._connection(instance, action=action)
            except (DeviceOffline, ClientError):
                results.append({"operation_id": row.id, "status": "unknown", "pending": True})
                continue
            try:
                result = await connection.request(
                    {
                        "type": "command",
                        "id": f"{operation_id}:{row.id}:{action}",
                        "action": action,
                        "instance_id": instance.local_id,
                        "execution_id": row.native_execution_id,
                        "session_id": resource.native_id if resource is not None else None,
                        "arguments": {
                            "operation_id": row.id,
                            **(
                                {"queued_submission_id": current["queued_submission_id"]}
                                if "queued_submission_id" in current
                                else {}
                            ),
                        },
                    }
                )
            except (DeviceOffline, OSError):
                results.append({"operation_id": row.id, "status": "unknown", "pending": True})
                continue
            results.append({"operation_id": row.id, **result})
            await self._control_result(row, action, result)
        return results

    async def _retire_legacy(self, task: Any, operation_id: str, *, purge: bool) -> dict[str, Any]:
        client = self.database.sandbox_client
        if client is None:
            if purge:
                raise ValueError("external_agent_cleanup_unconfirmed")
            return {"request_id": task.request_id, "status": "unknown"}
        result: dict[str, Any] = {"request_id": task.request_id, "kind": "acp_prompt"}
        if task.run_id:
            result["cancel"] = await client.execute(
                "acp_prompt_cancel",
                {"run_id": task.run_id},
                request_id=f"{operation_id}:{task.request_id}:cancel",
            )
        if purge:
            references = (
                {"peer_id": task.peer_id}
                if task.peer_id
                else {"resource_request_id": task.resource_request_id or f"{task.request_id}:peer"}
            )
            result["close"] = await client.execute(
                "acp_peer_close",
                {**references, "purge": True},
                request_id=f"{operation_id}:{task.request_id}:close",
            )
            if result["close"].get("admitted") is not True:
                raise ValueError("external_agent_cleanup_unconfirmed")
        return result

    async def reconcile(self) -> None:
        """Called by existing maintenance, not a second native-job scheduler."""
        for identity, task in tuple(self._retiring.items()):
            if task.done():
                self._retiring.pop(identity)
                # The existing maintenance loop owns error reporting and retry cadence.
                task.result()
        async with self.database.read_session() as session:
            rows = list(
                await session.scalars(
                    select(AgentOperationModel)
                    .join(work, work.c.id == AgentOperationModel.work_id)
                    .join(
                        SandboxTaskRunModel,
                        SandboxTaskRunModel.request_id == AgentOperationModel.id,
                    )
                    .join(
                        CanonicalConversationModel,
                        CanonicalConversationModel.id == work.c.conversation_id,
                    )
                    .where(
                        AgentOperationModel.run_id.is_not(None),
                        AgentOperationModel.detached_at.is_(None),
                        SandboxTaskRunModel.status == "waiting",
                        or_(
                            work.c.state.in_(TERMINAL),
                            func.json_extract(AgentOperationModel.source_json, "$.generation")
                            != CanonicalConversationModel.generation,
                        ),
                    )
                )
            )
        for row in rows:
            if row.id not in self._retiring:

                async def retire(identity: str) -> None:
                    await self.cleanup_tasks([identity], f"external-retire:{identity}")

                self._retiring[row.id] = asyncio.create_task(
                    retire(row.id), name=f"external-agent-retire:{row.id}"
                )
