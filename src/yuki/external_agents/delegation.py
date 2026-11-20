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

from sqlalchemy import ColumnElement, func, or_, select, update
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
_RUNNING_REASONS = frozenset({None, "native_executor", "native_followup", "native_request"})
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
    """Card state: Yuki's settlement, not a native status string.

    While waiting on its executor, any recorded loss fact (device offline, a lost SDK
    or ACP process, an unknown result) makes the record unknown, never running.
    """
    if state == "waiting_user":
        return "waiting"
    if state in TERMINAL:
        return state
    if reason in _RUNNING_REASONS:
        return "running"
    return "unknown"


def resolution_key(payload: dict[str, Any]) -> str | None:
    """The native request a progress fact says is resolved (by anyone)."""
    if payload.get("permission_resolved") is not None:
        return str(payload["permission_resolved"])
    native = payload.get("native")
    if payload.get("native_method") == "serverRequest/resolved" and isinstance(native, dict):
        return str(native.get("requestId"))
    return None


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
            # A re-delivered native request (same id, new event) is told once.
            await self._tell_parent(
                session,
                record,
                f"delegation:{record['id']}:request:{request_key or event_id}"[:256],
                card,
            )
            return
        if final_receipt(state):
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
            session, record, f"delegation:{record['id']}:lost:{operation.id}"[:256], card
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
        """A request was answered (by anyone): the delegation runs again once none is left.

        Reads only this record's own requests and answers (indexed by its operations).
        """
        if operation.work_id is None:
            return
        if await pending_requests(session, operation.work_id, "delegate"):
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

    async def unowned(
        self, session: AsyncSession, session_id: str, event: dict[str, Any], permission_id: str
    ) -> None:
        """A wait no operation received goes to the record that last held the session.

        Only metadata was uploaded, so it is answerable only with deny (§4.5, §15.2 #9):
        a delegation's parent gets the card; an observation's user is told where it is.
        A session that record released is not asked about: the release settles it (#645).
        """
        from yuki.external_agents.cards import external_agent_card

        if await release_in_effect(session, session_id) is not None:
            return  # Given back: the release settles the wait (#645).
        last = await last_holder(session, session_id)
        if last is None:
            return
        record = (await session.execute(select(work).where(work.c.id == last))).mappings().first()
        executor = await executor_of(session, last)
        if record is None or executor is None:
            return
        observe = executor["relationship"] == "observe"
        payload = event["payload"]
        card = external_agent_card(
            event=event["kind"],
            operation_id=None,
            run_id=None,
            relationship="observe" if observe else "unowned",
            status="requires_action",
            payload={key: payload[key] for key in ("tool", "options") if key in payload},
            permission_id=None if observe else permission_id,
            session_id=session_id,
            read={"get_tasks": {"id": last}},
        )
        key = f"unowned:{payload.get('native_request_id') or event['event_id']}"
        if observe:
            await self._notice(session, record, key, card)
        else:
            await self._tell_parent(session, record, f"delegation:{last}:{key}"[:256], card)

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
        if (
            payload.get("reason") in {"session_not_found", "not_live"}
            or state.get("pending") is False
        ):
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
        # A notice not yet started takes the new fact too: one Main tells both (§4.5).
        waiting = await session.scalar(
            select(work.c.id).where(
                work.c.conversation_id == record["conversation_id"],
                work.c.state == "queued",
                work.c.source_key.like(f"observe-notice:{record['id']}:%"),
            )
        )
        if waiting is not None:
            await session.execute(
                insert(inputs)
                .values(
                    conversation_id=record["conversation_id"],
                    generation=record["generation"],
                    source_key=f"{source_key}:card"[:256],
                    work_id=waiting,
                    kind="control",
                    ready=True,
                    payload_json=json.dumps({"text": card}, ensure_ascii=False, sort_keys=True),
                    created=now,
                )
                .on_conflict_do_nothing(index_elements=[inputs.c.source_key])
            )
            return
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


async def last_holder(reader: AsyncSession, session_id: str) -> str | None:
    """The delegation or observation record that most recently acted on this session.

    Answering a request (``respond``) acts on the holder's own wait; it never makes
    the answering record the holder.
    """
    return await reader.scalar(
        select(AgentOperationModel.work_id)
        .join(children, children.c.work_id == AgentOperationModel.work_id)
        .where(
            AgentOperationModel.session_id == session_id,
            AgentOperationModel.action != "respond",
            children.c.executor_json.is_not(None),
        )
        .order_by(AgentOperationModel.created_at.desc())
        .limit(1)
    )


