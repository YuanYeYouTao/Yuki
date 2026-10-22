"""Device resources and original receipts, with short, fact-only writer transactions."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import or_, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.client_api.db_models import ClientDeviceModel
from yuki.client_api.errors import ClientError
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.external_agents.db_models import (
    AgentEventModel,
    AgentExecutionModel,
    AgentInstanceModel,
    AgentOperationModel,
    AgentSessionModel,
)
from yuki.external_agents.identity import ConnectorIdentityService
from yuki.identity.credentials import require_person
from yuki.identity.person_identity import person_family_condition, same_person_id
from yuki.persistence.database import Database
from yuki.runtime.work_repository import TERMINAL, WorkLease, WorkRepository
from yuki.runtime.work_schema_v1 import inputs, work
from yuki.runtime.work_wait import WorkWaitRepository
from yuki.sandbox.db_models import SandboxTaskContinuationModel, SandboxTaskRunModel


def encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def instance_key(device_id: str, local_id: str) -> str:
    return str(uuid5(UUID(device_id), f"agent:{local_id}"))


def session_key(instance_id: str, native_id: str) -> str:
    return str(uuid5(UUID(instance_id), f"session:{native_id}"))


def execution_key(session_id: str, native_id: str) -> str:
    return str(uuid5(UUID(session_id), f"execution:{native_id}"))


class AgentRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.identity = ConnectorIdentityService(database)

    async def authorize(
        self, session: AsyncSession, instance_id: str, actor_person_id: str
    ) -> AgentInstanceModel:
        instance = await session.get(AgentInstanceModel, instance_id)
        if instance is None:
            raise ValueError("external_agent_not_found")
        device = await session.get(ClientDeviceModel, instance.device_id)
        await require_person(session, actor_person_id)
        if device is None or not await same_person_id(session, device.person_id, actor_person_id):
            raise ValueError("external_agent_not_found")
        return instance

    async def instance(self, identity: str, actor: str) -> AgentInstanceModel:
        async with self.database.sessions() as session:
            return await self.authorize(session, identity, actor)

    async def catalog(self, actor: str) -> list[tuple[AgentInstanceModel, ClientDeviceModel]]:
        async with self.database.sessions() as session:
            root = await require_person(session, actor)
            return [
                (instance, device)
                for instance, device in await session.execute(
                    select(AgentInstanceModel, ClientDeviceModel)
                    .join(ClientDeviceModel, AgentInstanceModel.device_id == ClientDeviceModel.id)
                    .where(
                        person_family_condition(ClientDeviceModel.person_id, root),
                        ClientDeviceModel.revoked_at.is_(None),
                        ClientDeviceModel.purpose == "connector",
                    )
                    .order_by(ClientDeviceModel.id, AgentInstanceModel.id)
                )
            ]

    async def register(self, device_id: str, catalog: list[dict[str, Any]]) -> None:
        # Only catalog changes write; connection activity and heartbeat remain in memory.
        prepared = {
            str(item["id"]): encode(
                {
                    key: item[key]
                    for key in ("label", "driver", "capabilities", "status", "error_type")
                    if key in item
                }
            )
            for item in catalog
        }
        async with self.database.sessions() as session:
            await self.identity.require_device(session, device_id)
            existing = {
                row.local_id: row.catalog_json
                for row in await session.scalars(
                    select(AgentInstanceModel).where(AgentInstanceModel.device_id == device_id)
                )
            }
        changes = {key: value for key, value in prepared.items() if existing.get(key) != value}
        if not changes:
            return
        async with self.database.immediate_session() as session:
            await self.identity.require_device(session, device_id)
            for local_id, value in changes.items():
                statement = insert(AgentInstanceModel).values(
                    id=instance_key(device_id, local_id),
                    device_id=device_id,
                    local_id=local_id,
                    catalog_json=value,
                )
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[AgentInstanceModel.id], set_={"catalog_json": value}
                    )
                )

    async def native_session(self, identity: str, actor: str) -> AgentSessionModel:
        async with self.database.sessions() as session:
            resource = await session.get(AgentSessionModel, identity)
            if resource is None:
                raise ValueError("external_agent_not_found")
            await self.authorize(session, resource.instance_id, actor)
            return resource

    async def remember_sessions(
        self, instance_id: str, actor: str, native_ids: list[str]
    ) -> dict[str, str]:
        mapping = {value: session_key(instance_id, value) for value in native_ids}
        if not mapping:
            return mapping
        async with self.database.sessions() as session:
            await self.authorize(session, instance_id, actor)
            known = set(
                await session.scalars(
                    select(AgentSessionModel.native_id).where(
                        AgentSessionModel.instance_id == instance_id,
                        AgentSessionModel.native_id.in_(native_ids),
                    )
                )
            )
        missing = set(mapping) - known
        if missing:
            async with self.database.immediate_session() as session:
                await self.authorize(session, instance_id, actor)
                for native_id in missing:
                    await session.execute(
                        insert(AgentSessionModel)
                        .values(id=mapping[native_id], instance_id=instance_id, native_id=native_id)
                        .on_conflict_do_nothing()
                    )
        return mapping

    async def operation(self, identity: str, actor: str) -> AgentOperationModel:
        async with self.database.sessions() as session:
            row = await session.scalar(
                select(AgentOperationModel).where(
                    or_(AgentOperationModel.id == identity, AgentOperationModel.run_id == identity)
                )
            )
            if row is None:
                raise ValueError("external_agent_not_found")
            await self.authorize(session, row.instance_id, actor)
            return row

    async def native_execution(self, identity: str, actor: str) -> AgentExecutionModel:
        async with self.database.sessions() as session:
            row = await session.get(AgentExecutionModel, identity)
            if row is None:
                raise ValueError("external_agent_not_found")
            resource = await session.get(AgentSessionModel, row.session_id)
            assert resource is not None
            await self.authorize(session, resource.instance_id, actor)
            return row

    async def remember_execution(self, session_id: str, native_id: str) -> str:
        identity = execution_key(session_id, native_id)
        async with self.database.sessions() as session:
            if await session.get(AgentExecutionModel, identity) is not None:
                return identity
        async with self.database.immediate_session() as session:
            await self._execution(session, session_id, native_id)
        return identity

    @staticmethod
    async def _execution(session: AsyncSession, session_id: str, native_id: str) -> str:
        identity = execution_key(session_id, native_id)
        await session.execute(
            insert(AgentExecutionModel)
            .values(id=identity, session_id=session_id, native_id=native_id)
            .on_conflict_do_nothing()
        )
        return identity

    async def prepare(
        self,
        *,
        identity: str,
        instance_id: str,
        actor: str,
        action: str,
        arguments: dict[str, Any],
        command: dict[str, Any],
        source: dict[str, Any],
        session_id: str | None,
        native_execution_id: str | None,
        lease: WorkLease | None,
    ) -> tuple[AgentOperationModel, bool]:
        digest = hashlib.sha256(encode(arguments).encode()).hexdigest()
        source_json = encode(source)
        command_json = encode(command)
        task_digest = hashlib.sha256(
            encode({"tool": "external_agent", "arguments": arguments}).encode()
        ).hexdigest()
        now = datetime.now(UTC)
        native_run = action in {"prompt", "queue", "watch"}
        run_id = (
            str(uuid5(NAMESPACE_URL, f"yuki:external-agent:{identity}")) if native_run else None
        )
        async with self.database.immediate_session() as session:
            instance = await self.authorize(session, instance_id, actor)
            existing = await session.get(AgentOperationModel, identity)
            if existing is not None:
                if existing.payload_hash != digest or existing.source_json != source_json:
                    raise ValueError("external_agent_request_conflict")
                return existing, False
            await self.identity.require_device(session, instance.device_id)
            if lease is not None:
                await WorkRepository(self.database)._assert_lease(session, lease)
                owner = (
                    (await session.execute(select(work).where(work.c.id == source.get("work_id"))))
                    .mappings()
                    .first()
                )
                if owner is None or owner["state"] in TERMINAL:
                    raise ValueError("external_agent_source_obsolete")
            row = AgentOperationModel(
                id=identity,
                instance_id=instance_id,
                session_id=session_id,
                actor_person_id=actor,
                work_id=source.get("work_id"),
                source_json=source_json,
                action=action,
                payload_hash=digest,
                command_json=command_json,
                run_id=run_id,
                native_execution_id=native_execution_id,
                created_at=now,
                dispatched_at=now,
            )
            session.add(row)
            if native_run:
                session.add(
                    SandboxTaskRunModel(
                        request_id=identity,
                        source_conversation_id=source["conversation_id"],
                        source_json=source_json,
                        payload_hash=task_digest,
                        run_id=run_id,
                        status="waiting",
                        progress_json=encode(
                            {
                                "kind": "external_agent",
                                "ownership": "observe" if action == "watch" else "execute",
                                "operation_id": identity,
                                "instance_id": instance_id,
                                "session_id": session_id,
                            }
                        ),
                        created_at=now,
                        updated_at=now,
                    )
                )
            await session.flush()
            return row, True

    async def response(self, identity: str, result: dict[str, Any]) -> AgentOperationModel:
        serialized = encode(result)
        async with self.database.sessions() as reader:
            original = await reader.get(AgentOperationModel, identity)
            if original is None:
                raise ValueError("external_agent_not_found")
            if (
                original.result_json == serialized
                or json.loads(original.result_json or "{}").get("pending") is False
            ):
                return original
        async with self.database.immediate_session() as session:
            row = await session.get(AgentOperationModel, identity)
            if row is None:
                raise ValueError("external_agent_not_found")
            # A delayed admission response must not overwrite an already committed terminal fact.
            previous = json.loads(row.result_json or "{}")
            if previous.get("pending") is False:
                return row
            row.result_json = serialized
            native_id = result.get("session_id")
            if isinstance(native_id, str):
                internal = session_key(row.instance_id, native_id)
                if row.session_id not in (None, internal):
                    raise ValueError("external_agent_response_session_mismatch")
                await session.execute(
                    insert(AgentSessionModel)
                    .values(id=internal, instance_id=row.instance_id, native_id=native_id)
                    .on_conflict_do_nothing()
                )
                row.session_id = internal
            if isinstance(result.get("execution_id"), str):
                if row.native_execution_id not in (None, result["execution_id"]):
                    raise ValueError("external_agent_response_execution_mismatch")
                row.native_execution_id = result["execution_id"]
                if row.session_id is not None:
                    await self._execution(session, row.session_id, row.native_execution_id)
            if row.run_id:
                task = await session.get(SandboxTaskRunModel, row.id)
                if task is not None and task.status == "waiting":
                    progress = json.loads(task.progress_json)
                    task.progress_json = encode({**progress, "session_id": row.session_id})
                    if result.get("pending") is False:
                        await self._complete(session, row, result)
            return row

    @staticmethod
    async def _complete(
        session: AsyncSession, row: AgentOperationModel, payload: dict[str, Any]
    ) -> None:
        task = await session.get(SandboxTaskRunModel, row.id)
        if task is None or task.status == "completed":
            return
        native_status = payload.get("status")
        status: str | None = None
        if native_status in {"completed", "succeeded"}:
            status = "succeeded"
        elif native_status in {"cancelled", "interrupted"}:
            status = "cancelled"
        elif payload.get("error") or native_status == "failed":
            status = "failed"
        elif native_status == "detached":
            status = "succeeded"
        if status is None:
            return
        result = {
            **payload,
            "kind": "external_agent",
            "request_id": row.id,
            "operation_id": row.id,
            "run_id": row.run_id,
            "session_id": row.session_id,
            "status": status,
            "native_status": payload.get("native_status", native_status),
            "pending": False,
            "admitted": payload.get("admitted") is True
            or native_status in {"completed", "succeeded", "cancelled", "interrupted", "detached"},
        }
        task.status = "completed"
        task.completion_json = encode(result)
        task.updated_at = datetime.now(UTC)
        session.add(
            SandboxTaskContinuationModel(
                request_id=row.id,
                state="ready",
                attempts=0,
                updated_at=datetime.now(UTC),
            )
        )

    async def receive_events(self, device_id: str, events: list[dict[str, Any]]) -> None:
        # Decode/serialize before taking the writer. No model, native RPC or network under it.
        prepared = [(event, encode(event)) for event in events if event.get("kind") != "delta"]
        if not prepared:
            return
        async with self.database.immediate_session() as session:
            await self.identity.require_device(session, device_id)
            for event, payload_json in prepared:
                instance = await session.scalar(
                    select(AgentInstanceModel).where(
                        AgentInstanceModel.device_id == device_id,
                        AgentInstanceModel.local_id == event["instance_id"],
                    )
                )
                if instance is None:
                    raise ValueError("external_agent_instance_unknown")
                operation = (
                    await session.get(AgentOperationModel, event["operation_id"])
                    if event.get("operation_id")
                    else None
                )
                if event.get("operation_id") and operation is None:
                    raise ValueError("external_agent_operation_unknown")
                if operation is not None and operation.instance_id != instance.id:
                    raise ValueError("external_agent_event_mismatch")
                added = await session.scalar(
                    insert(AgentEventModel)
                    .values(
                        device_id=device_id,
                        event_id=event["event_id"],
                        operation_id=operation.id if operation is not None else None,
                        kind=event["kind"],
                        payload_json=payload_json,
                        created_at=datetime.now(UTC),
                    )
                    .on_conflict_do_nothing(index_elements=["device_id", "event_id"])
                    .returning(AgentEventModel.id)
                )
                if added is None:
                    original = await session.scalar(
                        select(AgentEventModel.payload_json).where(
                            AgentEventModel.device_id == device_id,
                            AgentEventModel.event_id == event["event_id"],
                        )
                    )
                    if original != payload_json:
                        raise ValueError("external_agent_event_conflict")
                    continue
                if operation is None or operation.detached_at is not None:
                    continue
                native_id = event.get("session_id")
                if isinstance(native_id, str):
                    internal_id = session_key(instance.id, native_id)
                    if operation.session_id not in (None, internal_id):
                        raise ValueError("external_agent_event_session_mismatch")
                    await session.execute(
                        insert(AgentSessionModel)
                        .values(id=internal_id, instance_id=instance.id, native_id=native_id)
                        .on_conflict_do_nothing()
                    )
                    operation.session_id = internal_id
                execution = event.get("execution_id")
                if isinstance(execution, str):
                    if operation.native_execution_id not in (None, execution):
                        raise ValueError("external_agent_event_execution_mismatch")
                    operation.native_execution_id = execution
                    if operation.session_id is not None:
                        await self._execution(session, operation.session_id, execution)
                payload = event.get("payload", {})
                if not isinstance(payload, dict):
                    raise ValueError("invalid_external_agent_event")
                if event["kind"] == "completed":
                    terminal = {
                        **payload,
                        "session_id": native_id,
                        "execution_id": execution,
                        "pending": False,
                    }
                    if json.loads(operation.result_json or "{}").get("pending") is not False:
                        operation.result_json = encode(terminal)
                        await self._complete(session, operation, terminal)
                else:
                    previous = json.loads(operation.result_json or "{}")
                    if payload.get("admitted") is True and previous.get("pending") is not False:
                        # A native acceptance fact repairs a lost RPC reply, never resubmits it.
                        operation.result_json = encode(
                            {
                                **previous,
                                "admitted": True,
                                "pending": True,
                                "status": payload.get("status", "running"),
                                "session_id": native_id,
                                "execution_id": execution,
                            }
                        )
                    await self._input(
                        session,
                        operation,
                        {
                            **event,
                            **(
                                {"permission_id": str(added)}
                                if event["kind"] in {"permission", "question"}
                                else {}
                            ),
                        },
                    )

    async def _input(
        self, session: AsyncSession, operation: AgentOperationModel, event: dict[str, Any]
    ) -> None:
        if operation.work_id is None:
            return
        owner = (
            (await session.execute(select(work).where(work.c.id == operation.work_id)))
            .mappings()
            .first()
        )
        if owner is None or owner["state"] in TERMINAL:
            return
        source = json.loads(operation.source_json)
        generation = await session.scalar(
            select(CanonicalConversationModel.generation).where(
                CanonicalConversationModel.id == owner["conversation_id"]
            )
        )
        if source.get("generation") != owner["generation"] or generation != owner["generation"]:
            return
        try:
            await require_person(session, operation.actor_person_id)
        except ClientError:
            return
        await session.execute(
            insert(inputs)
            .values(
                conversation_id=owner["conversation_id"],
                generation=owner["generation"],
                work_id=owner["id"],
                source_key=f"external-agent:{event['event_id']}:{operation.id}",
                kind="control",
                ready=True,
                payload_json=encode(
                    {
                        "text": encode(
                            {
                                "kind": "external_agent_event",
                                "operation_id": operation.id,
                                "run_id": operation.run_id,
                                "event": event["kind"],
                                **(
                                    {"permission_id": event["permission_id"]}
                                    if "permission_id" in event
                                    else {}
                                ),
                                "payload": event.get("payload", {}),
                            }
                        ),
                        "signal": True,
                        "external_run_id": operation.run_id,
                    }
                ),
                created=time.time(),
            )
            .on_conflict_do_nothing(index_elements=[inputs.c.source_key])
        )
        if operation.run_id:
            await WorkWaitRepository(WorkRepository(self.database)).signal_external_action(
                session, owner["id"], operation.run_id
            )

    async def event_page(self, operation_id: str, cursor: int) -> list[dict[str, Any]]:
        async with self.database.sessions() as session:
            return [
                {
                    **json.loads(row.payload_json),
                    "cursor": row.id,
                    **(
                        {"permission_id": str(row.id)}
                        if row.kind in {"permission", "question"}
                        else {}
                    ),
                }
                for row in await session.scalars(
                    select(AgentEventModel)
                    .where(
                        AgentEventModel.operation_id == operation_id, AgentEventModel.id > cursor
                    )
                    .order_by(AgentEventModel.id)
                    .limit(100)
                )
            ]
