"""Delegation and observation records: Works a native agent executes, with no Yuki loop.

A delegation is a child Work of the Work that asked for it; an observation is a root
Work of the conversation that asked for it (taskbook §4.1, §4.3). Both carry their
executor in ``runtime_subagents.executor_json`` so neither scheduler starts Main for
them. Their state is settled deterministically from native facts; only decision
facts reach a model: the parent Work for a delegation, a one-off notice Work in the
original conversation for an observation.
"""

from __future__ import annotations

import json
import time
from datetime import UTC
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.external_agents.cards import encode_card
from yuki.external_agents.db_models import (
    AgentEventModel,
    AgentInstanceModel,
    AgentOperationModel,
    AgentSessionModel,
)
from yuki.persistence.database import Database
from yuki.runtime.subagent_schema import children
from yuki.runtime.work_repository import TERMINAL, WorkConflict, WorkLease, WorkRepository
from yuki.runtime.work_schema_v1 import inputs, work
from yuki.runtime.work_wait_schema import waits

RELATIONSHIPS = ("delegate", "observe", "takeover")
OWNING = frozenset({"delegate", "takeover"})
_REQUESTS = frozenset({"permission", "question"})
_TERMINAL_NATIVE = {
    "completed": "completed",
    "succeeded": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "interrupted": "cancelled",
}


def native_prompt(brief: dict[str, Any]) -> str:
    """The delegated text, built only from the brief the model wrote."""
    parts = [str(brief["goal"]).strip()]
    if brief.get("context"):
        parts.append(f"资料：{brief['context']}")
    if brief.get("acceptance"):
        parts.append(f"验收：{brief['acceptance']}")
    if brief.get("files"):
        parts.append("相关文件：" + "、".join(str(item) for item in brief["files"]))
    return "\n\n".join(parts)


def delegation_state(state: str, reason: str | None) -> str:
    """Card state: Yuki's settlement, not a native status string."""
    if state == "waiting_user":
        return "waiting"
    if state in TERMINAL:
        return state
    if reason in {"device_offline", "executor_lost"}:
        return "unknown"
    return "running"


