"""Durable work state; all mutations are fenced by one scope activation lease."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, select, update
from sqlalchemy.dialects.sqlite import insert

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.work_schema_v1 import WORK_STATES, effects, inputs, scope, work

TERMINAL = frozenset({"completed", "failed", "cancelled"})


class WorkConflict(RuntimeError):
    """An obsolete activation or revision cannot change durable work."""


@dataclass(frozen=True, slots=True)
class WorkLease:
    conversation_id: str
    generation: int
    cancel_epoch: int
    fence: int
    owner: str


def bounded_json(value: Any, limit: int = 65536) -> str:
    result = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(result.encode()) > limit:
        raise ValueError("work_record_too_large")
    return result


class WorkRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _fence(lease: WorkLease) -> Any:
        return and_(
            scope.c.conversation_id == lease.conversation_id,
            scope.c.generation == lease.generation,
            scope.c.cancel_epoch == lease.cancel_epoch,
            scope.c.fence == lease.fence,
            scope.c.owner == lease.owner,
            scope.c.lease_until > time.time(),
        )

    async def acquire(
        self, conversation_id: str, generation: int, *, seconds: float = 60
    ) -> WorkLease | None:
        if not 1 <= seconds <= 300:
            raise ValueError("invalid_work_lease_duration")
        now, owner = time.time(), str(uuid4())
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                insert(scope)
                .values(
                    conversation_id=conversation_id,
                    generation=generation,
                )
                .on_conflict_do_nothing(index_elements=[scope.c.conversation_id])
            )
            row = (
                (
                    await session.execute(
                        update(scope)
                        .where(
                            scope.c.conversation_id == conversation_id,
                            scope.c.generation == generation,
                            scope.c.lease_until <= now,
                        )
                        .values(owner=owner, lease_until=now + seconds, fence=scope.c.fence + 1)
                        .returning(scope)
                    )
                )
                .mappings()
                .first()
            )
            return (
                WorkLease(conversation_id, generation, row["cancel_epoch"], row["fence"], owner)
                if row
                else None
            )

    async def renew(self, lease: WorkLease, *, seconds: float = 60) -> bool:
        if not 1 <= seconds <= 300:
            raise ValueError("invalid_work_lease_duration")
        async with self.database.sessions() as session, session.begin():
            return (
                await session.execute(
                    update(scope)
                    .where(self._fence(lease))
                    .values(lease_until=time.time() + seconds)
                    .returning(scope.c.fence)
                )
            ).first() is not None

    async def release(self, lease: WorkLease) -> None:
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                update(scope).where(self._fence(lease)).values(owner=None, lease_until=0)
            )

    async def valid(self, lease: WorkLease) -> bool:
        async with self.database.sessions() as session:
            return (
                await session.execute(select(scope.c.fence).where(self._fence(lease)))
            ).first() is not None

    async def _assert_lease(self, session: Any, lease: WorkLease) -> None:
        # A write obtains SQLite's transaction writer reservation, preventing a
        # cancel/acquire from interleaving between validation and the mutation.
        row = (
            await session.execute(
                update(scope)
                .where(self._fence(lease))
                .values(fence=scope.c.fence)
                .returning(scope.c.fence)
            )
        ).first()
        if row is None:
            raise WorkConflict("work_activation_obsolete")

    async def accept(
        self, lease: WorkLease, *, source_key: str, source: dict[str, Any], goal: str
    ) -> dict[str, Any]:
        if not 1 <= len(goal) <= 8192 or not 1 <= len(source_key) <= 256:
            raise ValueError("invalid_work_goal")
        source_json, now = bounded_json(source), time.time()
        async with self.database.sessions() as session, session.begin():
            await self._assert_lease(session, lease)
            await session.execute(
                insert(work)
                .values(
                    id=str(uuid4()),
                    conversation_id=lease.conversation_id,
                    generation=lease.generation,
                    source_key=source_key,
                    source_json=source_json,
                    goal=goal,
                    state="running",
                    created=now,
                    updated=now,
                )
                .on_conflict_do_nothing(index_elements=[work.c.source_key])
            )
            row = (
                (await session.execute(select(work).where(work.c.source_key == source_key)))
                .mappings()
                .one()
            )
            if (
                row["conversation_id"] != lease.conversation_id
                or row["generation"] != lease.generation
                or row["source_json"] != source_json
            ):
                raise WorkConflict("work_source_conflict")
            return dict(row)

    async def get(self, identity: str) -> dict[str, Any] | None:
        async with self.database.sessions() as session:
            row = (
                (await session.execute(select(work).where(work.c.id == identity)))
                .mappings()
                .first()
            )
            return dict(row) if row else None

    async def active(self, conversation_id: str, generation: int) -> list[dict[str, Any]]:
        async with self.database.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(work)
                        .where(
                            work.c.conversation_id == conversation_id,
                            work.c.generation == generation,
                            work.c.state.not_in(TERMINAL),
                        )
                        .order_by(work.c.created)
                        .limit(32)
                    )
                )
                .mappings()
                .all()
            )
            return [dict(row) for row in rows]

    async def transition(
        self,
        lease: WorkLease,
        identity: str,
        revision: int,
        state: str,
        *,
        reason: str | None = None,
        goal: str | None = None,
    ) -> dict[str, Any]:
        if state not in WORK_STATES or (goal is not None and not 1 <= len(goal) <= 8192):
            raise ValueError("invalid_work_transition")
        if reason is not None and len(reason) > 128:
            raise ValueError("invalid_work_reason")
        values: dict[str, Any] = {
            "state": state,
            "reason": reason,
            "revision": revision + 1,
            "updated": time.time(),
        }
        if goal is not None:
            values["goal"] = goal
        async with self.database.sessions() as session, session.begin():
            await self._assert_lease(session, lease)
            row = (
                (
                    await session.execute(
                        update(work)
                        .where(
                            work.c.id == identity,
                            work.c.conversation_id == lease.conversation_id,
                            work.c.generation == lease.generation,
                            work.c.revision == revision,
                            work.c.state.not_in(TERMINAL),
                        )
                        .values(**values)
                        .returning(work)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise WorkConflict("work_revision_conflict")
            return dict(row)

    async def checkpoint(
        self,
        lease: WorkLease,
        identity: str,
        payload: dict[str, Any],
        *,
        models: int = 0,
        tools: int = 0,
        messages: int = 0,
    ) -> None:
        if min(models, tools, messages) < 0:
            raise ValueError("invalid_work_usage")
        serialized = bounded_json(payload, 1024 * 1024)
        async with self.database.sessions() as session, session.begin():
            await self._assert_lease(session, lease)
            row = (
                await session.execute(
                    update(work)
                    .where(
                        work.c.id == identity,
                        work.c.conversation_id == lease.conversation_id,
                        work.c.generation == lease.generation,
                        work.c.state.not_in(TERMINAL),
                    )
                    .values(
                        checkpoint_json=serialized,
                        updated=time.time(),
                        model_requests=work.c.model_requests + models,
                        tool_calls=work.c.tool_calls + tools,
                        sent_messages=work.c.sent_messages + messages,
                    )
                    .returning(work.c.id)
                )
            ).first()
            if row is None:
                raise WorkConflict("work_checkpoint_obsolete")

    async def enqueue(
        self,
        conversation_id: str,
        generation: int,
        source_key: str,
        *,
        kind: str,
        event_id: int | None = None,
        work_id: str | None = None,
    ) -> int:
        if not 1 <= len(source_key) <= 256 or kind not in {"message", "completion", "control"}:
            raise ValueError("invalid_work_input")
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                insert(inputs)
                .values(
                    conversation_id=conversation_id,
                    generation=generation,
                    source_key=source_key,
                    kind=kind,
                    event_id=event_id,
                    work_id=work_id,
                    created=time.time(),
                )
                .on_conflict_do_nothing(index_elements=[inputs.c.source_key])
            )
            row = (
                (await session.execute(select(inputs).where(inputs.c.source_key == source_key)))
                .mappings()
                .one()
            )
            if any(
                row[key] != value
                for key, value in {
                    "conversation_id": conversation_id,
                    "generation": generation,
                    "kind": kind,
                    "event_id": event_id,
                    "work_id": work_id,
                }.items()
            ):
                raise WorkConflict("work_input_conflict")
            return int(row["id"])

    async def pending(self, lease: WorkLease, *, limit: int = 8) -> list[dict[str, Any]]:
        async with self.database.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(inputs)
                        .where(
                            inputs.c.conversation_id == lease.conversation_id,
                            inputs.c.generation == lease.generation,
                            inputs.c.state == "pending",
                        )
                        .order_by(inputs.c.id)
                        .limit(min(32, max(1, limit)))
                    )
                )
                .mappings()
                .all()
            )
            return [dict(row) for row in rows]

    async def stage(self, lease: WorkLease, ids: list[int], attempt_id: str) -> None:
        if not ids or len(set(ids)) != len(ids) or len(ids) > 32:
            raise ValueError("invalid_work_input_batch")
        async with self.database.sessions() as session, session.begin():
            await self._assert_lease(session, lease)
            rows = (
                await session.execute(
                    update(inputs)
                    .where(
                        inputs.c.id.in_(ids),
                        inputs.c.conversation_id == lease.conversation_id,
                        inputs.c.generation == lease.generation,
                        inputs.c.state == "pending",
                    )
                    .values(state="staged", attempt_id=attempt_id)
                    .returning(inputs.c.id)
                )
            ).all()
            if len(rows) != len(ids):
                raise WorkConflict("work_input_already_staged")

    async def consume(self, lease: WorkLease, attempt_id: str) -> None:
        async with self.database.sessions() as session, session.begin():
            await self._assert_lease(session, lease)
            await session.execute(
                update(inputs)
                .where(
                    inputs.c.conversation_id == lease.conversation_id,
                    inputs.c.generation == lease.generation,
                    inputs.c.attempt_id == attempt_id,
                    inputs.c.state == "staged",
                )
                .values(state="consumed")
            )

    async def cancel(self, conversation_id: str, *, generation: int | None = None) -> None:
        """Hard boundary only; ordinary arrivals never revoke activation leases."""
        async with self.database.sessions() as session, session.begin():
            values: dict[str, Any] = {
                "cancel_epoch": scope.c.cancel_epoch + 1,
                "fence": scope.c.fence + 1,
                "owner": None,
                "lease_until": 0,
            }
            if generation is not None:
                values["generation"] = generation
            await session.execute(
                update(scope).where(scope.c.conversation_id == conversation_id).values(**values)
            )
            await session.execute(
                update(work)
                .where(
                    work.c.conversation_id == conversation_id,
                    work.c.state.not_in(TERMINAL),
                )
                .values(
                    state="cancelled",
                    reason="hard_boundary",
                    revision=work.c.revision + 1,
                    updated=time.time(),
                )
            )
            await session.execute(
                update(inputs)
                .where(
                    inputs.c.conversation_id == conversation_id,
                    inputs.c.state.in_(("pending", "staged")),
                )
                .values(state="cancelled")
            )

    async def prepare_effect(self, lease: WorkLease, identity: str, key: str, kind: str) -> bool:
        """False means an intent already exists, not that it is safe to send again."""
        now = time.time()
        async with self.database.sessions() as session, session.begin():
            await self._assert_lease(session, lease)
            row = (
                await session.execute(
                    select(work.c.id).where(
                        work.c.id == identity,
                        work.c.conversation_id == lease.conversation_id,
                        work.c.generation == lease.generation,
                        work.c.state.not_in(TERMINAL),
                    )
                )
            ).first()
            if row is None:
                raise WorkConflict("work_effect_obsolete")
            inserted = (
                await session.execute(
                    insert(effects)
                    .values(
                        effect_key=key,
                        work_id=identity,
                        kind=kind,
                        state="prepared",
                        created=now,
                        updated=now,
                    )
                    .on_conflict_do_nothing(index_elements=[effects.c.effect_key])
                    .returning(effects.c.effect_key)
                )
            ).first()
            return inserted is not None

    async def record_effect(self, key: str, state: str, receipt: dict[str, Any]) -> None:
        # A late receipt must survive cancellation. It records an already-issued
        # effect, never authorizes another one, so no current lease is required.
        if state not in {"accepted", "failed", "unknown"}:
            raise ValueError("invalid_work_effect_state")
        serialized = bounded_json(receipt)
        async with self.database.sessions() as session, session.begin():
            row = (
                await session.execute(
                    update(effects)
                    .where(
                        effects.c.effect_key == key,
                        effects.c.state.in_(("prepared", "unknown")),
                    )
                    .values(state=state, receipt_json=serialized, updated=time.time())
                    .returning(effects.c.effect_key)
                )
            ).first()
            if row is None:
                existing = (
                    (await session.execute(select(effects).where(effects.c.effect_key == key)))
                    .mappings()
                    .first()
                )
                if (
                    not existing
                    or existing["state"] != state
                    or existing["receipt_json"] != serialized
                ):
                    raise WorkConflict("work_effect_receipt_conflict")