async def release_in_effect(reader: AsyncSession, session_id: str) -> AgentOperationModel | None:
    """The release that still governs this session, if any (#645).

    The latest detach carrying ``release``, unless a record that can answer (a
    delegation or takeover, not an observation) gave the session input after it: a
    resume of the same record or another delegation ends it. Answers, including the
    release's own, and observations never do. A release detach is registered before
    it is sent, and a connector turns a wait unowned only after it arrives.
    """
    release = (
        await reader.scalars(
            select(AgentOperationModel)
            .where(
                AgentOperationModel.session_id == session_id,
                AgentOperationModel.action == "detach",
                func.json_extract(AgentOperationModel.command_json, "$.arguments.release") == 1,
            )
            .order_by(AgentOperationModel.created_at.desc(), AgentOperationModel.id.desc())
            .limit(1)
        )
    ).first()
    if release is None:
        return None
    relationship = func.coalesce(
        func.json_extract(children.c.executor_json, "$.relationship"), "delegate"
    )
    later = await reader.scalar(
        select(AgentOperationModel.id)
        .join(children, children.c.work_id == AgentOperationModel.work_id)
        .where(
            AgentOperationModel.session_id == session_id,
            AgentOperationModel.action.in_(("prompt", "queue", "steer", "watch")),
            AgentOperationModel.created_at > release.created_at,
            relationship != "observe",
        )
        .limit(1)
    )
    return release if later is None else None


async def release_reference(reader: AsyncSession, row: AgentOperationModel) -> bool:
    """Whether a row names, by ID, the release it settles for (#645).

    The named row must be this record's own detach of this session carrying
    ``release``; a ``settled_by`` marker without that fact grants nothing.
    """
    source = json.loads(row.source_json)
    named = source.get("release_operation_id")
    if (
        source.get("settled_by") != "release"
        or not isinstance(named, str)
        or row.work_id is None
        or row.session_id is None
    ):
        return False
    release = await reader.get(AgentOperationModel, named)
    return (
        release is not None
        and release.action == "detach"
        and release.work_id == row.work_id
        and release.session_id == row.session_id
        and json.loads(release.command_json).get("arguments", {}).get("release") is True
    )


async def release_answer(reader: AsyncSession, row: AgentOperationModel) -> bool:
    """Whether a registered command is a release's deny of a session wait (#645).

    Only facts decide: a deny of a wait no operation received (no target operation,
    no response body), naming the record's own release of the session, while that
    record's release is still in effect (no resume, no other delegation since).
    """
    arguments = json.loads(row.command_json).get("arguments", {})
    if (
        row.action != "respond"
        or arguments.get("option_id") != "deny"
        or "response" in arguments
        or "operation_id" in arguments
        or row.session_id is None
        or not await release_reference(reader, row)
    ):
        return False
    release = await release_in_effect(reader, row.session_id)
    return release is not None and release.work_id == row.work_id


async def waiting_on_parent(reader: AsyncSession, work_id: str) -> list[dict[str, Any]]:
    """Requests a live delegation record is waiting on its parent to answer (or deny).

    Read from facts (events and answers), never from whether a card was consumed. An
    observation's waits are handled at the native terminal and are never included.
    """
    executor = await executor_of(reader, work_id)
    if executor is None or executor["relationship"] == "observe":
        return []
    state = await reader.scalar(select(work.c.state).where(work.c.id == work_id))
    if state is None or state in TERMINAL:
        return []
    return [
        request
        for request in await pending_requests(reader, work_id, executor["relationship"])
        if request["answer_at"] in {"yuki_or_user", "deny_only"}
    ]


def final_receipt(result: dict[str, Any]) -> bool:
    """Nothing more follows this answer: the run ended, or the command was refused.

    A connector's refusal (admitted false, nothing executed) carries no "pending".
    """
    return result.get("pending") is False or result.get("admitted") is False


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
    answered = await _answered(reader, AgentOperationModel.work_id == work_id)
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
        elif (resolved := resolution_key(payload)) is not None:
            pending.pop(resolved, None)
    if relationship != "observe":
        pending.update(await _unowned_waits(reader, work_id, answered))
    return list(pending.values())


def answer_taken() -> ColumnElement[bool]:
    """A respond counts while in flight (no receipt yet), once the native callback took
    it, or once the device said that callback is gone (``request_resolved``).

    Any other unknown or failed receipt leaves the request pending and answerable
    again; the native owner still lets only a still-pending request take an answer.
    """
    return or_(
        AgentOperationModel.result_json.is_(None),
        func.json_type(AgentOperationModel.result_json, "$.response_submitted") == "true",
        func.json_extract(AgentOperationModel.result_json, "$.error.code") == "request_resolved",
        func.json_extract(AgentOperationModel.result_json, "$.error") == "request_resolved",
    )


async def _answered(reader: AsyncSession, scope: ColumnElement[bool]) -> set[str]:
    return {
        str(json.loads(command).get("arguments", {}).get("native_request_id"))
        for command in await reader.scalars(
            select(AgentOperationModel.command_json).where(
                scope, AgentOperationModel.action == "respond", answer_taken()
            )
        )
    }