class Delegations:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.works = WorkRepository(database)

    # ---- creation -------------------------------------------------------------

    async def create(
        self,
        lease: WorkLease,
        *,
        parent_id: str | None,
        key: str,
        goal: str,
        brief: dict[str, Any],
        executor: dict[str, Any],
        source: dict[str, Any],
    ) -> str:
        """Register the record before any native dispatch; idempotent by call key."""
        encoded_brief = json.dumps(brief, ensure_ascii=False, sort_keys=True)
        encoded_executor = json.dumps(executor, ensure_ascii=False, sort_keys=True)
        identity = str(uuid4())
        record_source = {**source, "work_id": identity, "parent_work_id": parent_id}
        if parent_id is not None:
            record_source["worker"] = True
        async with self.database.immediate_session() as session:
            await self.works._assert_lease(session, lease)
            previous = (
                (await session.execute(select(children).where(children.c.source_key == key)))
                .mappings()
                .first()
            )
            if previous is not None:
                if (
                    previous["brief_json"] != encoded_brief
                    or previous["executor_json"] != encoded_executor
                ):
                    raise WorkConflict("subagent_start_conflict")
                return str(previous["work_id"])
            if parent_id is not None:
                parent = (
                    (await session.execute(select(work).where(work.c.id == parent_id)))
                    .mappings()
                    .one()
                )
                if (
                    parent["conversation_id"] != lease.conversation_id
                    or parent["generation"] != lease.generation
                    or parent["state"] in TERMINAL
                ):
                    raise WorkConflict("subagent_parent_unavailable")
            if executor["relationship"] in OWNING and executor.get("session_id"):
                owner = await self.session_owner(session, executor["session_id"])
                if owner is not None:
                    raise DelegationConflict(owner)
            await self.works.accept_in_session(
                session,
                lease,
                identity=identity,
                parent_work_id=parent_id,
                source_key=f"executor:{identity}",
                source=record_source,
                prepared_source_json=json.dumps(record_source, ensure_ascii=False, sort_keys=True),
                goal=goal,
                initial_state="queued",
            )
            # No Yuki loop ever runs it: it waits on its native executor from the start.
            await session.execute(
                update(work)
                .where(work.c.id == identity)
                .values(state="waiting_external", reason="native_executor")
            )
            await session.execute(
                insert(children).values(
                    work_id=identity,
                    source_key=key,
                    brief_json=encoded_brief,
                    executor_json=encoded_executor,
                )
            )
        return identity

    @staticmethod
    async def session_owner(session: AsyncSession, session_id: str) -> str | None:
        """The live delegation that owns this native session, if any (a fact, not a cap)."""
        owner = await session.scalar(
            select(work.c.id)
            .join(children, children.c.work_id == work.c.id)
            .where(
                work.c.state.not_in(TERMINAL),
                children.c.executor_json.is_not(None),
                func.json_extract(children.c.executor_json, "$.relationship").in_(tuple(OWNING)),
                func.json_extract(children.c.executor_json, "$.session_id") == session_id,
            )
            .limit(1)
        )
        if owner is not None:
            return str(owner)
        bound = await session.scalar(
            select(AgentOperationModel.work_id)
            .join(children, children.c.work_id == AgentOperationModel.work_id)
            .join(work, work.c.id == AgentOperationModel.work_id)
            .where(
                AgentOperationModel.session_id == session_id,
                work.c.state.not_in(TERMINAL),
                func.json_extract(children.c.executor_json, "$.relationship").in_(tuple(OWNING)),
            )
            .limit(1)
        )
        return str(bound) if bound is not None else None

    async def executor(self, work_id: str) -> dict[str, Any] | None:
        async with self.database.read_session() as reader:
            return await executor_of(reader, work_id)

    # ---- settlement inside the event writer -------------------------------------

    async def settle(
        self,
        session: AsyncSession,
        operation: AgentOperationModel,
        executor: dict[str, Any],
        *,
        event_id: str,
        kind: str,
        payload: dict[str, Any],
        state: dict[str, Any],
        card: str,
        request_key: str | None,
    ) -> None:
        """Apply one wake-class native fact to its record (same writer as the event)."""
        record = (
            (await session.execute(select(work).where(work.c.id == operation.work_id)))
            .mappings()
            .first()
        )
        if record is None or record["state"] in TERMINAL:
            return  # Late facts never revive an ended record.
        if executor["relationship"] == "observe":
            await self._observed(
                session, record, executor, kind, payload, state, card, event_id, request_key
            )
            return
        now = time.time()
        if kind in _REQUESTS:
            await session.execute(
                update(work)
                .where(work.c.id == record["id"], work.c.state.not_in(TERMINAL))
                .values(
                    state="waiting_user",
                    reason="native_request",
                    revision=work.c.revision + 1,
                    updated=now,
                )
            )
            await self._tell_parent(session, record, f"delegation:{record['id']}:{event_id}", card)
            return
        if state.get("pending") is False:
            if await self._other_runs_pending(session, record["id"], operation.id):
                return
            native = str(state.get("status") or "")
            ending = _TERMINAL_NATIVE.get(native, "failed" if state.get("error") else None)
            if ending is None:
                return
            checkpoint = (
                func.json_set(work.c.checkpoint_json, "$.sync_result", card)
                if ending == "completed"
                else func.json_set(work.c.checkpoint_json, "$.reason", card)
            )
            await session.execute(
                update(work)
                .where(work.c.id == record["id"], work.c.state.not_in(TERMINAL))
                .values(
                    state=ending,
                    reason=None if ending == "completed" else f"native_{native or 'error'}"[:128],
                    checkpoint_json=checkpoint,
                    revision=work.c.revision + 1,
                    updated=now,
                )
            )
            await self._publish_result(session, record["id"], ending, card)
            return
        # The executor is gone or its result is unknown: told once per fact, never failed.
        reason = str(payload.get("reason") or "executor_lost")[:128]
        await session.execute(
            update(work)
            .where(work.c.id == record["id"], work.c.state.not_in(TERMINAL))
            .values(reason=reason, updated=now)
        )
        await self._tell_parent(
            session, record, f"delegation:{record['id']}:lost:{operation.id}:{reason}", card
        )

    @staticmethod
    async def _publish_result(session: AsyncSession, record_id: str, state: str, card: str) -> None:
        """The result card reaches the parent in the same writer as the settlement.

        Same input (key, shape) and notification watermark as the ordinary subtask
        result path, so the wake that delivers the parent's wait already carries the
        card (taskbook §5.2 P3) and the maintenance pass has nothing left to publish.
        """
        row = (
            (
                await session.execute(
                    select(
                        work.c.revision,
                        work.c.parent_work_id,
                        work.c.conversation_id,
                        work.c.generation,
                    ).where(work.c.id == record_id)
                )
            )
            .mappings()
            .one()
        )
        if row["parent_work_id"] is None:
            return
        text = json.dumps(
            {
                "child_id": record_id,
                "detail": (
                    "这是该子任务的结果，结合原父目标继续处理或按约定交付。"
                    "需要明细时再用 get_tasks(id=原ID) 查询原结果与执行回执。"
                ),
                "state": state,
                "text": card,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        await session.execute(
            insert(inputs)
            .values(
                conversation_id=row["conversation_id"],
                generation=row["generation"],
                source_key=f"worker-result:{record_id}:{row['revision']}",
                work_id=row["parent_work_id"],
                kind="subagent",
                ready=True,
                payload_json=json.dumps({"text": text}, ensure_ascii=False, sort_keys=True),
                created=time.time(),
            )
            .on_conflict_do_nothing(index_elements=[inputs.c.source_key])
        )
        await session.execute(
            update(children)
            .where(children.c.work_id == record_id)
            .values(
                notified_revision=row["revision"],
                result_json=json.dumps(
                    {"child_id": record_id, "state": state, "text": card},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
        await session.execute(
            update(work)
            .where(
                work.c.id == row["parent_work_id"],
                work.c.state == "waiting_external",
                ~select(waits.c.id)
                .where(waits.c.work_id == work.c.id, waits.c.status == "active")
                .exists(),
            )
            .values(state="queued", revision=work.c.revision + 1, updated=time.time())
        )

    async def resolved(self, session: AsyncSession, operation: AgentOperationModel) -> None:
        """A request was answered (by anyone): the delegation runs again."""
        if operation.work_id is None:
            return
        pending = await session.scalar(
            select(func.count())
            .select_from(AgentEventModel)
            .join(AgentOperationModel, AgentOperationModel.id == AgentEventModel.operation_id)
            .where(
                AgentOperationModel.work_id == operation.work_id,
                AgentEventModel.kind.in_(tuple(_REQUESTS)),
            )
        )
        resolutions = await session.scalar(
            select(func.count())
            .select_from(AgentEventModel)
            .join(AgentOperationModel, AgentOperationModel.id == AgentEventModel.operation_id)
            .where(
                AgentOperationModel.work_id == operation.work_id,
                func.json_extract(
                    AgentEventModel.payload_json, "$.payload.permission_resolved"
                ).is_not(None),
            )
        )
        answered = await session.scalar(
            select(func.count())
            .select_from(AgentOperationModel)
            .where(
                AgentOperationModel.work_id == operation.work_id,
                AgentOperationModel.action == "respond",
                func.json_type(AgentOperationModel.result_json, "$.error").is_(None),
                AgentOperationModel.result_json.is_not(None),
            )
        )
        if (pending or 0) > max(resolutions or 0, answered or 0):
            return
        await session.execute(
            update(work)
            .where(work.c.id == operation.work_id, work.c.state == "waiting_user")
            .values(
                state="waiting_external",
                reason="native_executor",
                revision=work.c.revision + 1,
                updated=time.time(),
            )
        )

    async def device_offline(
        self, session: AsyncSession, operation: AgentOperationModel, event_id: str, card: str
    ) -> None:
        record = (
            (await session.execute(select(work).where(work.c.id == operation.work_id)))
            .mappings()
            .first()
        )
        if record is None or record["state"] in TERMINAL:
            return
        executor = await executor_of(session, record["id"])
        await session.execute(
            update(work)
            .where(work.c.id == record["id"])
            .values(reason="device_offline", updated=time.time())
        )
        if executor is not None and executor["relationship"] == "observe":
            await self._notice(session, record, f"{event_id}:{record['id']}", card)
        else:
            await self._tell_parent(session, record, f"delegation:{record['id']}:{event_id}", card)

    async def device_online(self, session: AsyncSession, work_id: str) -> None:
        await session.execute(
            update(work)
            .where(work.c.id == work_id, work.c.reason == "device_offline")
            .values(reason="native_executor", updated=time.time())
        )

    @staticmethod
    async def _other_runs_pending(session: AsyncSession, work_id: str, operation_id: str) -> bool:
        return bool(
            await session.scalar(
                select(AgentOperationModel.id)
                .where(
                    AgentOperationModel.work_id == work_id,
                    AgentOperationModel.id != operation_id,
                    AgentOperationModel.run_id.is_not(None),
                    AgentOperationModel.detached_at.is_(None),
                    func.coalesce(
                        func.json_type(AgentOperationModel.result_json, "$.pending"), "true"
                    )
                    != "false",
                )
                .limit(1)
            )
        )

    async def _tell_parent(
        self, session: AsyncSession, record: Any, source_key: str, card: str
    ) -> None:
        """Deliver a card to the parent as the child's own question would be."""
        parent_id = record["parent_work_id"]
        if parent_id is None:
            return
        parent = (
            (await session.execute(select(work).where(work.c.id == parent_id))).mappings().first()
        )
        if parent is None:
            return
        created = await session.scalar(
            insert(inputs)
            .values(
                conversation_id=parent["conversation_id"],
                generation=parent["generation"],
                source_key=source_key,
                work_id=parent_id,
                kind="subagent",
                ready=True,
                payload_json=json.dumps(
                    {"text": card, "child_id": record["id"]}, ensure_ascii=False, sort_keys=True
                ),
                created=time.time(),
            )
            .on_conflict_do_nothing(index_elements=[inputs.c.source_key])
            .returning(inputs.c.id)
        )
        # A suspended or ended parent keeps the card without being revived (§4.6).
        if created is None or parent["state"] not in {"waiting_external", "waiting_user"}:
            return
        await session.execute(
            update(waits)
            .where(waits.c.work_id == parent_id, waits.c.status == "active")
            .values(status="cancelled", updated=time.time())
        )
        await session.execute(
            update(work)
            .where(work.c.id == parent_id, work.c.state.in_(("waiting_external", "waiting_user")))
            .values(
                state="queued",
                reason="delegation_requires_action",
                checkpoint_json=func.json_remove(work.c.checkpoint_json, "$.accepted_control"),
                revision=work.c.revision + 1,
                updated=time.time(),
            )
        )

    async def _observed(
        self,
        session: AsyncSession,
        record: Any,
        executor: dict[str, Any],
        kind: str,
        payload: dict[str, Any],
        state: dict[str, Any],
        card: str,
        event_id: str,
        request_key: str | None,
    ) -> None:
        """Observation wakes narrowly: requests once, whole-turn failure, a lost executor."""
        if kind in _REQUESTS:
            await self._notice(session, record, f"request:{request_key or event_id}", card)
            return
        if payload.get("reason") in {"session_not_found", "not_live"}:
            # The observed session is gone: the observation ends with it (Q3).
            await session.execute(
                update(work)
                .where(work.c.id == record["id"], work.c.state.not_in(TERMINAL))
                .values(
                    state="cancelled",
                    reason="observed_session_gone",
                    revision=work.c.revision + 1,
                    updated=time.time(),
                )
            )
        await self._notice(session, record, f"fact:{event_id}", card)

    async def observed_turn_end(
        self,
        session: AsyncSession,
        operation: AgentOperationModel,
        event_id: str,
        payload: dict[str, Any],
        card_text: str,
    ) -> None:
        """Turn end of an observed session: a failed turn always, a finished one on request."""
        if operation.work_id is None:
            return
        executor = await executor_of(session, operation.work_id)
        if executor is None or executor["relationship"] != "observe":
            return
        status = str(payload.get("execution_status") or "")
        if status == "failed" or (status == "completed" and executor.get("notify_turn_end")):
            record = (
                (await session.execute(select(work).where(work.c.id == operation.work_id)))
                .mappings()
                .first()
            )
            if record is not None and record["state"] not in TERMINAL:
                await self._notice(session, record, f"turn:{event_id}", card_text)

    async def _notice(self, session: AsyncSession, record: Any, key: str, card: str) -> None:
        """One ordinary Main in the original conversation tells the user (no loop here)."""
        source = json.loads(record["source_json"])
        source.pop("work_id", None)
        source.pop("parent_work_id", None)
        source["observation_id"] = record["id"]
        source_key = f"observe-notice:{record['id']}:{key}"[:256]
        identity = str(uuid5(NAMESPACE_URL, f"yuki:{source_key}"))
        now = time.time()
        inserted = await session.scalar(
            insert(work)
            .values(
                id=identity,
                conversation_id=record["conversation_id"],
                generation=record["generation"],
                source_key=source_key,
                source_json=json.dumps(source, ensure_ascii=False, sort_keys=True),
                goal=(
                    f"告诉用户观察记录 {record['id']} 的新情况：只说明发生了什么、在哪里处理；"
                    "不向被观察的会话发送输入，也不回答或拒绝它的请求。"
                ),
                checkpoint_json="{}",
                state="queued",
                created=now,
                updated=now,
            )
            .on_conflict_do_nothing(index_elements=[work.c.source_key])
            .returning(work.c.id)
        )
        if inserted is None:
            return
        await session.execute(
            insert(inputs)
            .values(
                conversation_id=record["conversation_id"],
                generation=record["generation"],
                source_key=f"{source_key}:card"[:256],
                work_id=identity,
                kind="control",
                ready=True,
                payload_json=json.dumps({"text": card}, ensure_ascii=False, sort_keys=True),
                created=now,
            )
            .on_conflict_do_nothing(index_elements=[inputs.c.source_key])
        )

    # ---- projection ---------------------------------------------------------------

    async def card(self, reader: AsyncSession, work_id: str) -> dict[str, Any] | None:
        """The bounded card of one record, as get_tasks and the Client API show it."""
        row = (
            (
                await reader.execute(
                    select(work, children.c.executor_json)
                    .join(children, children.c.work_id == work.c.id)
                    .where(work.c.id == work_id, children.c.executor_json.is_not(None))
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        executor = json.loads(row["executor_json"])
        instance = await reader.get(AgentInstanceModel, executor.get("instance_id"))
        catalog = json.loads(instance.catalog_json) if instance is not None else {}
        operations = list(
            await reader.scalars(
                select(AgentOperationModel)
                .where(AgentOperationModel.work_id == work_id)
                .order_by(AgentOperationModel.created_at, AgentOperationModel.id)
            )
        )
        pending = await pending_requests(reader, work_id, executor["relationship"])
        last_event = await reader.scalar(
            select(func.max(AgentEventModel.created_at))
            .join(AgentOperationModel, AgentOperationModel.id == AgentEventModel.operation_id)
            .where(AgentOperationModel.work_id == work_id)
        )
        events = await reader.scalar(
            select(func.count())
            .select_from(AgentEventModel)
            .join(AgentOperationModel, AgentOperationModel.id == AgentEventModel.operation_id)
            .where(AgentOperationModel.work_id == work_id)
        )
        session_id = executor.get("session_id") or next(
            (item.session_id for item in reversed(operations) if item.session_id), None
        )
        state = delegation_state(row["state"], row["reason"])
        agent = catalog.get("label") or executor.get("agent_label")
        ended = {"completed": "已完成", "failed": "已失败", "cancelled": "已结束"}
        status_line = {
            "running": f"{agent} 正在运行",
            "waiting": f"{agent} 在等待回答",
            "unknown": f"{agent} 状态未知（{row['reason']}）",
        }.get(state, f"{agent} {ended.get(state, state)}")
        if executor["relationship"] == "observe":
            status_line = f"正在观察 {agent} 的会话"
        return {
            "id": work_id,
            "executor": {
                "device_id": executor.get("device_id"),
                "agent": executor.get("instance_id"),
                "label": agent,
                "session": session_id,
            },
            "relationship": executor["relationship"],
            "state": state,
            "status_line": status_line,
            "scope": executor.get("scope"),
            "pending": pending,
            "counts": {"operations": len(operations), "events": events or 0},
            "last_event_at": last_event.replace(tzinfo=UTC).isoformat()
            if last_event is not None
            else None,
            "read": {"get_tasks": {"id": work_id}},
        }

    async def timeline(
        self, reader: AsyncSession, work_id: str, cursor: int, limit: int
    ) -> dict[str, Any]:
        """Dispatch, receipts, native facts and who answered, read from original rows."""
        operations = list(
            await reader.scalars(
                select(AgentOperationModel)
                .where(AgentOperationModel.work_id == work_id)
                .order_by(AgentOperationModel.created_at, AgentOperationModel.id)
            )
        )
        rows = list(
            await reader.scalars(
                select(AgentEventModel)
                .join(AgentOperationModel, AgentOperationModel.id == AgentEventModel.operation_id)
                .where(AgentOperationModel.work_id == work_id, AgentEventModel.id > cursor)
                .order_by(AgentEventModel.id)
                .limit(limit)
            )
        )
        return {
            "operations": [operation_entry(item) for item in operations],
            "events": [
                {
                    "cursor": row.id,
                    "kind": row.kind,
                    "operation_id": row.operation_id,
                    "received_at": row.created_at.replace(tzinfo=UTC).isoformat(),
                    "payload": json.loads(row.payload_json).get("payload"),
                    **({"permission_id": str(row.id)} if row.kind in _REQUESTS else {}),
                }
                for row in rows
            ],
            "next_cursor": rows[-1].id if len(rows) == limit else None,
        }


class DelegationConflict(ValueError):
    """Another live delegation owns this native session (one owner per session)."""

    def __init__(self, owner: str) -> None:
        super().__init__("conflict")
        self.owner = owner


def operation_entry(item: AgentOperationModel) -> dict[str, Any]:
    source = json.loads(item.source_json)
    command = json.loads(item.command_json)
    result = json.loads(item.result_json or "{}")
    entry: dict[str, Any] = {
        "operation_id": item.id,
        "action": item.action,
        "status": result.get("status", "unknown"),
        "pending": result.get("pending", True),
        "created_at": item.created_at.replace(tzinfo=UTC).isoformat(),
        "actor_person_id": item.actor_person_id,
    }
    if item.action == "respond":
        arguments = command.get("arguments", {})
        entry.update(
            answered_by="user" if source.get("answered_via") == "desktop" else "yuki",
            answered_via=source.get("answered_via", "yuki"),
            option=arguments.get("option_id") or arguments.get("decision"),
            reason=source.get("answer_reason"),
            request=source.get("answer_request"),
            error=result.get("error"),
        )
    return {key: value for key, value in entry.items() if value is not None}


async def executor_of(reader: AsyncSession, work_id: str | None) -> dict[str, Any] | None:
    if work_id is None:
        return None
    value = await reader.scalar(
        select(children.c.executor_json).where(children.c.work_id == work_id)
    )
    return json.loads(value) if value else None


async def pending_requests(
    reader: AsyncSession, work_id: str, relationship: str
) -> list[dict[str, Any]]:
    """Requests of this record still awaiting an answer, with who may answer them."""
    rows = list(
        await reader.execute(
            select(
                AgentEventModel.id,
                AgentEventModel.kind,
                AgentEventModel.payload_json,
                AgentEventModel.created_at,
                AgentEventModel.operation_id,
            )
            .join(AgentOperationModel, AgentOperationModel.id == AgentEventModel.operation_id)
            .where(AgentOperationModel.work_id == work_id)
            .order_by(AgentEventModel.id)
        )
    )
    answered = {
        json.loads(command).get("arguments", {}).get("native_request_id")
        for command, result in (
            await reader.execute(
                select(AgentOperationModel.command_json, AgentOperationModel.result_json).where(
                    AgentOperationModel.work_id == work_id,
                    AgentOperationModel.action == "respond",
                )
            )
        ).all()
        if result is not None and json.loads(result).get("error") is None
    }
    pending: dict[str, dict[str, Any]] = {}
    for identity, kind, payload_json, created, operation_id in rows:
        payload = json.loads(payload_json).get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if kind in _REQUESTS:
            native = str(payload.get("native_request_id"))
            if native in answered:
                continue
            pending[native] = {
                "permission_id": str(identity),
                "kind": kind,
                "tool": payload.get("tool"),
                "options": payload.get("options"),
                "operation_id": operation_id,
                "received_at": created.replace(tzinfo=UTC).isoformat(),
                # Observation never answers: the user handles it at the native terminal.
                "answer_at": "native_terminal" if relationship == "observe" else "yuki_or_user",
            }
        elif isinstance(payload.get("permission_resolved"), str):
            pending.pop(payload["permission_resolved"], None)
    return list(pending.values())


async def session_native_id(reader: AsyncSession, session_id: str | None) -> str | None:
    if session_id is None:
        return None
    row = await reader.get(AgentSessionModel, session_id)
    return row.native_id if row is not None else None


def card_text(card: dict[str, Any]) -> str:
    return encode_card(card)
