"""Device resources and original receipts, with short, fact-only writer transactions."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from yuki.client_api.db_models import ClientDeviceModel
from yuki.client_api.errors import ClientError
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.external_agents.cards import encode_card, external_agent_card, wake_event
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
from yuki.persistence.unit_of_work import optional_session
from yuki.runtime.work_repository import TERMINAL, WorkLease, WorkRepository
from yuki.runtime.work_schema_v1 import inputs, work
from yuki.runtime.work_wait import PreparedWaitAction, WorkWaitRepository
from yuki.sandbox.db_models import SandboxTaskContinuationModel, SandboxTaskRunModel


def encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def instance_key(device_id: str, local_id: str) -> str:
    return str(uuid5(UUID(device_id), f"agent:{local_id}"))


def session_key(instance_id: str, native_id: str) -> str:
    return str(uuid5(UUID(instance_id), f"session:{native_id}"))


def execution_key(session_id: str, native_id: str) -> str:
    return str(uuid5(UUID(session_id), f"execution:{native_id}"))


def session_observation(operation: AgentOperationModel) -> bool:
    return (
        operation.action == "watch"
        and json.loads(operation.command_json).get("observation_scope") == "session"
    )


class CatalogSuperseded(Exception):
    """The connection owning a prepared catalog was replaced before publication."""


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
        async with self.database.read_session() as session:
            return await self.authorize(session, identity, actor)

    async def catalog(
        self, actor: str
    ) -> list[tuple[AgentInstanceModel | None, ClientDeviceModel]]:
        async with self.database.read_session() as session:
            root = await require_person(session, actor)
            return [
                (instance, device)
                for instance, device in await session.execute(
                    select(AgentInstanceModel, ClientDeviceModel)
                    .select_from(ClientDeviceModel)
                    .outerjoin(
                        AgentInstanceModel,
                        and_(
                            AgentInstanceModel.device_id == ClientDeviceModel.id,
                            func.coalesce(
                                func.json_extract(AgentInstanceModel.catalog_json, "$._configured"),
                                1,
                            )
                            == 1,
                        ),
                    )
                    .where(
                        person_family_condition(ClientDeviceModel.person_id, root),
                        ClientDeviceModel.revoked_at.is_(None),
                        ClientDeviceModel.purpose == "connector",
                    )
                    .order_by(ClientDeviceModel.id, AgentInstanceModel.id)
                )
            ]

    async def register(
        self,
        device_id: str,
        catalog: list[dict[str, Any]],
        *,
        current: Callable[[], bool] | None = None,
        synchronize: bool = False,
    ) -> None:
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
        async with self.database.read_session() as session:
            await self.identity.require_device(session, device_id)
            existing = {
                row.local_id: row.catalog_json
                for row in await session.scalars(
                    select(AgentInstanceModel).where(AgentInstanceModel.device_id == device_id)
                )
            }
        changes = any(existing.get(key) != value for key, value in prepared.items()) or any(
            key not in prepared and json.loads(value).get("_configured", True)
            for key, value in existing.items()
        )
        if not changes and not synchronize:
            return
        async with self.database.immediate_session() as session:
            await self.identity.require_device(session, device_id)
            if current is not None and not current():
                raise CatalogSuperseded
            for local_id, value in prepared.items():
                statement = insert(AgentInstanceModel).values(
                    id=instance_key(device_id, local_id),
                    device_id=device_id,
                    local_id=local_id,
                    catalog_json=value,
                )
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[AgentInstanceModel.id],
                        set_={"catalog_json": value},
                        where=AgentInstanceModel.catalog_json != value,
                    )
                )
            selected = func.json_each(encode(list(prepared))).table_valued("value")
            await session.execute(
                update(AgentInstanceModel)
                .where(
                    AgentInstanceModel.device_id == device_id,
                    AgentInstanceModel.local_id.not_in(select(selected.c.value)),
                    func.coalesce(
                        func.json_extract(AgentInstanceModel.catalog_json, "$._configured"), 1
                    )
                    == 1,
                )
                .values(
                    catalog_json=func.json_set(
                        AgentInstanceModel.catalog_json, "$._configured", func.json("false")
                    )
                )
            )
            if current is not None and not current():
                raise CatalogSuperseded

    async def unowned_requests(
        self, instance: AgentInstanceModel, native_session: str
    ) -> list[dict[str, Any]]:
        """Native waits in this session that no operation received, still unresolved.

        Only metadata was uploaded for them (tool name, time); never their content.
        Callers have already authorized the actor for this instance's device. Only
        this session's unowned waits and their resolutions are read.
        """
        stored = AgentEventModel.payload_json
        async with self.database.read_session() as reader:
            rows = (
                await reader.execute(
                    select(
                        AgentEventModel.id,
                        AgentEventModel.kind,
                        AgentEventModel.payload_json,
                        AgentEventModel.created_at,
                    )
                    .where(
                        AgentEventModel.instance_id == instance.id,
                        AgentEventModel.native_session_id == native_session,
                        AgentEventModel.operation_id.is_(None),
                        or_(
                            and_(
                                AgentEventModel.kind.in_(("permission", "question")),
                                func.json_extract(stored, "$.payload.unowned") == 1,
                            ),
                            func.json_extract(stored, "$.payload.permission_resolved").is_not(None),
                        ),
                    )
                    .order_by(AgentEventModel.id)
                )
            ).all()
        pending: dict[str, dict[str, Any]] = {}
        for identity, kind, payload_json, created in rows:
            payload = json.loads(payload_json).get("payload")
            if not isinstance(payload, dict):
                continue
            if kind in {"permission", "question"} and payload.get("unowned") is True:
                pending[str(payload.get("native_request_id"))] = {
                    "permission_id": str(identity),
                    "kind": kind,
                    "tool": payload.get("tool"),
                    "received_at": created.replace(tzinfo=UTC).isoformat(),
                    "options": ["deny"],
                }
            elif isinstance(payload.get("permission_resolved"), str):
                pending.pop(payload["permission_resolved"], None)
        return list(pending.values())

    async def native_session(self, identity: str, actor: str) -> AgentSessionModel:
        async with self.database.read_session() as session:
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
        async with self.database.read_session() as session:
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
        async with self.database.read_session() as session:
            row = await session.scalar(
                select(AgentOperationModel).where(
                    or_(AgentOperationModel.id == identity, AgentOperationModel.run_id == identity)
                )
            )
            if row is None:
                raise ValueError("external_agent_not_found")
            await self.authorize(session, row.instance_id, actor)
            return row

    async def execution_operations(
        self, instance_id: str, session_id: str, native_id: str, actor: str
    ) -> list[AgentOperationModel]:
        async with self.database.read_session() as session:
            await self.authorize(session, instance_id, actor)
            return list(
                await session.scalars(
                    select(AgentOperationModel)
                    .where(
                        AgentOperationModel.instance_id == instance_id,
                        AgentOperationModel.session_id == session_id,
                        AgentOperationModel.native_execution_id == native_id,
                        AgentOperationModel.action.in_(("prompt", "queue")),
                        person_family_condition(AgentOperationModel.actor_person_id, actor),
                        func.coalesce(
                            func.json_extract(AgentOperationModel.result_json, "$.executed"), 1
                        )
                        != 0,
                        func.coalesce(
                            func.json_extract(AgentOperationModel.result_json, "$.admitted"), 1
                        )
                        != 0,
                    )
                    .limit(2)
                )
            )

    async def work_operation(
        self,
        operation_id: str,
        *,
        work_id: str,
        action: str,
        actor_person_id: str,
        session: AsyncSession,
    ) -> dict[str, Any] | None:
        """Read an original Work control receipt, without authorizing a new effect."""
        operation = await session.scalar(
            select(AgentOperationModel).where(
                AgentOperationModel.id == operation_id,
                AgentOperationModel.work_id == work_id,
                AgentOperationModel.action == action,
                AgentOperationModel.actor_person_id == actor_person_id,
            )
        )
        if operation is None:
            return None
        return {**await self.operation_view(operation, session=session), "action": operation.action}

    async def operation_view(
        self, operation: AgentOperationModel, *, session: AsyncSession | None = None
    ) -> dict[str, Any]:
        """Project the original local receipt for public reads and Work recovery."""
        result = json.loads(operation.result_json or "{}")
        native_execution = (
            result.get("execution_id")
            if session_observation(operation)
            else operation.native_execution_id
        )
        native = {
            "session_id": result.get("session_id"),
            "execution_id": native_execution,
        }
        execution_id = (
            execution_key(operation.session_id, native_execution)
            if operation.session_id and native_execution
            else None
        )
        async with optional_session(self.database, session, write=False) as reader:
            if (
                operation.action == "cancel"
                and result.get("admitted") is True
                and result.get("pending") is True
                and operation.native_execution_id is not None
            ):
                # Interrupt admission and the original execution's terminal fact can
                # arrive on different operations (for example, a later watch).
                terminal = await reader.scalar(
                    select(AgentOperationModel.result_json)
                    .where(
                        AgentOperationModel.instance_id == operation.instance_id,
                        AgentOperationModel.session_id == operation.session_id,
                        AgentOperationModel.native_execution_id == operation.native_execution_id,
                        AgentOperationModel.action.in_(("prompt", "queue", "watch")),
                        func.json_type(AgentOperationModel.result_json, "$.admitted") == "true",
                        func.json_type(AgentOperationModel.result_json, "$.pending") == "false",
                        func.json_extract(AgentOperationModel.result_json, "$.status").in_(
                            ("completed", "succeeded", "cancelled", "interrupted", "failed")
                        ),
                    )
                    .order_by(AgentOperationModel.created_at.desc())
                    .limit(1)
                )
                if terminal is not None:
                    native_result = json.loads(terminal)
                    result = {
                        **result,
                        "status": native_result["status"],
                        "pending": False,
                        "native_status": native_result["status"],
                        "error": native_result.get("error"),
                    }
            task = await reader.get(SandboxTaskRunModel, operation.id) if operation.run_id else None
        if isinstance(result.get("error"), dict) and "code" in result["error"]:
            result = {**result, "error_detail": result["error"], "error": result["error"]["code"]}
        if task is not None and task.status == "completed":
            completion = json.loads(task.completion_json or "{}")
            return {
                **completion,
                **(
                    {"status": "detached"}
                    if completion.get("operation_status") == "detached"
                    else {}
                ),
                "instance_id": operation.instance_id,
                "work_id": operation.work_id,
                "native_reference": native,
                "execution_id": execution_id,
            }
        return {
            **result,
            "kind": "external_agent",
            "operation_id": operation.id,
            "request_id": operation.id,
            "instance_id": operation.instance_id,
            "work_id": operation.work_id,
            "session_id": operation.session_id,
            "run_id": operation.run_id,
            "execution_id": execution_id,
            "native_reference": native,
            "pending": result.get("pending", True),
            "status": result.get("status", "unknown"),
            "uncertain": not bool(result) or result.get("status") == "unknown",
            "admitted": result.get("admitted") is True,
            "ownership": "observe" if operation.action == "watch" else "execute",
        }

    async def native_execution(self, identity: str, actor: str) -> AgentExecutionModel:
        async with self.database.read_session() as session:
            row = await session.get(AgentExecutionModel, identity)
            if row is None:
                raise ValueError("external_agent_not_found")
            resource = await session.get(AgentSessionModel, row.session_id)
            assert resource is not None
            await self.authorize(session, resource.instance_id, actor)
            return row

    async def remember_execution(self, session_id: str, native_id: str) -> str:
        identity = execution_key(session_id, native_id)
        async with self.database.read_session() as session:
            if await session.get(AgentExecutionModel, identity) is not None:
                return identity
        async with self.database.immediate_session() as session:
            await self._execution(session, session_id, native_id)
        return identity

    async def remember_executions(
        self, session_id: str, native_ids: list[str], actor: str
    ) -> dict[str, str]:
        """Publish a queried page's real native turn identities in one short writer."""
        mapping = {value: execution_key(session_id, value) for value in native_ids}
        if not mapping:
            return mapping
        async with self.database.read_session() as reader:
            resource = await reader.get(AgentSessionModel, session_id)
            if resource is None:
                raise ValueError("external_agent_not_found")
            await self.authorize(reader, resource.instance_id, actor)
            instance_id = resource.instance_id
            known = set(
                await reader.scalars(
                    select(AgentExecutionModel.native_id).where(
                        AgentExecutionModel.session_id == session_id,
                        AgentExecutionModel.native_id.in_(mapping),
                    )
                )
            )
        missing = set(mapping) - known
        if missing:
            async with self.database.immediate_session() as writer:
                await self.authorize(writer, instance_id, actor)
                for native_id in missing:
                    await self._execution(writer, session_id, native_id)
        return mapping

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
        progress_json = encode(
            {
                "kind": "external_agent",
                "ownership": "observe" if action == "watch" else "execute",
                "operation_id": identity,
                "instance_id": instance_id,
                "session_id": session_id,
            }
        )
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
                        progress_json=progress_json,
                        created_at=now,
                        updated_at=now,
                    )
                )
            await session.flush()
            return row, True

    async def response(self, identity: str, result: dict[str, Any]) -> AgentOperationModel:
        serialized = encode(result)
        async with self.database.read_session() as reader:
            original = await reader.get(AgentOperationModel, identity)
            if original is None:
                raise ValueError("external_agent_not_found")
            if original.result_json == serialized:
                return original
            previous = json.loads(original.result_json or "{}")
            if previous.get("pending") is False or encode(previous) == serialized:
                return original
        async with self.database.immediate_session() as session:
            row = await session.get(AgentOperationModel, identity)
            if row is None:
                raise ValueError("external_agent_not_found")
            await self._bind_native(session, row, result)
            await self._project(session, row, result, serialized)
            return row

    async def _bind_native(
        self, session: AsyncSession, row: AgentOperationModel, native: dict[str, Any]
    ) -> None:
        native_id = native.get("session_id")
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
        execution = native.get("execution_id")
        if isinstance(execution, str):
            if not session_observation(row):
                if row.native_execution_id not in (None, execution):
                    raise ValueError("external_agent_response_execution_mismatch")
                row.native_execution_id = execution
            if row.session_id is not None:
                await self._execution(session, row.session_id, execution)

    async def _project(
        self,
        session: AsyncSession,
        row: AgentOperationModel,
        result: dict[str, Any],
        serialized: str,
    ) -> None:
        previous = json.loads(row.result_json or "{}")
        # The connector owns reduction; the server only orders its original receipts.
        if row.detached_at is not None or previous.get("pending") is False:
            return
        if (
            "state_sequence" in previous
            and result.get("state_sequence", -1) <= previous["state_sequence"]
        ):
            return
        row.result_json = serialized
        if row.run_id:
            await session.execute(
                update(SandboxTaskRunModel)
                .where(
                    SandboxTaskRunModel.request_id == row.id,
                    SandboxTaskRunModel.status == "waiting",
                )
                .values(
                    progress_json=func.json_set(
                        SandboxTaskRunModel.progress_json, "$.session_id", row.session_id
                    )
                )
            )
            if result.get("pending") is False:
                await self._complete(session, row, result, serialized)

    @staticmethod
    async def _complete(
        session: AsyncSession,
        row: AgentOperationModel,
        payload: dict[str, Any],
        payload_json: Any,
    ) -> None:
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
        result = func.json_set(
            payload_json,
            "$.kind",
            "external_agent",
            "$.request_id",
            row.id,
            "$.operation_id",
            row.id,
            "$.run_id",
            row.run_id,
            "$.instance_id",
            row.instance_id,
            "$.session_id",
            row.session_id,
            "$.execution_id",
            (
                execution_key(row.session_id, row.native_execution_id)
                if row.session_id and row.native_execution_id
                else None
            ),
            "$.native_reference",
            func.json_object(
                "session_id",
                payload.get("session_id"),
                "execution_id",
                row.native_execution_id,
            ),
            "$.status",
            status,
            "$.native_status",
            payload.get("native_status", native_status),
            "$.pending",
            func.json("false"),
            "$.admitted",
            func.json("true" if payload.get("admitted") is True else "false"),
        )
        completed = await session.scalar(
            update(SandboxTaskRunModel)
            .where(
                SandboxTaskRunModel.request_id == row.id,
                SandboxTaskRunModel.status != "completed",
            )
            .values(status="completed", completion_json=result, updated_at=datetime.now(UTC))
            .returning(SandboxTaskRunModel.request_id)
        )
        if completed is None:
            return
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
        prepared: list[tuple[dict[str, Any], str, str, Any, Any, bool]] = []
        for event in events:
            if event.get("kind") == "delta":
                continue
            payload = event.get("payload", {})
            original_wire = encode(
                {
                    key: value
                    for key, value in event.items()
                    if key not in {"sequence", "operation_state"}
                }
            )
            if not isinstance(payload, dict):
                # Unowned/replayed facts keep their existing transport receipt
                # semantics. Only an active operation consumes this payload.
                prepared.append((event, encode(event), original_wire, None, None, False))
                continue
            state = event.get("operation_state")
            # Only a fact that needs a decision becomes a Work input; progress stays
            # on the timeline (taskbook §4.5). Classified before the writer.
            wake = wake_event(event["kind"], payload, state if isinstance(state, dict) else None)
            prepared.append((event, encode(event), original_wire, state, encode(state), wake))
        if not prepared:
            return
        wait_repository = WorkWaitRepository(WorkRepository(self.database))
        operation_ids = encode(
            [event[0]["operation_id"] for event in prepared if event[0].get("operation_id")]
        )
        selected = func.json_each(operation_ids).table_valued("value")
        source_query = (
            select(AgentOperationModel.id, AgentOperationModel.work_id)
            .where(AgentOperationModel.id.in_(select(selected.c.value)))
            .order_by(AgentOperationModel.id)
        )
        while True:
            async with self.database.read_session() as reader:
                await reader.execute(text("BEGIN"))
                sources = (await reader.execute(source_query)).all()
                work_ids = {work_id for _, work_id in sources if work_id is not None}
                prepared_waits = {
                    work_id: await wait_repository.prepare_external_action(reader, work_id)
                    for work_id in work_ids
                }
            async with self.database.immediate_session() as session:
                await self.identity.require_device(session, device_id)
                if (await session.execute(source_query)).all() != sources:
                    continue
                changed = False
                for work_id, prepared_wait in prepared_waits.items():
                    if not await wait_repository.external_action_current(
                        session, work_id, prepared_wait
                    ):
                        changed = True
                        break
                if changed:
                    continue
                for event, payload_json, original_wire, state, state_json, wake in prepared:
                    instance = await session.scalar(
                        select(AgentInstanceModel).where(
                            AgentInstanceModel.device_id == device_id,
                            AgentInstanceModel.local_id == event["instance_id"],
                        )
                    )
                    if instance is None:
                        raise ValueError("external_agent_instance_unknown")
                    original = (
                        (
                            await session.execute(
                                select(
                                    AgentOperationModel,
                                    func.json_type(AgentOperationModel.result_json, "$.pending"),
                                )
                                .where(AgentOperationModel.id == event["operation_id"])
                                .execution_options(populate_existing=True)
                            )
                        ).first()
                        if event.get("operation_id")
                        else None
                    )
                    operation, pending_type = original if original is not None else (None, None)
                    if event.get("operation_id") and operation is None:
                        raise ValueError("external_agent_operation_unknown")
                    if operation is not None and operation.instance_id != instance.id:
                        raise ValueError("external_agent_event_mismatch")
                    received_at = datetime.now(UTC)
                    added = await session.scalar(
                        insert(AgentEventModel)
                        .values(
                            device_id=device_id,
                            event_id=event["event_id"],
                            operation_id=operation.id if operation is not None else None,
                            kind=event["kind"],
                            payload_json=payload_json,
                            created_at=received_at,
                            instance_id=instance.id,
                            native_session_id=event.get("session_id"),
                        )
                        .on_conflict_do_nothing(index_elements=["device_id", "event_id"])
                        .returning(AgentEventModel.id)
                    )
                    if added is None:
                        original = (
                            await session.execute(
                                select(
                                    AgentEventModel.payload_json,
                                    func.json_type(AgentEventModel.payload_json, "$.sequence"),
                                    func.json_type(
                                        AgentEventModel.payload_json, "$.operation_state"
                                    ),
                                ).where(
                                    AgentEventModel.device_id == device_id,
                                    AgentEventModel.event_id == event["event_id"],
                                )
                            )
                        ).one()
                        # An upgrade can add projection metadata to a v2 fact whose
                        # original commit succeeded but ACK was lost. Keep that row.
                        same_legacy_fact = (
                            original[1] is None
                            and original[2] is None
                            and original[0] == original_wire
                        )
                        if original[0] != payload_json and not same_legacy_fact:
                            raise ValueError("external_agent_event_conflict")
                        continue
                    if operation is None or operation.detached_at is not None:
                        continue
                    payload = event.get("payload", {})
                    if (
                        not isinstance(payload, dict)
                        or not isinstance(state, dict)
                        or not isinstance(event.get("sequence"), int)
                        or state.get("state_sequence") != event["sequence"]
                    ):
                        raise ValueError("invalid_external_agent_event")
                    await self._bind_native(session, operation, event)
                    if any(
                        state.get(key) is not None and state[key] != event.get(key)
                        for key in ("session_id", "execution_id")
                    ):
                        await self._bind_native(session, operation, state)
                    await self._project(session, operation, state, state_json)
                    if wake and state.get("pending") is not False and pending_type != "false":
                        native_execution = event.get("execution_id")
                        observe = operation.action == "watch"
                        request = event["kind"] in {"permission", "question"}
                        card = external_agent_card(
                            event=event["kind"],
                            operation_id=operation.id,
                            run_id=operation.run_id,
                            relationship="observe" if observe else "delegate",
                            status=state.get("status"),
                            payload=payload,
                            # An observed request is told, never made answerable (D6).
                            permission_id=str(added) if request and not observe else None,
                            session_id=operation.session_id,
                            execution_id=execution_key(operation.session_id, native_execution)
                            if operation.session_id and native_execution
                            else None,
                        )
                        await self._input(
                            session,
                            operation,
                            event["event_id"],
                            card,
                            prepared_waits=prepared_waits,
                            request=request,
                        )
            return

    async def dispatchable(self, identity: str) -> bool:
        """Whether a registered command's original source still permits writing it."""
        async with self.database.read_session() as reader:
            row = await reader.get(AgentOperationModel, identity)
            if row is None or row.result_json is not None or row.detached_at is not None:
                return False
            if row.work_id is None:
                return True
            owner = await reader.scalar(
                select(work.c.id)
                .join(
                    CanonicalConversationModel,
                    CanonicalConversationModel.id == work.c.conversation_id,
                )
                .where(
                    work.c.id == row.work_id,
                    work.c.state.not_in(TERMINAL),
                    work.c.generation == func.json_extract(row.source_json, "$.generation"),
                    CanonicalConversationModel.generation == work.c.generation,
                )
            )
            return owner is not None

    async def unconfirmed(self, device_id: str) -> list[tuple[str, str]]:
        """Operations whose device answer was never recorded and still matters.

        Run-bearing operations of live Work; controls while their target is still
        unsettled or their initiating Work is live. Once neither holds, the answer
        changes nothing and the control keeps its unknown receipt. Each is only read
        back by its original ID, never resent.
        """
        target = aliased(AgentOperationModel)
        live_work = work.c.state.not_in(TERMINAL)
        async with self.database.read_session() as reader:
            rows = await reader.execute(
                select(AgentOperationModel.id, AgentInstanceModel.local_id)
                .join(AgentInstanceModel, AgentInstanceModel.id == AgentOperationModel.instance_id)
                .outerjoin(work, work.c.id == AgentOperationModel.work_id)
                .outerjoin(
                    target,
                    target.id
                    == func.json_extract(
                        AgentOperationModel.command_json, "$.arguments.operation_id"
                    ),
                )
                .where(
                    AgentInstanceModel.device_id == device_id,
                    AgentOperationModel.result_json.is_(None),
                    AgentOperationModel.detached_at.is_(None),
                    or_(
                        and_(AgentOperationModel.run_id.is_not(None), live_work),
                        and_(
                            AgentOperationModel.action.in_(
                                ("steer", "respond", "cancel", "detach")
                            ),
                            or_(
                                live_work,
                                and_(
                                    target.id.is_not(None),
                                    target.detached_at.is_(None),
                                    or_(
                                        target.result_json.is_(None),
                                        func.coalesce(
                                            func.json_extract(target.result_json, "$.pending"), 1
                                        )
                                        != 0,
                                    ),
                                ),
                            ),
                        ),
                    ),
                )
                .order_by(AgentOperationModel.created_at)
            )
            return [(identity, local_id) for identity, local_id in rows.all()]

    async def control_settled(self, identity: str) -> None:
        """Tell the Work that issued a control what it settled (private input)."""
        waits = WorkWaitRepository(WorkRepository(self.database))
        while True:
            async with self.database.read_session() as reader:
                await reader.execute(text("BEGIN"))
                row = await reader.get(AgentOperationModel, identity)
                if row is None or row.work_id is None or row.result_json is None:
                    return
                target_id = json.loads(row.command_json).get("arguments", {}).get("operation_id")
                target = await reader.get(AgentOperationModel, target_id) if target_id else None
                # A run the control already ended is matched by the normal completion
                # path (with its real terminal fact); only a still-waiting run is signalled.
                waiting_run = (
                    target.run_id
                    if target is not None
                    and target.run_id
                    and target.work_id == row.work_id
                    and await reader.scalar(
                        select(SandboxTaskRunModel.status).where(
                            SandboxTaskRunModel.request_id == target.id
                        )
                    )
                    == "waiting"
                    else None
                )
                prepared = {row.work_id: await waits.prepare_external_action(reader, row.work_id)}
            result = json.loads(row.result_json)
            input_json = encode_card(
                {
                    "kind": "external_agent_control",
                    "action": row.action,
                    "operation_id": row.id,
                    "run_id": row.run_id,
                    "target_operation_id": target_id,
                    **{
                        key: result[key]
                        for key in (
                            "status",
                            "pending",
                            "error",
                            "cancel_requested",
                            "response_submitted",
                            "released_without_device",
                        )
                        if key in result
                    },
                }
            )
            async with self.database.immediate_session() as writer:
                if not await waits.external_action_current(
                    writer, row.work_id, prepared[row.work_id]
                ):
                    continue
                current = await writer.get(AgentOperationModel, identity)
                assert current is not None
                await self._input(
                    writer,
                    current,
                    f"control:{identity}",
                    input_json,
                    prepared_waits=prepared,
                    request=False,
                )
                if waiting_run is not None:
                    # A Work waiting on that very run hears about the control at once.
                    await waits.signal_external_action(
                        writer, row.work_id, waiting_run, prepared=prepared[row.work_id]
                    )
            return

    async def connection_event(self, device_id: str, connection_id: str, online: bool) -> None:
        """Record transport facts in original Work inputs, never in native receipts."""
        query = (
            select(AgentOperationModel)
            .join(AgentInstanceModel, AgentInstanceModel.id == AgentOperationModel.instance_id)
            .join(work, work.c.id == AgentOperationModel.work_id)
            .where(
                AgentInstanceModel.device_id == device_id,
                work.c.state.not_in(TERMINAL),
                AgentOperationModel.run_id.is_not(None),
                AgentOperationModel.detached_at.is_(None),
                func.coalesce(func.json_type(AgentOperationModel.result_json, "$.pending"), "")
                != "false",
            )
        )
        if online:
            # Reconnecting is progress: the original runs carry on, nobody is woken.
            return
        waits = WorkWaitRepository(WorkRepository(self.database))
        event_id = f"transport:{connection_id}:device_offline"
        while True:
            async with self.database.read_session() as reader:
                await reader.execute(text("BEGIN"))
                operations = list(await reader.scalars(query))
                prepared = {
                    cast(str, row.work_id): await waits.prepare_external_action(
                        reader, cast(str, row.work_id)
                    )
                    for row in operations
                }
            if not operations:
                return
            async with self.database.immediate_session() as writer:
                current = list(await writer.scalars(query))
                if {row.id for row in current} != {row.id for row in operations}:
                    continue
                changed = False
                for work_id, waiting in prepared.items():
                    if not await waits.external_action_current(writer, work_id, waiting):
                        changed = True
                        break
                if changed:
                    continue
                for row in current:
                    await self._input(
                        writer,
                        row,
                        event_id,
                        external_agent_card(
                            event="device_offline",
                            operation_id=row.id,
                            run_id=row.run_id,
                            relationship="observe" if row.action == "watch" else "delegate",
                            status="unknown",
                            device_id=device_id,
                        ),
                        prepared_waits=prepared,
                        request=False,
                    )
            return

    async def _input(
        self,
        session: AsyncSession,
        operation: AgentOperationModel,
        event_id: str,
        text: str,
        *,
        prepared_waits: dict[str, PreparedWaitAction],
        request: bool,
    ) -> None:
        """Append one wake-class card to the owning Work and wake it.

        Only decision facts arrive here (requests, terminal or lost executor, control
        results, going offline); progress never does. A request also wakes a Work
        waiting for its user (a root waiting only for messages): the question is the
        input it waits for (taskbook §4.6).
        """
        if operation.work_id is None:
            return
        owner = (
            (
                await session.execute(
                    select(work)
                    .join(
                        CanonicalConversationModel,
                        CanonicalConversationModel.id == work.c.conversation_id,
                    )
                    .where(
                        work.c.id == operation.work_id,
                        work.c.state.not_in(TERMINAL),
                        work.c.generation
                        == func.json_extract(operation.source_json, "$.generation"),
                        CanonicalConversationModel.generation == work.c.generation,
                    )
                )
            )
            .mappings()
            .first()
        )
        if owner is None:
            return
        try:
            await require_person(session, operation.actor_person_id)
        except ClientError:
            return
        # Work stores the card text, marked as a wake signal of this run.
        payload_json = encode(
            {"text": text, "signal": True, "wake": True, "external_run_id": operation.run_id}
        )
        await session.execute(
            insert(inputs)
            .values(
                conversation_id=owner["conversation_id"],
                generation=owner["generation"],
                work_id=owner["id"],
                source_key=f"external-agent:{event_id}:{operation.id}",
                kind="control",
                ready=True,
                payload_json=payload_json,
                created=time.time(),
            )
            .on_conflict_do_nothing(index_elements=[inputs.c.source_key])
        )
        if request and owner["state"] == "waiting_user":
            await WorkWaitRepository(WorkRepository(self.database)).wake_for_request(
                session, owner["id"]
            )
        elif operation.run_id:
            await WorkWaitRepository(WorkRepository(self.database)).signal_external_action(
                session, owner["id"], operation.run_id, prepared=prepared_waits[owner["id"]]
            )

    async def event_page(self, operation_id: str, cursor: int) -> list[dict[str, Any]]:
        async with self.database.read_session() as session:
            instance_id = cast(
                str,
                await session.scalar(
                    select(AgentOperationModel.instance_id).where(
                        AgentOperationModel.id == operation_id
                    )
                ),
            )
            return [
                {
                    **self._event_view(json.loads(row.payload_json), instance_id),
                    "cursor": row.id,
                    "received_at": row.created_at.replace(tzinfo=UTC).isoformat(),
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

    @staticmethod
    def _event_view(event: dict[str, Any], instance_id: str) -> dict[str, Any]:
        native_session = event.get("session_id")
        native_execution = event.get("execution_id")
        internal_session = session_key(instance_id, native_session) if native_session else None
        result = {
            **event,
            "instance_id": instance_id,
            "session_id": internal_session,
            "execution_id": execution_key(internal_session, native_execution)
            if internal_session and native_execution
            else None,
            "native_reference": {"session_id": native_session, "execution_id": native_execution},
        }
        if "operation_state" in result:
            state = result["operation_state"]
            result["operation_state"] = {
                **state,
                "session_id": session_key(instance_id, state["session_id"])
                if state.get("session_id")
                else None,
                "execution_id": execution_key(
                    session_key(instance_id, state["session_id"]), state["execution_id"]
                )
                if state.get("session_id") and state.get("execution_id")
                else None,
            }
        return result
