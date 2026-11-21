"""One native-agent application boundary shared by all Main entrypoints."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import String, cast, func, literal, or_, select, update

from yuki.client_api.errors import ClientError
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.external_agents.db_models import (
    AgentEventModel,
    AgentInstanceModel,
    AgentOperationModel,
    AgentSessionModel,
)
from yuki.external_agents.delegation import answer_taken, release_in_effect
from yuki.external_agents.errors import ExternalAgentArgumentError
from yuki.external_agents.identity import ConnectorIdentityService
from yuki.external_agents.repository import (
    CONTROLS,
    AgentRepository,
    CatalogSuperseded,
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

CONNECTOR_PROTOCOL = 5
# Actions that create a Work-owned run. Their device RPC belongs to this service,
# never to the activation: the run, events and completion carry the outcome.
_SUBMITTED = frozenset({"prompt", "queue", "watch"})
# New native input checked against the instance's own declared capabilities.
_DECLARED = frozenset({"prompt", "queue", "watch", "steer"})
# Receipt of an explicit relationship end the device never confirmed.
_RELEASED = {
    "status": "released",
    "detached": True,
    "pending": False,
    # The device never received it; only the server ended the relationship.
    "admitted": False,
    "executed": False,
    "released_without_device": True,
}
_TOOL_ACTION = {"respond": "answer"}
RELEASE_REASON = "已交还给用户，Yuki 不再拥有该会话，无人可答，按交还拒绝"

logger = logging.getLogger(__name__)


def _refusal_code(attempt: AgentOperationModel) -> Any:
    error = json.loads(attempt.result_json or "{}").get("error")
    return error.get("code") if isinstance(error, dict) else error


def _answer_outcome(attempt: AgentOperationModel) -> str:
    """answered: the callback consumed it; gone: the device says no such wait remains;
    pending: no receipt yet; refused: any other receipt (retried, never "answered")."""
    if attempt.result_json is None:
        return "pending"
    result = json.loads(attempt.result_json)
    if result.get("error") is None and result.get("response_submitted") is True:
        return "answered"
    if _refusal_code(attempt) == "request_resolved":
        return "gone"
    return "refused"


def unowned_wait(
    event: AgentEventModel | None, instance: AgentInstanceModel, native_session: str
) -> dict[str, Any] | None:
    """The stored fact of a wait no operation received in this session, or None."""
    if (
        event is None
        or event.operation_id is not None
        or event.kind not in {"permission", "question"}
        or event.device_id != instance.device_id
    ):
        return None
    stored: dict[str, Any] = json.loads(event.payload_json)
    payload = stored.get("payload")
    if (
        stored.get("instance_id") != instance.local_id
        or stored.get("session_id") != native_session
        or not isinstance(payload, dict)
        or payload.get("unowned") is not True
    ):
        return None
    return stored


class ExternalAgentService:
    def __init__(self, database: Database, tasks: SandboxTaskRepository) -> None:
        self.database = database
        self.tasks = tasks
        self.repository = AgentRepository(database)
        self.identity = ConnectorIdentityService(database)
        self.connections = DeviceConnections()
        from yuki.external_agents.local_device import LocalDeviceService

        self.local_devices = LocalDeviceService(self)
        self._retiring: dict[str, asyncio.Task[None]] = {}
        self._dispatches: dict[str, asyncio.Task[None]] = {}
        self._queries: dict[str, asyncio.Task[None]] = {}
        # #645: one settlement pass per device at a time; a request during a pass
        # reruns it with (write unconfirmed answers again, sessions or None for all).
        # One lock per native session for check-then-write against new input. The
        # stored attempts are the only other state, so a restart loses nothing.
        self._settling: dict[str, asyncio.Task[None]] = {}
        self._settle_requested: dict[str, tuple[bool, set[tuple[str, str]] | None]] = {}
        self._session_locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    async def close(self) -> None:
        # Closing Yuki's transport never sends native cancel or closes a user daemon. The
        # Bot stopping is not the device going offline: no offline fact is recorded and
        # no Work is woken (each reconnect after the restart is ordinary progress).
        errors: list[BaseException] = []
        try:
            await self.connections.close()
        except BaseException as close_error:
            errors.append(close_error)
        # Each owned task's outcome is reported once, even if close() runs again. Tasks
        # created while earlier ones finish are collected too, as drain() does.
        seen: set[asyncio.Task[None]] = set()
        while owned := [
            task
            for task in (
                *self._retiring.values(),
                *self._dispatches.values(),
                *self._queries.values(),
                *self._settling.values(),
            )
            if task not in seen
        ]:
            seen.update(owned)
            self._retiring.clear()
            results = await asyncio.gather(*owned, return_exceptions=True)
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
        async with self.database.read_session() as session:
            await self.identity.require_device(session, device_id)
        await self.connections.attach(connection)
        if not await self.update_catalog(
            device_id,
            connection,
            hello["instances"],
            hello.get("device_capabilities", {}),
            synchronize=True,
        ):
            return
        self.database.commit_hints.publish()
        await self.repository.connection_event(device_id, connection.id, True)
        # A connection may follow a lost write or a restart: settle released sessions
        # and write unconfirmed answers again under their own IDs (#645), in background.
        self._kick_settlement(device_id, None, resend=True)

    async def disconnect(self, connection: DeviceConnection) -> None:
        if self.connections.detach(connection):
            self.database.commit_hints.publish()
            await self.repository.connection_event(connection.device_id, connection.id, False)

    async def update_catalog(
        self,
        device_id: str,
        connection: DeviceConnection,
        instances: list[dict[str, Any]],
        device_capabilities: dict[str, Any] | None = None,
        *,
        synchronize: bool = False,
    ) -> bool:
        if not self.connections.current(connection):
            return False
        try:
            await self.repository.register(
                device_id,
                instances,
                current=lambda: self.connections.current(connection),
                synchronize=synchronize,
            )
        except CatalogSuperseded:
            return False
        if not self.connections.current(connection):
            return False
        connection.instances = {
            item["id"] for item in instances if item.get("status") != "unavailable"
        }
        connection.declared = {
            item["id"]: frozenset(str(action) for action in item["capabilities"])
            for item in instances
            if item["id"] in connection.instances and isinstance(item.get("capabilities"), list)
        }
        if device_capabilities is not None:
            connection.device_capabilities = device_capabilities
        return True

    async def drain(self) -> None:
        """Wait for this process's in-flight device work (shutdown and tests)."""
        # Finished tasks may still be listed until their done-callback runs; only
        # unfinished ones are awaited, so this never spins on completed futures.
        while pending := [
            task
            for task in (
                *self._dispatches.values(),
                *self._queries.values(),
                *self._settling.values(),
            )
            if not task.done()
        ]:
            await asyncio.wait(pending)

    @staticmethod
    def _background(
        tasks: dict[str, asyncio.Task[None]],
        identity: str,
        job: Coroutine[Any, Any, None],
    ) -> None:
        task = asyncio.create_task(job, name=f"external-agent:{identity}")
        tasks[identity] = task

        def finished(done: asyncio.Task[None]) -> None:
            if tasks.get(identity) is done:
                tasks.pop(identity)
            if not done.cancelled() and (error := done.exception()) is not None:
                # The receipt stays as recorded; the original ID remains queryable.
                logger.error(
                    "external_agent_background_failed operation=%s error=%s",
                    identity,
                    type(error).__name__,
                )

        task.add_done_callback(finished)

    def _session_lock(self, device_id: str, command: dict[str, Any]) -> asyncio.Lock:
        key = (device_id, str(command.get("instance_id")), str(command.get("session_id")))
        return self._session_locks.setdefault(key, asyncio.Lock())

    async def _dispatch(
        self, identity: str, connection: DeviceConnection, command: dict[str, Any]
    ) -> None:
        """Write one registered command and record the device's answer to it."""
        if command["action"] in _SUBMITTED and isinstance(command.get("session_id"), str):
            # New input never lands between a release's check and its write (#645).
            async with self._session_lock(connection.device_id, command):
                await self._write(identity, connection, command)
        else:
            await self._write(identity, connection, command)

    async def _write(
        self,
        identity: str,
        connection: DeviceConnection,
        command: dict[str, Any],
        *,
        unconfirmed: bool = False,
    ) -> None:
        try:
            async with self.database.read_session() as session:
                await self.identity.require_device(session, connection.device_id)
            allowed = await self.repository.dispatchable(identity)
        except ClientError:
            allowed = False
        # The connection is checked last, with no await before the write.
        if not allowed or not self.connections.current(connection):
            if unconfirmed:
                # An earlier write of this ID may have run on the device; not writing
                # it again now proves nothing about that (#645).
                return
            # Nothing was written: the source, connection or grant ended first.
            await self.repository.response(
                identity,
                {
                    "error": "source_or_connection_changed_before_dispatch",
                    "executed": False,
                    "admitted": False,
                    "pending": False,
                },
            )
            return
        try:
            # Protocol 5: the device's answer is a retransmitted receipt event settled
            # in receive_events before its ACK, never an RPC reply awaited here.
            await connection.send(command)
        except Exception as error:
            # Whatever ended the write, delivery is unknown; the original ID stays
            # queryable and is never resent.
            logger.info(
                "external_agent_dispatch_unconfirmed operation=%s error=%s",
                identity,
                type(error).__name__,
            )

    async def _settle(self, identity: str, result: dict[str, Any]) -> None:
        """Record one device answer and everything it settles, idempotently.

        A control's target is parsed from the original control command. The target
        is settled before the control's own receipt is written: if the process ends
        in between, the control still has no receipt, so a reconnect reads it back by
        its original ID and settles again while its target is unsettled or its Work
        is live (every settlement step is idempotent); otherwise it stays unknown.
        The receipt and the issuing Work's input commit together (#668).
        """
        async with self.database.read_session() as reader:
            operation = await reader.get(AgentOperationModel, identity)
            assert operation is not None
            target_id = json.loads(operation.command_json).get("arguments", {}).get("operation_id")
            target = (
                await reader.get(AgentOperationModel, target_id)
                if operation.action in CONTROLS and isinstance(target_id, str)
                else None
            )
        if target is not None:
            refusal = result.get("error")
            if (
                operation.action == "detach"
                and isinstance(refusal, dict)
                and refusal.get("code") == "original_operation_not_found"
            ):
                # The device holds no such operation: nothing native to stop. End
                # only the Work relationship and say the device never confirmed it.
                await self._detached(target)
                result = {**_RELEASED, "device_answer": result}
            else:
                await self._control_result(target, operation.action, result)
        await self.repository.response(identity, result)

    def _query_original(self, identity: str, connection: DeviceConnection, local_id: str) -> None:
        if identity in self._dispatches or identity in self._queries:
            return
        self._background(
            self._queries, identity, self._read_original(identity, connection, local_id)
        )

    async def _read_original(
        self, identity: str, connection: DeviceConnection, local_id: str
    ) -> None:
        try:
            result = await connection.request(
                {
                    "type": "command",
                    "id": f"query:{identity}:{uuid4()}",
                    "action": "get",
                    "instance_id": local_id,
                    "arguments": {"operation_id": identity},
                }
            )
        except Exception as error:
            # A failed read records nothing; the persisted receipt stays as it was.
            logger.info(
                "external_agent_query_unanswered operation=%s error=%s",
                identity,
                type(error).__name__,
            )
            return
        status = result.get("status")
        if result.get("reason") == "original_dispatch_not_confirmed":
            async with self.database.read_session() as reader:
                operation = await reader.get(AgentOperationModel, identity)
            assert operation is not None
            if operation.action in CONTROLS:
                # The device journal began this control and has no answer yet. A control
                # has no later events, so this placeholder is not its receipt: it stays
                # unanswered and is read back again (reconnect or an explicit query).
                connection.reconciliation[identity] = "device_in_progress"
                return
        if isinstance(status, str) and status != "not_found":
            connection.reconciliation.pop(identity, None)
            await self._settle(identity, result)
        else:
            # Absence of a journal result never authorizes another dispatch.
            connection.reconciliation[identity] = status or "query_failed"

    async def _connection(
        self, instance: AgentInstanceModel, *, original: bool = False
    ) -> DeviceConnection:
        # Recheck the real grant for every native access, including an already-open WSS.
        async with self.database.read_session() as session:
            await self.identity.require_device(session, instance.device_id)
        connection = self.connections.get(instance.device_id)
        if not original and instance.local_id not in connection.instances:
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
            if self.connections.online(device.id):
                devices[device.id]["device_capabilities"] = self.connections.get(
                    device.id
                ).device_capabilities
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
            if instance is not None:
                try:
                    await self.identity.require_device(reader, instance.device_id)
                except ClientError as error:
                    return self._unavailable_view(view, str(error))
                if not self.connections.online(instance.device_id):
                    return self._unavailable_view(view, "device_offline")
                if instance.local_id not in self.connections.get(instance.device_id).instances:
                    return self._unavailable_view(view, "instance_unavailable", device_online=True)
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
                # An explicit turn selects that turn, not the latest turn of a
                # session-wide watch whose receipt happens to be supplied too.
                selected = await self._get(
                    {
                        key: value
                        for key, value in args.items()
                        if key not in {"operation_id", "run_id", "request_id", "cursor"}
                    },
                    request_id,
                    actor,
                )
                return {**selected, "related_operation_id": operation.id}
            view = await self._view(operation)
            if view["pending"]:
                instance = await self.repository.instance(operation.instance_id, actor)
                try:
                    connection = await self._connection(instance, original=True)
                except (DeviceOffline, ClientError) as error:
                    view = self._unavailable_view(view, str(error))
                else:
                    # Return persisted facts now; the device answer lands in the receipt.
                    self._query_original(operation.id, connection, instance.local_id)
                    # What this live connection last answered for it, if it held no record.
                    if (status := connection.reconciliation.get(operation.id)) is not None:
                        view = self._unavailable_view(view, status, device_online=True)
                        view["reconciliation_status"] = status
            events = await self.repository.event_page(operation.id, args.get("cursor", 0))
            return {
                **view,
                "events": events,
                "next_cursor": events[-1]["cursor"] if events else args.get("cursor", 0),
                "query_scope": "operation_receipt",
                "status_scope": "operation",
                "cursor_parameter": "cursor",
                "history_order": "asc",
                "observed_at": datetime.now(UTC).isoformat(),
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
                "target 给 operation_id、session_id 或 execution_id；查看设备与实例时省略 target。",
            )
        named = args.get("instance_id")
        resource = await self.repository.native_session(
            session_id, actor, named if isinstance(named, str) else None
        )
        if args.get("instance_id") not in (None, resource.instance_id):
            raise ValueError("external_agent_target_mismatch")
        session_id = resource.id
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
        if execution is not None and native_execution is None:
            result = {
                **result,
                "status": "unknown",
                "pending": True,
                "query_scope": "execution",
                "status_scope": "execution",
                "reason": "execution_not_confirmed",
            }
        views = [
            value
            for key in ("latest_turn", "selected_turn")
            if isinstance(value := result.get(key), dict)
        ]
        native = result.get("native")
        if isinstance(native, dict) and isinstance(native.get("turns"), list):
            views.extend(native["turns"])
        mapping = await self.repository.remember_executions(
            session_id,
            ([native_execution] if isinstance(native_execution, str) else [])
            + [value["id"] for value in views if isinstance(value.get("id"), str)],
            actor,
        )
        for value in views:
            if isinstance(value.get("id"), str):
                value["execution_id"] = mapping[value["id"]]
        execution_id = (
            mapping.get(native_execution)
            if isinstance(native_execution, str)
            else execution.id
            if execution is not None
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
            # Waits no operation received (metadata only); answerable with deny.
            "pending_unowned_requests": await self.repository.unowned_requests(
                instance, resource.native_id
            ),
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
        self,
        args: dict[str, Any],
        request_id: str,
        source: dict[str, Any] | None,
        *,
        owner_work_id: str | None = None,
    ) -> dict[str, Any]:
        """Register one native action. ``owner_work_id`` is set only by trusted callers.

        A delegation, observation or Desktop answer names the record that owns the
        operation; the activation's lease (when there is one) still guards the write.
        """
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
        if owner_work_id is not None:
            async with self.database.read_session() as reader:
                record = (
                    (await reader.execute(select(work).where(work.c.id == owner_work_id)))
                    .mappings()
                    .first()
                )
            if record is None or (record["state"] in TERMINAL and action != "detach"):
                # Ending a relationship (release of a finished delegation) is still allowed;
                # new input to a finished delegation continues it with resume.
                if record is not None and action in {"queue", "steer", "respond"}:
                    raise ExternalAgentArgumentError(
                        "delegation_finished_use_resume",
                        "这个委派已结束；在同一会话继续用 manage_task(action=resume, id, text)。"
                        "本次未派发。",
                    )
                raise ValueError("external_agent_source_obsolete")
            trusted = {
                **trusted,
                "work_id": owner_work_id,
                "conversation_id": record["conversation_id"],
                "generation": record["generation"],
            }
        elif action in {"prompt", "queue", "watch"}:
            if control is None or control.current is None:
                raise ExternalAgentArgumentError(
                    "accept_work_before_execution",
                    "先用 manage_task(action=start) 登记工作，"
                    "再调用 prompt、queue 或 watch。本次未派发。",
                )
            trusted = {
                **trusted,
                "work_id": control.current["id"],
                "conversation_id": control.lease.conversation_id,
                "generation": control.lease.generation,
            }
        elif control is not None and control.current is not None:
            # A control issued inside a Work reports back to that Work. It never owns a
            # run, so it neither blocks the Work's completion nor gains the target.
            trusted = {
                **trusted,
                "work_id": control.current["id"],
                "conversation_id": control.lease.conversation_id,
                "generation": control.lease.generation,
            }
        owned = await self._operation_target(args, actor)
        if owned is not None and owned.action == "watch" and action != "detach":
            # Observation only looks (D6): no input, no answer (not even deny), no control.
            raise ExternalAgentArgumentError(
                "observe_only",
                "这是观察关系：只看不答，不发送输入、不回答或拒绝权限、不发原生控制；"
                "等待需由用户在本机终端处理。停止观察用 detach。本次未派发。",
            )
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
                "target 给目录中的 instance_id，或能定位实例的会话、执行或原操作 ID。",
            )
        instance = await self._instance(instance_id, actor, args.get("device_id"))
        if action in {"steer", "cancel"} and owned is None and execution is None:
            raise ExternalAgentArgumentError(
                "operation_id_or_execution_id_required",
                "steer、cancel 的 target 用原 operation_id 或目录中的 execution_id。",
            )
        # A wait no operation received is answered on its session, and only with deny.
        unowned_answer = (
            action == "respond"
            and owned is None
            and execution is None
            and native_session is not None
            and isinstance(args.get("session_id"), str)
        )
        if action in {"detach", "respond"} and owned is None and not unowned_answer:
            raise ExternalAgentArgumentError(
                "operation_id_required",
                "answer、detach 的 target 用原 operation_id；execution_id 不能替代。",
            )
        original_control = owned is not None and action in {"detach", "cancel", "steer", "respond"}
        try:
            connection = await self._connection(instance, original=original_control)
        except DeviceOffline:
            if (
                action == "detach"
                and owned is not None
                and owned.run_id is not None
                and owned.detached_at is None
                and json.loads(owned.result_json or "{}").get("pending") is not False
            ):
                # An explicit end of the Work relationship cannot wait for the device.
                return await self._release(owned, instance, args, request_id, actor, trusted)
            return {"error": "device_offline", "executed": False, "pending": False}
        target = owned
        if target is None and execution is not None and action in {"cancel", "steer"}:
            matches = await self.repository.execution_operations(
                instance.id, execution.session_id, execution.native_id, actor
            )
            if len(matches) > 1:
                raise ExternalAgentArgumentError(
                    "external_agent_execution_ambiguous",
                    "该 execution_id 对应多个原操作，target 改用 operation_id。",
                )
            target = matches[0] if matches else None
        # Only the declaration this live connection made for an available instance
        # refuses early. Stored catalogs may be stale; controls of an original operation
        # (selected by operation_id or by its execution) and undeclared instances still
        # go to the device, which decides.
        declared = connection.declared.get(instance.local_id)
        if "scope" in args and "scope" not in (declared or frozenset()):
            # A fixed scope is never dropped silently (D2): an older connector would
            # run the delegation without it.
            raise ExternalAgentArgumentError(
                "connector_outdated",
                "这台电脑的连接器不支持委派范围，请先在 Desktop 更新连接器。本次未派发。",
            )
        if (
            action in _DECLARED
            and not original_control
            and not (action == "steer" and target is not None)
            and instance.local_id in connection.instances
            and declared
            and action not in declared
        ):
            available = "、".join(
                _TOOL_ACTION.get(item, item) for item in sorted(declared - {"list", "get"})
            )
            raise ExternalAgentArgumentError(
                "unsupported_action",
                f"该实例声明不支持 {action}；可用动作：{available}。本次未派发。",
            )
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
        if action in {"prompt", "queue"}:
            # New input starts or joins whatever turn the native owner decides. A selected
            # operation or execution only locates the session; its turn is never pre-bound
            # to this new operation, which would claim another operation's (or Work's) turn.
            native_execution = None
        if target is not None:
            arguments["operation_id"] = target.id
            if target.action == "followup":
                # A turn the native owner started itself: the device locates it by owner.
                arguments["owner_operation_id"] = json.loads(target.command_json)[
                    "owner_operation_id"
                ]
        if action == "steer":
            arguments["expected_execution_id"] = native_execution
        if action == "respond":
            try:
                permission_key = int(args["permission_id"])
            except (KeyError, ValueError, TypeError) as error:
                raise ExternalAgentArgumentError(
                    "permission_id_required",
                    "reply_to 用卡片或时间线里的 permission_id，不用 Agent 自己的请求 ID。",
                ) from error
            async with self.database.read_session() as session:
                event = await session.get(AgentEventModel, permission_key)
                if unowned_answer:
                    assert native_session is not None
                    if unowned_wait(event, instance, native_session.native_id) is None:
                        raise ExternalAgentArgumentError(
                            "external_agent_question_not_found",
                            "用该会话 pending_unowned_requests 中的 permission_id。",
                        )
                    if args.get("option_id") != "deny" or "response" in args:
                        # Its content never left the computer; allowing it blind would
                        # approve an unseen command.
                        raise ExternalAgentArgumentError(
                            "deny_only",
                            "无归属的等待只上传了元信息，只能用 deny 解除；"
                            "要允许，请在本机 Claude 中查看后处理。",
                        )
                elif owned is None or event is None or event.operation_id != owned.id:
                    raise ExternalAgentArgumentError(
                        "external_agent_question_not_found",
                        "reply_to 用这个子任务卡片或时间线中权限请求或提问的 permission_id。",
                    )
                assert event is not None
                event_data = json.loads(event.payload_json)
                if event.kind not in {"permission", "question"}:
                    raise ExternalAgentArgumentError(
                        "external_agent_question_not_found",
                        "reply_to 必须是这个子任务的权限请求或提问的 permission_id。",
                    )
                native_question = event_data["payload"]["native_request_id"]
                if owned is not None:
                    earlier = await session.scalar(
                        select(AgentOperationModel.id).where(
                            AgentOperationModel.instance_id == owned.instance_id,
                            AgentOperationModel.action == "respond",
                            AgentOperationModel.id != request_id,
                            # Codex request ids are integers; compare their text form.
                            cast(
                                func.json_extract(
                                    AgentOperationModel.command_json,
                                    "$.arguments.native_request_id",
                                ),
                                String,
                            )
                            == str(native_question),
                            func.json_extract(
                                AgentOperationModel.command_json, "$.arguments.operation_id"
                            )
                            == owned.id,
                            answer_taken(),
                        )
                    )
                    if earlier is not None:
                        # The native request is consumed once: the first answer wins.
                        raise ExternalAgentArgumentError(
                            "request_already_answered",
                            "这个请求已经有人回答（先答者生效），本次未派发；"
                            "结果见该子任务的时间线。",
                        )
                question_execution = event_data.get("execution_id")
                if execution is not None and execution.native_id != question_execution:
                    raise ValueError("external_agent_target_mismatch")
                native_execution = question_execution
            arguments["permission_id"] = native_question
            arguments["native_request_id"] = native_question
            arguments["connection_id"] = event_data["payload"].get("connection_id")
        if action == "cancel" and target is not None:
            queued = json.loads(target.result_json or "{}").get("queued_submission_id")
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
            # A fork's receipt names its new native session; the original is only its source.
            session_id=None if args.get("fork") else session_id,
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
                connection = await self._connection(instance, original=original_control)
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
            # The activation never waits on the device: writing, the answer and what it
            # settles belong to this service; the receipt and inputs carry the outcome.
            self._background(
                self._dispatches,
                operation.id,
                self._dispatch(operation.id, connection, command),
            )
        return await self._view(operation)

    async def release(
        self,
        work_id: str,
        source: dict[str, Any],
        *,
        request_id: str,
        close_owner: bool,
    ) -> list[dict[str, Any]]:
        """End Yuki's relationship with every live operation of one record.

        Each operation is detached (offline: released without the device). With
        ``close_owner`` a connector declaring ``release`` also closes the native
        process Yuki opened for it once the current turn ends, so the user can take
        the session back without a fork (G9). Nothing native is cancelled.
        """
        async with self.database.read_session() as reader:
            live = list(
                await reader.scalars(
                    select(AgentOperationModel)
                    .where(
                        # Finished operations too: an idle owner still holds the session.
                        AgentOperationModel.work_id == work_id,
                        AgentOperationModel.run_id.is_not(None),
                        AgentOperationModel.detached_at.is_(None),
                    )
                    .order_by(AgentOperationModel.created_at, AgentOperationModel.id)
                )
            )
        results = []
        for operation in live:
            arguments: dict[str, Any] = {"action": "detach", "operation_id": operation.id}
            if close_owner:
                arguments["release"] = True
            results.append(
                await self.execute(
                    arguments,
                    f"{request_id}:{operation.id}"[:256],
                    source,
                    owner_work_id=work_id,
                )
            )
            # The relationship ends now, a server fact; the device's detach follows in
            # the background, so no later cleanup mistakes it for work to cancel and no
            # later self-started turn is attributed to it.
            await self._detached(operation)
        if live:
            async with self.database.immediate_session() as writer:
                await writer.execute(
                    update(AgentOperationModel)
                    .where(
                        AgentOperationModel.id.in_([item.id for item in live]),
                        AgentOperationModel.detached_at.is_(None),
                    )
                    .values(detached_at=datetime.now(UTC))
                )
        return results

    async def _release(
        self,
        target: AgentOperationModel,
        instance: AgentInstanceModel,
        args: dict[str, Any],
        request_id: str,
        actor: str,
        source: dict[str, Any],
    ) -> dict[str, Any]:
        """End only the Work relationship while the device is offline.

        Nothing native is stopped or claimed: the original receipt keeps its own
        admitted/unknown facts, and later device events for it are stored but no
        longer projected into the Work. (A device that reports no record of the
        original operation is settled the same way in `_settle`.)
        """
        operation, _ = await self.repository.prepare(
            identity=request_id,
            instance_id=target.instance_id,
            actor=actor,
            action="detach",
            arguments=args,
            command={
                "type": "command",
                "id": request_id,
                "action": "detach",
                "instance_id": instance.local_id,
                "arguments": {
                    "operation_id": target.id,
                    **({"release": True} if args.get("release") else {}),
                },
            },
            source=source,
            session_id=target.session_id,
            native_execution_id=None,
            lease=None,
        )
        # Target first, then this receipt: see `_settle` for the restart argument.
        await self._detached(target)
        operation = await self.repository.response(operation.id, dict(_RELEASED))
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
                    # Ending the relationship never claims the original input was admitted.
                    "$.admitted",
                    func.json(func.coalesce(original.op("->")("$.admitted"), "false")),
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
                        "admitted": json.loads(row.result_json or "{}").get("admitted") is True,
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
        """Commit native facts and receipts; the caller ACKs only after this returns."""
        facts = [event for event in events if event.get("kind") != "receipt"]
        if facts:
            await self.repository.receive_events(device_id, facts)
        unowned = {
            (str(fact.get("instance_id")), str(fact.get("session_id")))
            for fact in facts
            if fact.get("kind") in {"permission", "question"}
            and fact.get("operation_id") is None
            and isinstance(fact.get("payload"), dict)
            and fact["payload"].get("unowned") is True
        }
        if unowned:
            # A released session's wait is settled by its release, in the background,
            # never on the ACK path (#645).
            self._kick_settlement(device_id, unowned)
        for receipt in (event for event in events if event.get("kind") == "receipt"):
            identity = receipt.get("operation_id")
            result = (receipt.get("payload") or {}).get("result")
            if not isinstance(identity, str) or not isinstance(result, dict):
                raise ValueError("invalid_external_agent_receipt")
            async with self.database.read_session() as reader:
                operation = await reader.get(AgentOperationModel, identity)
                instance = (
                    await reader.get(AgentInstanceModel, operation.instance_id)
                    if operation is not None
                    else None
                )
            if (
                instance is None
                or instance.device_id != device_id
                or instance.local_id != receipt.get("instance_id")
            ):
                # A device settles only its own original operations. The receipt is
                # acknowledged and dropped: refusing the ACK would make the device resend
                # it forever and stay offline.
                logger.warning(
                    "external_agent_receipt_mismatch device=%s operation=%s", device_id, identity
                )
                continue
            await self._settle(identity, result)

    def _kick_settlement(
        self, device_id: str, sessions: set[tuple[str, str]] | None, *, resend: bool = False
    ) -> None:
        """One background settlement pass per device; a request during a pass reruns it.

        ``sessions`` are (instance local ID, native session ID) pairs with a new wait;
        None (a connection) means every session of the device.
        """
        key = f"release:{device_id}"
        previous = self._settle_requested.get(key)
        if previous is not None:
            resend = resend or previous[0]
            sessions = None if sessions is None or previous[1] is None else previous[1] | sessions
        self._settle_requested[key] = (resend, sessions)
        running = self._settling.get(key)
        if running is None or running.done():
            self._background(self._settling, key, self._settle_device(device_id, key))

    async def _settle_device(self, device_id: str, key: str) -> None:
        while key in self._settle_requested:
            resend, wanted = self._settle_requested.pop(key)
            query = (
                select(AgentSessionModel.id, AgentSessionModel.native_id, AgentInstanceModel)
                .join(AgentInstanceModel, AgentInstanceModel.id == AgentSessionModel.instance_id)
                .where(AgentInstanceModel.device_id == device_id)
            )
            if wanted is not None:
                query = query.where(AgentSessionModel.native_id.in_({n for _, n in wanted}))
            released = []
            async with self.database.read_session() as reader:
                for session_id, native_session, instance in (await reader.execute(query)).all():
                    if wanted is not None and (instance.local_id, native_session) not in wanted:
                        continue
                    release = await release_in_effect(reader, session_id)
                    if release is not None and release.work_id is not None:
                        released.append((release.work_id, session_id, native_session, instance))
            for work_id, session_id, native_session, instance in released:
                await self._settle_release(
                    device_id, work_id, session_id, native_session, instance, resend
                )

    async def _settle_release(
        self,
        device_id: str,
        work_id: str,
        session_id: str,
        native_session: str,
        instance: AgentInstanceModel,
        resend: bool,
    ) -> None:
        """Deny, on the release's behalf, every wait left in a released session (#645).

        Yuki gave the session back, the user cannot reach a callback inside Yuki's own
        native process, and only metadata left the computer: nobody else can answer
        these waits, and the turn must reach its boundary for the process to close.
        Outcomes are facts: a submitted answer, a native resolution or
        ``request_resolved`` end a wait. Any other refusal says only that no owner can
        take it now; it is tried again, under a new ID, at the next fact (another wait
        in the session, or a reconnect), never on a timer. An answer whose delivery is
        unknown is written again under its original ID on a reconnect (the connector
        runs an ID at most once).
        """
        try:
            connection = self.connections.get(device_id)
        except DeviceOffline:
            return
        lock_key = (device_id, instance.local_id, native_session)
        async with self._session_locks.setdefault(lock_key, asyncio.Lock()):
            async with self.database.read_session() as reader:
                release = await release_in_effect(reader, session_id)
                if release is None or release.work_id != work_id:
                    return  # Taken over or resumed: the holder's own path owns the waits.
                record = (
                    await reader.execute(
                        select(work.c.conversation_id, work.c.generation).where(
                            work.c.id == work_id
                        )
                    )
                ).first()
                attempts = list(
                    await reader.scalars(
                        select(AgentOperationModel)
                        .where(
                            AgentOperationModel.work_id == work_id,
                            AgentOperationModel.action == "respond",
                            AgentOperationModel.id.like(f"release:{work_id}:deny:%"),
                        )
                        .order_by(AgentOperationModel.created_at, AgentOperationModel.id)
                    )
                )
            if record is None:
                return
            by_wait: dict[str, list[AgentOperationModel]] = {}
            for attempt in attempts:
                by_wait.setdefault(attempt.id.split(":")[3], []).append(attempt)
            for wait in await self.repository.unowned_requests(instance, native_session):
                mine = by_wait.get(wait["permission_id"], [])
                if any(_answer_outcome(item) in {"answered", "gone"} for item in mine):
                    continue
                last = mine[-1] if mine else None
                if last is not None and last.result_json is None:
                    if resend:
                        logger.info(
                            "external_agent_release_answer_resent work=%s session=%s wait=%s "
                            "operation=%s",
                            work_id,
                            session_id,
                            wait["permission_id"],
                            last.id,
                        )
                        await self._write(
                            last.id, connection, json.loads(last.command_json), unconfirmed=True
                        )
                    continue
                registered = await self._register_release_answer(
                    work_id,
                    session_id,
                    release,
                    record,
                    instance,
                    native_session,
                    wait,
                    len(mine) + 1,
                )
                if registered is not None:
                    await self._write(
                        registered.id, connection, json.loads(registered.command_json)
                    )

    async def _register_release_answer(
        self,
        work_id: str,
        session_id: str,
        release: AgentOperationModel,
        record: Any,
        instance: AgentInstanceModel,
        native_session: str,
        wait: dict[str, Any],
        attempt: int,
    ) -> AgentOperationModel | None:
        """Register one deny attempt, authorized by the release row it names."""
        async with self.database.read_session() as reader:
            event = await reader.get(AgentEventModel, int(wait["permission_id"]))
        stored = unowned_wait(event, instance, native_session)
        if stored is None:
            return None
        assert event is not None
        payload = stored["payload"]
        native_request = payload["native_request_id"]
        execution = stored.get("execution_id")
        identity = f"release:{work_id}:deny:{event.id}:{attempt}"
        operation, _ = await self.repository.prepare(
            identity=identity,
            instance_id=instance.id,
            actor=release.actor_person_id,
            action="respond",
            arguments={
                "action": "respond",
                "session_id": session_id,
                "permission_id": str(event.id),
                "option_id": "deny",
            },
            command={
                "type": "command",
                "id": identity,
                "action": "respond",
                "instance_id": instance.local_id,
                "arguments": {
                    "option_id": "deny",
                    "permission_id": native_request,
                    "native_request_id": native_request,
                    "connection_id": payload.get("connection_id"),
                },
                "session_id": native_session,
                **({"execution_id": execution} if isinstance(execution, str) else {}),
            },
            source={
                "actor_person_id": release.actor_person_id,
                "work_id": work_id,
                "conversation_id": record.conversation_id,
                "generation": record.generation,
                "settled_by": "release",
                "release_operation_id": release.id,
                "answered_via": "release",
                "answer_reason": RELEASE_REASON,
                "answer_request": {"kind": wait["kind"], "tool": wait["tool"]},
            },
            session_id=session_id,
            native_execution_id=execution if isinstance(execution, str) else None,
            lease=None,
        )
        logger.info(
            "external_agent_release_answer work=%s session=%s wait=%s attempt=%d",
            work_id,
            session_id,
            event.id,
            attempt,
        )
        return operation

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
                connection = await self._connection(instance, original=True)
            except (DeviceOffline, ClientError):
                results.append({"operation_id": row.id, "status": "unknown", "pending": True})
                continue
            # One attempt per connection: a control the source check refused before
            # writing is written again on the next connection, never twice on one.
            identity = f"{operation_id}:{row.id}:{action}:{connection.id}"[:256]
            command = {
                "type": "command",
                "id": identity,
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
            # Registered first, then written: the receipt event settles the target.
            source = {
                key: value
                for key, value in json.loads(row.source_json).items()
                if key not in {"work_id", "generation"}
            }
            control, created = await self.repository.prepare(
                identity=identity,
                instance_id=row.instance_id,
                actor=row.actor_person_id,
                action=action,
                arguments={"action": action, "operation_id": row.id, "source_cleanup": True},
                command=command,
                source={**source, "source_cleanup": operation_id},
                session_id=row.session_id,
                native_execution_id=None,
                lease=None,
            )
            if created:
                self._background(
                    self._dispatches, identity, self._dispatch(identity, connection, command)
                )
            results.append(await self._view(control))
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
        devices = tuple(connection.device_id for connection in self.connections.active())
        if not devices:
            return
        online = func.json_each(json.dumps(devices)).table_valued("value")
        retiring = func.json_each(json.dumps(tuple(self._retiring))).table_valued("value")
        query = (
            select(AgentOperationModel.id)
            .join(work, work.c.id == AgentOperationModel.work_id)
            .join(AgentInstanceModel, AgentInstanceModel.id == AgentOperationModel.instance_id)
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
                AgentOperationModel.id.not_in(select(retiring.c.value)),
                AgentInstanceModel.device_id.in_(select(online.c.value)),
                SandboxTaskRunModel.status == "waiting",
                or_(
                    work.c.state.in_(TERMINAL),
                    func.json_extract(AgentOperationModel.source_json, "$.generation")
                    != CanonicalConversationModel.generation,
                ),
            )
        )

        async def retire(identity: str) -> None:
            await self.cleanup_tasks([identity], f"external-retire:{identity}")

        after: str | None = None
        while True:
            page = query.where(AgentOperationModel.id > after) if after is not None else query
            async with self.database.read_session() as session:
                identities = tuple(
                    await session.scalars(page.order_by(AgentOperationModel.id).limit(128))
                )
            if not identities:
                return
            after = identities[-1]
            for identity in identities:
                if identity not in self._retiring:
                    self._retiring[identity] = asyncio.create_task(
                        retire(identity), name=f"external-agent-retire:{identity}"
                    )
