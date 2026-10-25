"""Transactional persistence and lease coordination for automations."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter
from sqlalchemy import and_, false, func, or_, select, true, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.automation.authority import AutomationScene
from yuki.automation.models import (
    AutomationCreatorIdentity,
    AutomationDirectoryEntry,
    AutomationDirectoryRecord,
    AutomationRecord,
    AutomationRunRecord,
    AutomationStatus,
    RunStatus,
    Schedule,
)
from yuki.automation.task_spec import TaskSpec, canonical_task_hash
from yuki.identity.db_models import (
    CanonicalPersonModel,
    PresenceModel,
)
from yuki.identity.person_identity import (
    person_family_condition,
    person_root_id,
    same_person_id,
)
from yuki.persistence.database import Database
from yuki.persistence.models import (
    AutomationModel,
    AutomationRunModel,
    AutomationVersionModel,
)
from yuki.persistence.unit_of_work import optional_session


@dataclass(frozen=True, slots=True)
class PreparedTaskDefinition:
    """The encoded task/version and its original public values, prepared before writing."""

    task: TaskSpec
    authority: AutomationScene
    script_json: str
    authority_json: str
    script_hash: str
    script: dict[str, Any]
    authority_snapshot: dict[str, Any]

    @classmethod
    def prepare(cls, task: TaskSpec, authority: AutomationScene) -> PreparedTaskDefinition:
        return cls(
            task,
            authority,
            task.model_dump_json(exclude_none=True),
            authority.model_dump_json(),
            canonical_task_hash(task),
            task.model_dump(mode="json", exclude_none=True),
            authority.model_dump(mode="json"),
        )


class AutomationRepository:
    """Own automation rows, immutable versions, runs, and leases."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @staticmethod
    def _owner_clause(owner_id: str) -> Any:
        if owner_id == "self":
            return AutomationModel.creator_kind == "self"
        return and_(
            AutomationModel.creator_kind == "person",
            person_family_condition(AutomationModel.canonical_creator_person_id, owner_id),
        )

    async def create(
        self,
        definition: PreparedTaskDefinition,
        *,
        creator_person_id: str,
        max_runs: int | None,
        next_run_at: datetime,
        now: datetime,
        creation_source_key: str | None = None,
        source_event_id: int | None = None,
        session: AsyncSession | None = None,
    ) -> AutomationRecord:
        authority = definition.authority
        timestamp = _aware_utc(now)
        async with optional_session(self._database, session, write=True) as active:
            if creation_source_key is not None:
                previous = await self.get_by_creation_key(
                    creator_person_id, creation_source_key, session=active
                )
                if previous is not None:
                    initial_hash = await self.initial_script_hash(previous.id, session=active)
                    if (
                        initial_hash != definition.script_hash
                        or previous.creation_max_runs != max_runs
                    ):
                        raise ValueError("automation_creation_key_conflict")
                    return previous
            if creator_person_id != "self":
                creator = await active.get(CanonicalPersonModel, creator_person_id)
                if (
                    creator is None
                    or not creator.enabled
                    or authority.canonical_creator_person_id is None
                    or not await same_person_id(
                        active, authority.canonical_creator_person_id, creator_person_id
                    )
                ):
                    raise ValueError("创建者没有对应的永久主体")
            if authority.principal_kind != ("self" if creator_person_id == "self" else "person"):
                raise ValueError("invalid_automation_owner")
            presence = await active.get(PresenceModel, authority.canonical_presence_id)
            if presence is None:
                raise ValueError("automation_presence_unavailable")
            row = AutomationModel(
                creator_kind=authority.principal_kind,
                creator_user_id=authority.creator_user_id,
                bot_user_id=authority.bot_user_id,
                status=AutomationStatus.ACTIVE.value,
                created_from_message_id=authority.created_from_message_id,
                creation_source_key=creation_source_key,
                source_event_id=source_event_id,
                canonical_conversation_id=authority.canonical_conversation_id,
                next_run_at=next_run_at,
                last_run_at=None,
                run_count=0,
                max_runs=max_runs,
                creation_max_runs=max_runs,
                claimed_by=None,
                claimed_until=None,
                created_at=timestamp,
                updated_at=timestamp,
                canonical_creator_person_id=None
                if creator_person_id == "self"
                else creator_person_id,
                canonical_presence_id=presence.id,
            )
            active.add(row)
            await active.flush()
            version = AutomationVersionModel(
                automation_id=row.id,
                version=1,
                script_json=definition.script_json,
                script_hash=definition.script_hash,
                authority_snapshot_json=definition.authority_json,
                canonical_presence_id=presence.id,
                updated_by="",
                created_at=timestamp,
            )
            active.add(version)
            await active.flush()
            return _automation_record(row, version, definition=definition)

    async def get(
        self, automation_id: int, *, session: AsyncSession | None = None
    ) -> AutomationRecord | None:
        async with optional_session(self._database, session, write=False) as active:
            pair = (
                await active.execute(
                    _definition_statement().where(AutomationModel.id == automation_id)
                )
            ).one_or_none()
        return _automation_record(*pair) if pair else None

    async def get_for_run(
        self, run_id: int | None, *, session: AsyncSession | None = None
    ) -> AutomationRecord | None:
        """Read only the immutable definition accepted by this occurrence."""
        async with optional_session(self._database, session, write=False) as active:
            pair = (
                await active.execute(
                    select(AutomationModel, AutomationVersionModel)
                    .join(
                        AutomationRunModel, AutomationRunModel.automation_id == AutomationModel.id
                    )
                    .join(
                        AutomationVersionModel,
                        AutomationVersionModel.id == AutomationRunModel.version_id,
                    )
                    .where(AutomationRunModel.id == run_id)
                )
            ).one_or_none()
        if pair is None or pair[1].authority_snapshot_json is None:
            return None
        return _automation_record(*pair)

    async def get_by_creation_key(
        self, creator_person_id: str, key: str, *, session: AsyncSession | None = None
    ) -> AutomationRecord | None:
        async with optional_session(self._database, session, write=False) as active:
            pair = (
                await active.execute(
                    _definition_statement().where(
                        self._owner_clause(creator_person_id),
                        AutomationModel.creation_source_key == key,
                    )
                )
            ).first()
        return _automation_record(*pair) if pair else None

    async def initial_script_hash(
        self, automation_id: int, *, session: AsyncSession | None = None
    ) -> str | None:
        """The immutable first publication identifies an original creation."""
        async with optional_session(self._database, session, write=False) as active:
            return cast(
                str | None,
                await active.scalar(
                    select(AutomationVersionModel.script_hash)
                    .where(AutomationVersionModel.automation_id == automation_id)
                    .order_by(AutomationVersionModel.version)
                    .limit(1)
                ),
            )

    async def original_creation(
        self, automation_id: int
    ) -> tuple[dict[str, Any], dict[str, Any], str] | None:
        async with self._database.read_session() as session:
            version = await session.scalar(
                select(AutomationVersionModel)
                .where(AutomationVersionModel.automation_id == automation_id)
                .order_by(AutomationVersionModel.version)
                .limit(1)
            )
        if version is None:
            return None
        return (
            json.loads(version.script_json),
            json.loads(version.authority_snapshot_json) if version.authority_snapshot_json else {},
            version.script_hash,
        )

    async def list_directory(
        self, *, statuses: tuple[AutomationStatus, ...], limit: int = 50, offset: int = 0
    ) -> tuple[AutomationDirectoryEntry, ...]:
        query = _directory_statement().where(
            AutomationModel.status.in_([status.value for status in statuses])
        )
        current = set(statuses) <= {AutomationStatus.ACTIVE, AutomationStatus.PAUSED}
        order = (
            (AutomationModel.next_run_at.is_(None), AutomationModel.next_run_at, AutomationModel.id)
            if current
            else (AutomationModel.updated_at.desc(), AutomationModel.id.desc())
        )
        async with self._database.read_session() as session:
            rows = (await session.execute(query.order_by(*order).offset(offset).limit(limit))).all()
        return tuple(_automation_directory_entry(row) for row in rows)

    async def list_current_for_creator(
        self, creator_person_id: str, *, limit: int = 100
    ) -> tuple[AutomationRecord, ...]:
        async with self._database.read_session() as session:
            rows = (
                await session.execute(
                    _definition_statement()
                    .where(
                        self._owner_clause(creator_person_id),
                        AutomationModel.status.in_(("active", "paused")),
                    )
                    .order_by(
                        AutomationModel.next_run_at.is_(None),
                        AutomationModel.next_run_at,
                        AutomationModel.id,
                    )
                    .limit(limit)
                )
            ).all()
        return tuple(_automation_record(*row) for row in rows)

    async def list_terminal_for_creator(
        self, creator_person_id: str, *, limit: int = 100
    ) -> tuple[AutomationRecord, ...]:
        async with self._database.read_session() as session:
            rows = (
                await session.execute(
                    _definition_statement()
                    .where(
                        self._owner_clause(creator_person_id),
                        AutomationModel.status.in_(("completed", "cancelled", "failed", "blocked")),
                    )
                    .order_by(AutomationModel.updated_at.desc(), AutomationModel.id.desc())
                    .limit(limit)
                )
            ).all()
        return tuple(_automation_record(*row) for row in rows)

    async def active_count(
        self, creator_person_id: str | None = None, *, session: AsyncSession | None = None
    ) -> int:
        query = select(func.count(AutomationModel.id)).where(
            AutomationModel.status == AutomationStatus.ACTIVE.value
        )
        if creator_person_id is not None:
            query = query.where(self._owner_clause(creator_person_id))
        async with optional_session(self._database, session, write=False) as active:
            return int(await active.scalar(query) or 0)

    async def set_status(
        self,
        automation_id: int,
        *,
        creator_person_id: str,
        status: AutomationStatus,
        now: datetime,
        session: AsyncSession | None = None,
    ) -> bool:
        values: dict[str, Any] = {
            "status": status.value,
            "updated_at": _aware_utc(now),
        }
        if status in {AutomationStatus.CANCELLED, AutomationStatus.COMPLETED}:
            values["next_run_at"] = None
            values["claimed_by"] = None
            values["claimed_until"] = None
        async with optional_session(self._database, session, write=True) as active:
            result = await active.execute(
                update(AutomationModel)
                .where(
                    AutomationModel.id == automation_id,
                    self._owner_clause(creator_person_id),
                    AutomationModel.status.not_in(
                        [AutomationStatus.CANCELLED.value, AutomationStatus.COMPLETED.value]
                    ),
                )
                .values(**values)
            )
        return bool(cast(CursorResult[Any], result).rowcount)

    async def cancel(
        self,
        automation_id: int,
        *,
        creator_person_id: str,
        now: datetime,
        session: AsyncSession | None = None,
    ) -> bool:
        async with optional_session(self._database, session, write=True) as active:
            result = await active.execute(
                update(AutomationModel)
                .where(
                    AutomationModel.id == automation_id,
                    self._owner_clause(creator_person_id),
                    AutomationModel.status != AutomationStatus.CANCELLED.value,
                )
                .values(
                    status=AutomationStatus.CANCELLED.value,
                    next_run_at=None,
                    updated_at=_aware_utc(now),
                )
            )
            return cast(CursorResult[Any], result).rowcount == 1

    async def resume(
        self,
        automation_id: int,
        *,
        creator_person_id: str,
        next_run_at: datetime,
        now: datetime,
        session: AsyncSession | None = None,
    ) -> bool:
        async with optional_session(self._database, session, write=True) as active:
            result = await active.execute(
                update(AutomationModel)
                .where(
                    AutomationModel.id == automation_id,
                    self._owner_clause(creator_person_id),
                    AutomationModel.status.in_(
                        [
                            AutomationStatus.PAUSED.value,
                            AutomationStatus.FAILED.value,
                            AutomationStatus.BLOCKED.value,
                        ]
                    ),
                )
                .values(
                    status=AutomationStatus.ACTIVE.value,
                    next_run_at=_aware_utc(next_run_at),
                    updated_at=_aware_utc(now),
                )
            )
        return bool(cast(CursorResult[Any], result).rowcount)

    async def schedule_now(
        self,
        automation_id: int,
        *,
        creator_person_id: str,
        now: datetime,
        session: AsyncSession | None = None,
    ) -> bool:
        timestamp = _aware_utc(now)
        async with optional_session(self._database, session, write=True) as active:
            result = await active.execute(
                update(AutomationModel)
                .where(
                    AutomationModel.id == automation_id,
                    self._owner_clause(creator_person_id),
                    AutomationModel.status.not_in(
                        [AutomationStatus.CANCELLED.value, AutomationStatus.COMPLETED.value]
                    ),
                )
                .values(
                    status=AutomationStatus.ACTIVE.value,
                    next_run_at=timestamp,
                    updated_at=timestamp,
                )
            )
        return bool(cast(CursorResult[Any], result).rowcount)

    async def publish_task(
        self,
        automation_id: int,
        *,
        creator_person_id: str,
        definition: PreparedTaskDefinition,
        now: datetime,
        next_run_at: datetime | None = None,
        max_runs: int | None = None,
        max_runs_set: bool = False,
        session: AsyncSession | None = None,
    ) -> AutomationRecord | None:
        timestamp = _aware_utc(now)
        async with optional_session(self._database, session, write=True) as active:
            row = await active.scalar(
                select(AutomationModel).where(
                    AutomationModel.id == automation_id, self._owner_clause(creator_person_id)
                )
            )
            if row is None or row.status in {"cancelled", "completed"}:
                return None
            latest_version = int(
                await active.scalar(
                    select(func.max(AutomationVersionModel.version)).where(
                        AutomationVersionModel.automation_id == automation_id
                    )
                )
                or 0
            )
            if next_run_at is not None:
                row.next_run_at = next_run_at
            if max_runs_set:
                row.max_runs = max_runs
                if max_runs is not None and row.run_count >= max_runs:
                    running = await active.scalar(
                        select(AutomationRunModel.id)
                        .where(
                            AutomationRunModel.automation_id == automation_id,
                            AutomationRunModel.status == RunStatus.RUNNING.value,
                        )
                        .limit(1)
                    )
                    if running is None:
                        row.status = AutomationStatus.COMPLETED.value
                        row.next_run_at = None
            row.updated_at = timestamp
            version = AutomationVersionModel(
                automation_id=automation_id,
                version=latest_version + 1,
                script_json=definition.script_json,
                script_hash=definition.script_hash,
                authority_snapshot_json=definition.authority_json,
                canonical_presence_id=row.canonical_presence_id,
                updated_by="",
                created_at=timestamp,
            )
            active.add(version)
            await active.flush()
            return _automation_record(row, version, definition=definition)

    async def claim_due(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
        enabled: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> AsyncIterator[AutomationRecord]:
        timestamp = _aware_utc(now)

        def due_at(current: datetime) -> Any:
            return and_(
                true() if enabled else false(),
                AutomationModel.status == AutomationStatus.ACTIVE.value,
                AutomationModel.next_run_at.is_not(None),
                AutomationModel.next_run_at <= current,
                ~select(AutomationRunModel.id)
                .where(
                    AutomationRunModel.automation_id == AutomationModel.id,
                    AutomationRunModel.scheduled_for == AutomationModel.next_run_at,
                )
                .exists(),
            )

        after: tuple[datetime, int] | None = None
        while True:
            async with self._database.read_session() as session:
                page = (
                    await session.execute(
                        select(AutomationModel.id, AutomationModel.next_run_at)
                        .where(
                            due_at(timestamp),
                            or_(
                                AutomationModel.claimed_until.is_(None),
                                AutomationModel.claimed_until < timestamp,
                            ),
                            or_(
                                AutomationModel.next_run_at > after[0],
                                and_(
                                    AutomationModel.next_run_at == after[0],
                                    AutomationModel.id > after[1],
                                ),
                            )
                            if after is not None
                            else true(),
                        )
                        .order_by(AutomationModel.next_run_at.asc(), AutomationModel.id.asc())
                        .limit(128)
                    )
                ).all()
            if not page:
                return
            after = page[-1].next_run_at, page[-1].id
            candidate_ids = [candidate.id for candidate in page]
            async with self._database.immediate_session() as session:
                claimed_at = _aware_utc(clock() if clock is not None else datetime.now(UTC))
                rows = (
                    await session.scalars(
                        update(AutomationModel)
                        .where(
                            AutomationModel.id.in_(candidate_ids),
                            due_at(timestamp),
                            or_(
                                AutomationModel.claimed_until.is_(None),
                                AutomationModel.claimed_until < claimed_at,
                            ),
                        )
                        .values(
                            claimed_by=worker_id,
                            claimed_until=claimed_at + timedelta(seconds=lease_seconds),
                        )
                        .returning(AutomationModel)
                    )
                ).all()
                accepted_ids = [row.id for row in rows]
                latest = (
                    select(
                        AutomationVersionModel.automation_id,
                        func.max(AutomationVersionModel.version).label("version"),
                    )
                    .where(AutomationVersionModel.automation_id.in_(accepted_ids))
                    .group_by(AutomationVersionModel.automation_id)
                    .subquery()
                )
                versions: dict[int, AutomationVersionModel] = (
                    dict(
                        (
                            await session.execute(
                                select(
                                    AutomationVersionModel.automation_id, AutomationVersionModel
                                ).join(
                                    latest,
                                    and_(
                                        AutomationVersionModel.automation_id
                                        == latest.c.automation_id,
                                        AutomationVersionModel.version == latest.c.version,
                                    ),
                                )
                            )
                        )
                        .tuples()
                        .all()
                    )
                    if rows
                    else {}
                )
            # The returned ORM columns are the accepted writer snapshot. Parse
            # their original JSON only after commit has positively completed.
            by_id = {row.id: row for row in rows}
            for identity in candidate_ids:
                if identity in by_id:
                    yield _automation_record(by_id[identity], versions[identity])

    async def release_claim(
        self,
        automation_id: int,
        *,
        worker_id: str,
        not_before: datetime | None = None,
    ) -> None:
        values: dict[str, Any] = {"claimed_by": None, "claimed_until": not_before}
        async with self._database.immediate_session() as session:
            await session.execute(
                update(AutomationModel)
                .where(
                    AutomationModel.id == automation_id,
                    AutomationModel.claimed_by == worker_id,
                )
                .values(**values)
            )

    async def claim_expiry(self, automation_id: int, worker_id: str) -> float | None:
        async with self._database.read_session() as session:
            value = await session.scalar(
                select(AutomationModel.claimed_until).where(
                    AutomationModel.id == automation_id,
                    AutomationModel.claimed_by == worker_id,
                )
            )
        return _aware_utc(value).timestamp() if value is not None else None

    async def renew_claim(
        self,
        automation_id: int,
        worker_id: str,
        *,
        lease_seconds: int,
        clock: Callable[[], datetime],
    ) -> bool:
        async with self._database.immediate_session() as session:
            now = _aware_utc(clock())
            result = await session.execute(
                update(AutomationModel)
                .where(
                    AutomationModel.id == automation_id,
                    AutomationModel.claimed_by == worker_id,
                    AutomationModel.claimed_until > now,
                )
                .values(claimed_until=now + timedelta(seconds=lease_seconds))
            )
            return cast(CursorResult[Any], result).rowcount == 1

    async def create_run(
        self,
        automation_id: int,
        *,
        scheduled_for: datetime,
        actual_started_at: datetime,
        worker_id: str | None = None,
        version_id: int | None = None,
    ) -> AutomationRunRecord | None:

        scheduled = _aware_utc(scheduled_for)
        started = _aware_utc(actual_started_at)
        key = f"{automation_id}:{scheduled.isoformat()}"
        async with self._database.immediate_session() as session:
            owner = await session.get(AutomationModel, automation_id)
            if owner is None:
                raise ValueError("automation_not_found")
            current_version = await _current_version_id(session, automation_id)
            if worker_id is not None and (
                owner.claimed_by != worker_id
                or owner.claimed_until is None
                or _aware_utc(owner.claimed_until) <= datetime.now(UTC)
                or owner.status != AutomationStatus.ACTIVE.value
                or owner.next_run_at is None
                or _aware_utc(owner.next_run_at) != scheduled
                or current_version != version_id
            ):
                return None
            existing = await session.scalar(
                select(AutomationRunModel).where(
                    AutomationRunModel.automation_id == automation_id,
                    AutomationRunModel.scheduled_for == scheduled,
                )
            )
            if existing is not None:
                if (
                    existing.status != RunStatus.RUNNING.value
                    and owner.next_run_at is not None
                    and _aware_utc(owner.next_run_at) == scheduled
                ):
                    owner.next_run_at = None
                return None
            if owner.max_runs is not None:
                admitted = int(
                    await session.scalar(
                        select(func.count(AutomationRunModel.id)).where(
                            AutomationRunModel.automation_id == automation_id,
                            AutomationRunModel.status == RunStatus.RUNNING.value,
                        )
                    )
                    or 0
                )
                if owner.run_count + admitted >= owner.max_runs:
                    if admitted == 0:
                        owner.status = AutomationStatus.COMPLETED.value
                        owner.next_run_at = None
                    return None
            version = await session.get(AutomationVersionModel, version_id or current_version)
            if version is None:
                raise ValueError("automation_version_unavailable")
            run_id = await session.scalar(
                insert(AutomationRunModel)
                .values(
                    automation_id=automation_id,
                    version_id=version.id,
                    scheduled_for=scheduled,
                    actual_started_at=started,
                    finished_at=None,
                    status=RunStatus.RUNNING.value,
                    idempotency_key=key,
                    llm_calls=0,
                    tool_calls=0,
                    messages_sent=0,
                    error_category=None,
                    result_summary_json="{}",
                    created_at=started,
                )
                .on_conflict_do_nothing(index_elements=["automation_id", "scheduled_for"])
                .returning(AutomationRunModel.id)
            )
            if run_id is None:
                return None
            row = await session.get(AutomationRunModel, run_id)
            assert row is not None
            return _run_record(row)

    async def settle_interrupted_occurrences(
        self,
        *,
        run_id: int | None = None,
        occurrence: tuple[int, datetime] | None = None,
        error_category: str = "main_activation_interrupted",
    ) -> None:
        """Close unowned original occurrences without dispatching any effects."""
        from yuki.runtime.work_schema_v1 import work

        async with self._database.read_session() as reader:
            owned = (
                select(work.c.id)
                .where(
                    func.json_extract(work.c.source_json, "$.owner") == "automation",
                    func.json_extract(work.c.source_json, "$.automation_run_id")
                    == AutomationRunModel.id,
                    work.c.parent_work_id.is_(None),
                )
                .exists()
            )
            runs = (
                await reader.scalars(
                    select(AutomationRunModel).where(
                        AutomationRunModel.status == RunStatus.RUNNING.value,
                        ~owned,
                        AutomationRunModel.id == run_id if run_id is not None else true(),
                        and_(
                            AutomationRunModel.automation_id == occurrence[0],
                            AutomationRunModel.scheduled_for == _aware_utc(occurrence[1]),
                        )
                        if occurrence is not None
                        else true(),
                    )
                )
            ).all()
        for run in runs:
            await self.finish_run(
                run.id,
                status=RunStatus.UNCERTAIN,
                llm_calls=run.llm_calls,
                tool_calls=run.tool_calls,
                messages_sent=run.messages_sent,
                error_category=error_category,
                summary=json.loads(run.result_summary_json),
                finished_at=datetime.now(UTC),
            )

    async def settle_work_occurrences(self) -> None:
        """Settle the original execution's root; recovery stays in WorkScheduler."""
        from yuki.runtime.work_recovery_schema import invocations
        from yuki.runtime.work_schema_v1 import work

        async with self._database.read_session() as reader:
            rows = (
                (
                    await reader.execute(
                        select(
                            AutomationRunModel.id,
                            work.c.id.label("work_id"),
                            work.c.state,
                            work.c.model_requests,
                            work.c.tool_calls,
                            work.c.sent_messages,
                        )
                        .join(
                            AutomationVersionModel,
                            AutomationVersionModel.id == AutomationRunModel.version_id,
                        )
                        .outerjoin(invocations, invocations.c.run_id == AutomationRunModel.id)
                        .join(
                            work,
                            and_(
                                func.json_extract(work.c.source_json, "$.owner") == "automation",
                                func.json_extract(work.c.source_json, "$.automation_run_id")
                                == AutomationRunModel.id,
                                work.c.parent_work_id.is_(None),
                                or_(
                                    and_(
                                        invocations.c.script_hash
                                        == AutomationVersionModel.script_hash,
                                        work.c.id
                                        == func.json_extract(
                                            invocations.c.payload_json, "$.work_id"
                                        ),
                                    ),
                                    and_(
                                        invocations.c.run_id.is_(None),
                                        work.c.source_key
                                        == func.printf(
                                            "automation:%d:%s",
                                            AutomationRunModel.id,
                                            AutomationVersionModel.script_hash,
                                        ),
                                    ),
                                ),
                            ),
                        )
                        .where(
                            AutomationRunModel.status == RunStatus.RUNNING.value,
                            work.c.state.in_(("completed", "failed", "cancelled")),
                        )
                    )
                )
                .mappings()
                .all()
            )
        for row in rows:
            await self.finish_run(
                row["id"],
                status={
                    "completed": RunStatus.SUCCEEDED,
                    "failed": RunStatus.FAILED,
                    "cancelled": RunStatus.CANCELLED,
                }[row["state"]],
                llm_calls=row["model_requests"],
                tool_calls=row["tool_calls"],
                messages_sent=row["sent_messages"],
                error_category=None if row["state"] == "completed" else row["state"],
                summary={
                    "work_id": row["work_id"],
                    "work_state": row["state"],
                    "messages_sent_semantics": "work_delivery_reservations",
                },
                finished_at=datetime.now(UTC),
            )

    async def finish_run(
        self,
        run_id: int,
        *,
        status: RunStatus,
        llm_calls: int,
        tool_calls: int,
        messages_sent: int,
        error_category: str | None,
        summary: dict[str, Any],
        finished_at: datetime,
        worker_id: str | None = None,
    ) -> bool:
        from yuki.time.schedules import schedule_after_completion

        timestamp = _aware_utc(finished_at)
        summary_json = _redacted_json(summary)
        # Occurrences retain their immutable accepted version. Read only its
        # small schedule projection; task bodies and authority JSON stay out.
        async with self._database.read_session() as reader:
            accepted = (
                await reader.execute(
                    select(
                        AutomationRunModel.version_id,
                        AutomationRunModel.scheduled_for,
                        func.coalesce(
                            func.json_extract(AutomationVersionModel.script_json, "$.trigger"),
                            func.json_extract(AutomationVersionModel.script_json, "$.schedule"),
                        ).label("schedule"),
                        func.json_extract(AutomationVersionModel.script_json, "$.timezone").label(
                            "timezone"
                        ),
                        AutomationVersionModel.authority_snapshot_json.is_not(None).label(
                            "has_authority"
                        ),
                    )
                    .join(
                        AutomationVersionModel,
                        AutomationVersionModel.id == AutomationRunModel.version_id,
                    )
                    .where(AutomationRunModel.id == run_id)
                )
            ).one_or_none()
        next_scheduled = (
            schedule_after_completion(
                schedule_from_script(
                    {"trigger": json.loads(accepted.schedule), "timezone": accepted.timezone}
                ),
                _aware_utc(accepted.scheduled_for),
                timestamp,
                accepted.timezone,
            )
            if accepted is not None and accepted.has_authority
            else None
        )
        async with self._database.immediate_session() as active:
            run = await active.get(AutomationRunModel, run_id)
            if run is None:
                return False
            llm_calls = max(llm_calls, run.llm_calls)
            tool_calls = max(tool_calls, run.tool_calls)
            messages_sent = max(messages_sent, run.messages_sent)
            result = await active.execute(
                update(AutomationRunModel)
                .where(
                    AutomationRunModel.id == run_id,
                    AutomationRunModel.status == RunStatus.RUNNING.value,
                    AutomationRunModel.automation_id.in_(
                        select(AutomationModel.id).where(AutomationModel.claimed_by == worker_id)
                    )
                    if worker_id is not None
                    else true(),
                )
                .values(
                    status=status.value,
                    llm_calls=llm_calls,
                    tool_calls=tool_calls,
                    messages_sent=messages_sent,
                    error_category=error_category,
                    result_summary_json=summary_json,
                    finished_at=timestamp,
                )
                .returning(AutomationRunModel.id)
            )
            if result.scalar_one_or_none() is None:
                return False
            row = await active.get(AutomationModel, run.automation_id)
            assert row is not None
            row.last_run_at = timestamp
            row.run_count += 1
            row.updated_at = timestamp
            if row.status in {AutomationStatus.CANCELLED.value, AutomationStatus.COMPLETED.value}:
                return True
            if (
                status is not RunStatus.BLOCKED
                and row.max_runs is not None
                and row.run_count >= row.max_runs
            ):
                row.status = AutomationStatus.COMPLETED.value
                row.next_run_at = None
                return True
            if row.next_run_at is not None and _aware_utc(row.next_run_at) == _aware_utc(
                run.scheduled_for
            ):
                row.next_run_at = next_scheduled
                if row.status != AutomationStatus.PAUSED.value:
                    if status is RunStatus.BLOCKED:
                        row.status = AutomationStatus.BLOCKED.value
                    elif row.next_run_at is None:
                        row.status = (
                            AutomationStatus.COMPLETED.value
                            if status is RunStatus.SUCCEEDED
                            else AutomationStatus.FAILED.value
                        )
            return True

    async def run_history(
        self,
        automation_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[AutomationRunRecord, ...]:
        async with self._database.read_session() as session:
            rows = (
                await session.execute(
                    select(AutomationRunModel, AutomationVersionModel.script_json)
                    .outerjoin(
                        AutomationVersionModel,
                        AutomationVersionModel.id == AutomationRunModel.version_id,
                    )
                    .where(AutomationRunModel.automation_id == automation_id)
                    .order_by(AutomationRunModel.created_at.desc(), AutomationRunModel.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
        return tuple(
            _run_record(row, definition=json.loads(raw) if raw is not None else None)
            for row, raw in rows
        )

    async def latest_run_at(self) -> datetime | None:
        async with self._database.read_session() as session:
            return await session.scalar(select(func.max(AutomationRunModel.finished_at)))

    async def next_due_at(self) -> datetime | None:
        async with self._database.read_session() as session:
            return await session.scalar(
                select(func.min(AutomationModel.next_run_at)).where(
                    AutomationModel.status == AutomationStatus.ACTIVE.value
                )
            )


async def _current_version_id(session: AsyncSession, automation_id: int) -> int | None:
    return cast(
        int | None,
        await session.scalar(
            select(AutomationVersionModel.id)
            .where(AutomationVersionModel.automation_id == automation_id)
            .order_by(AutomationVersionModel.version.desc())
            .limit(1)
        ),
    )


def _latest_version_clause() -> Any:
    latest = (
        select(func.max(AutomationVersionModel.version))
        .where(AutomationVersionModel.automation_id == AutomationModel.id)
        .correlate(AutomationModel)
        .scalar_subquery()
    )
    return and_(
        AutomationVersionModel.automation_id == AutomationModel.id,
        AutomationVersionModel.version == latest,
    )


def _definition_statement() -> Any:
    return select(AutomationModel, AutomationVersionModel).join(
        AutomationVersionModel, _latest_version_clause()
    )


def task_timezone(script: dict[str, Any]) -> str:
    trigger = script.get("trigger", script.get("schedule", {}))
    return str(trigger.get("timezone") or script.get("timezone") or "Asia/Shanghai")


def schedule_from_script(script: dict[str, Any]) -> Schedule:
    raw = dict(script.get("trigger", script.get("schedule", {})))
    if raw.get("type") == "once":
        value = raw["local_datetime"]
        dt = datetime.fromisoformat(value) if isinstance(value, str) else value
        if dt.utcoffset() is None:
            dt = dt.replace(tzinfo=ZoneInfo(task_timezone(script)), fold=0)
        raw["local_datetime"] = dt
        raw.pop("timezone", None)
    return TypeAdapter(Schedule).validate_python(raw)


def _automation_record(
    row: AutomationModel,
    version: AutomationVersionModel,
    *,
    definition: PreparedTaskDefinition | None = None,
) -> AutomationRecord:
    script = definition.script if definition else json.loads(version.script_json)
    scene = (
        definition.authority_snapshot
        if definition
        else (
            json.loads(version.authority_snapshot_json) if version.authority_snapshot_json else {}
        )
    )
    return AutomationRecord(
        id=row.id,
        version_id=version.id,
        claimed_by=row.claimed_by,
        creator_kind=row.creator_kind,
        creator_user_id=row.creator_user_id,
        bot_user_id=row.bot_user_id,
        name=script.get("name") or f"自动化 #{row.id}",
        status=AutomationStatus(row.status),
        timezone=task_timezone(script),
        script=script,
        schedule=definition.task.trigger if definition else schedule_from_script(script),
        script_hash=version.script_hash,
        authority_snapshot=scene,
        created_from_message_id=row.created_from_message_id,
        next_run_at=_aware_utc(row.next_run_at) if row.next_run_at else None,
        last_run_at=_aware_utc(row.last_run_at) if row.last_run_at else None,
        run_count=row.run_count,
        max_runs=row.max_runs,
        creation_max_runs=row.creation_max_runs,
        created_at=_aware_utc(row.created_at),
        updated_at=_aware_utc(row.updated_at),
        canonical_creator_person_id=row.canonical_creator_person_id,
        canonical_presence_id=version.canonical_presence_id,
        canonical_conversation_id=scene.get("canonical_conversation_id"),
        source_event_id=row.source_event_id,
    )


def _directory_statement() -> Any:
    return (
        select(
            AutomationModel,
            func.json_extract(AutomationVersionModel.script_json, "$.name").label("name"),
            CanonicalPersonModel.display_name,
            person_root_id(AutomationModel.canonical_creator_person_id),
        )
        .join(AutomationVersionModel, _latest_version_clause())
        .outerjoin(
            CanonicalPersonModel,
            CanonicalPersonModel.id == person_root_id(AutomationModel.canonical_creator_person_id),
        )
    )


def _automation_directory_entry(row: Any) -> AutomationDirectoryEntry:
    owner = row[0]
    record = AutomationDirectoryRecord(
        id=owner.id,
        creator_kind=owner.creator_kind,
        creator_user_id=owner.creator_user_id,
        canonical_creator_person_id=owner.canonical_creator_person_id,
        name=row.name or f"自动化 #{owner.id}",
        status=AutomationStatus(owner.status),
        timezone="Asia/Shanghai",
        next_run_at=_aware_utc(owner.next_run_at) if owner.next_run_at else None,
    )
    return AutomationDirectoryEntry(
        record=record,
        creator=AutomationCreatorIdentity(
            person_id=row[-1],
            external_account_id=owner.creator_user_id,
            display_name="SELF" if owner.creator_kind == "self" else row[-2] or None,
            kind=owner.creator_kind,
        ),
    )


def _run_record(
    row: AutomationRunModel, *, definition: dict[str, Any] | None = None
) -> AutomationRunRecord:
    return AutomationRunRecord(
        id=row.id,
        version_id=row.version_id,
        automation_id=row.automation_id,
        scheduled_for=_aware_utc(row.scheduled_for),
        actual_started_at=_aware_utc(row.actual_started_at),
        finished_at=_aware_utc(row.finished_at) if row.finished_at else None,
        status=RunStatus(row.status),
        steps_completed=row.steps_completed,
        llm_calls=row.llm_calls,
        tool_calls=row.tool_calls,
        messages_sent=row.messages_sent,
        error_category=row.error_category,
        result_summary=json.loads(row.result_summary_json or "{}"),
        definition=definition,
    )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _redacted_json(value: dict[str, Any]) -> str:
    sensitive = {"api_key", "token", "password", "secret", "authorization"}

    def redact(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                str(key): "[redacted]" if str(key).casefold() in sensitive else redact(child)
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [redact(child) for child in item]
        return item

    return json.dumps(redact(value), ensure_ascii=False, separators=(",", ":"))