async def _unowned_waits(
    reader: AsyncSession, work_id: str, answered: set[str]
) -> dict[str, dict[str, Any]]:
    """Waits no operation received on the sessions this record held (deny only).

    Read through the session index (migration 0159), never by scanning payloads.
    """
    sessions = list(
        (
            await reader.execute(
                select(
                    AgentSessionModel.id,
                    AgentSessionModel.instance_id,
                    AgentSessionModel.native_id,
                )
                .join(AgentOperationModel, AgentOperationModel.session_id == AgentSessionModel.id)
                .where(AgentOperationModel.work_id == work_id)
                .distinct()
            )
        ).all()
    )
    # The parent's deny of such a wait names the session, not one of this record's operations.
    answered = answered | await _answered(
        reader, AgentOperationModel.session_id.in_([row[0] for row in sessions])
    )
    waits_found: dict[str, dict[str, Any]] = {}
    for _, instance_id, native_session in sessions:
        rows = (
            await reader.execute(
                select(
                    AgentEventModel.id,
                    AgentEventModel.kind,
                    AgentEventModel.payload_json,
                    AgentEventModel.created_at,
                )
                .where(
                    AgentEventModel.instance_id == instance_id,
                    AgentEventModel.native_session_id == native_session,
                    AgentEventModel.operation_id.is_(None),
                )
                .order_by(AgentEventModel.id)
            )
        ).all()
        for identity, kind, payload_json, created in rows:
            payload = json.loads(payload_json).get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if kind in _REQUESTS and payload.get("unowned") is True:
                native = str(payload.get("native_request_id"))
                if native not in answered:
                    waits_found[native] = {
                        "permission_id": str(identity),
                        "kind": kind,
                        "tool": payload.get("tool"),
                        "options": ["deny"],
                        "received_at": created.replace(tzinfo=UTC).isoformat(),
                        "answer_at": "deny_only",
                    }
            elif (resolved := resolution_key(payload)) is not None:
                waits_found.pop(resolved, None)
    return waits_found


async def session_native_id(reader: AsyncSession, session_id: str | None) -> str | None:
    if session_id is None:
        return None
    row = await reader.get(AgentSessionModel, session_id)
    return row.native_id if row is not None else None


def card_text(card: dict[str, Any]) -> str:
    return encode_card(card)


def receipt_final(receipt: dict[str, Any] | None) -> bool:
    """No later result will come: ``pending`` false or a refusal (``admitted`` false)."""
    return receipt is not None and (
        receipt.get("pending") is False or receipt.get("admitted") is False
    )


def tool_name(tool: Any) -> str | None:
    """Only a tool's name: an ACP tool is a whole ToolCallUpdate (title, rawInput)."""
    name = tool.get("kind") or tool.get("name") if isinstance(tool, dict) else tool
    return name[:64] if isinstance(name, str) and name else None


def _epoch(value: Any) -> float:
    return float(
        value.replace(tzinfo=UTC).timestamp() if value.tzinfo is None else value.timestamp()
    )


async def watch_records(reader: AsyncSession) -> list[dict[str, Any]]:
    """Safe facts of every live delegation/observation record, for the watchdog.

    IDs, states, receipt finality and times only; never native payloads or briefs.
    Bounded by the live records (few) and their operations, through existing indexes.
    """
    rows = list(
        (
            await reader.execute(
                select(
                    work.c.id,
                    work.c.state,
                    work.c.reason,
                    work.c.updated,
                    work.c.parent_work_id,
                    children.c.executor_json,
                )
                .join(children, children.c.work_id == work.c.id)
                .where(children.c.executor_json.is_not(None), work.c.state.not_in(TERMINAL))
            )
        ).mappings()
    )
    records: list[dict[str, Any]] = []
    for row in rows:
        executor = json.loads(row["executor_json"])
        operations = []
        for operation in await reader.scalars(
            select(AgentOperationModel).where(AgentOperationModel.work_id == row["id"])
        ):
            receipt = json.loads(operation.result_json) if operation.result_json else None
            operations.append(
                {
                    "id": operation.id,
                    "action": operation.action,
                    "created": _epoch(operation.created_at),
                    "final": receipt_final(receipt),
                    "unknown": receipt is None or receipt.get("status", "unknown") == "unknown",
                }
            )
        # Newest event per operation through ``(operation_id, id)``; no event scan.
        newest = [
            await reader.scalar(
                select(func.max(AgentEventModel.id)).where(
                    AgentEventModel.operation_id == operation["id"]
                )
            )
            for operation in operations
        ]
        last_id = max((identity for identity in newest if identity is not None), default=None)
        last_event = (
            await reader.scalar(
                select(AgentEventModel.created_at).where(AgentEventModel.id == last_id)
            )
            if last_id is not None
            else None
        )
        # Requests exist only while the record waits for an answer (``waiting_user``);
        # only then are its events and answers read.
        waiting = (
            await waiting_on_parent(reader, row["id"]) if row["state"] == "waiting_user" else []
        )
        records.append(
            {
                "id": row["id"],
                "state": row["state"],
                "reason": row["reason"],
                "updated": float(row["updated"]),
                "parent_work_id": row["parent_work_id"],
                "relationship": executor.get("relationship"),
                "device_id": executor.get("device_id"),
                "operations": operations,
                "last_event": _epoch(last_event) if last_event is not None else None,
                "waiting_on_parent": [
                    {
                        "permission_id": item["permission_id"],
                        "kind": item["kind"],
                        "tool": tool_name(item.get("tool")),
                        "received_at": item["received_at"],
                    }
                    for item in waiting
                ],
            }
        )
    return records
