"""Transactional persistence and lease coordination for automations."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from pydantic import TypeAdapter
from sqlalchemy import and_, false, func, or_, select, true, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.automation.authority import DelegatedAuthority
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
    authority: DelegatedAuthority
    script_json: str
    schedule_json: str
    authority_json: str
    script_hash: str
    script: dict[str, Any]
    authority_snapshot: dict[str, Any]

    @classmethod
    def prepare(cls, task: TaskSpec, authority: DelegatedAuthority) -> PreparedTaskDefinition:
        return cls(
            task,
            authority,
            task.model_dump_json(exclude_none=True),
            task.trigger.model_dump_json(exclude_none=True),
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
        session: AsyncSession | None = None,
    ) -> AutomationRecord:
        task, authority = definition.task, definition.authority
        script_json = definition.script_json
        schedule_json = definition.schedule_json
        authority_json = definition.authority_json
        timestamp = _aware_utc(now)
        transaction = (
            self._database.immediate_session()
            if session is None
            else optional_session(self._database, session, write=True)
        )
        async with transaction as active:
            if creation_source_key is not None:
                previous = await active.scalar(
                    select(AutomationModel)
                    .where(
                        self._owner_clause(creator_person_id),
                        AutomationModel.creation_source_key == creation_source_key,
                    )
                    .order_by(AutomationModel.id)
                    .limit(1)
                )
                if previous is not None:
                    initial_hash = await self.initial_script_hash(previous.id, session=active)
                    if initial_hash != definition.script_hash or previous.max_runs != max_runs:
                        raise ValueError("automation_creation_key_conflict")
                    return _automation_record(previous)
            if creator_person_id == "self":
                if authority.principal_kind != "self":
                    raise ValueError("invalid_self_automation_authority")
            else:
                creator = await active.get(CanonicalPersonModel, creator_person_id)
                if (
                    creator is None
                    or not creator.enabled
                    or authority.canonical_creator_person_id is None
                    or not await same_person_id(
                        active, authority.canonical_creator_person_id, creator_person_id
                    )
                    or authority.principal_kind != "person"
                ):
                    raise ValueError("创建者没有对应的永久主体")
            target_person, target_space = _canonical_send_targets(task, authority)
            presence = await active.get(PresenceModel, authority.canonical_presence_id)
            if presence is None:
                raise ValueError("automation_presence_unavailable")
            row = AutomationModel(
                creator_kind=authority.principal_kind,
                creator_user_id=authority.creator_user_id,
                bot_user_id=authority.bot_user_id,
                name=task.name,
                status=AutomationStatus.ACTIVE.value,
                timezone=task.timezone,
                schedule_json=schedule_json,
                script_json=script_json,
                script_hash=definition.script_hash,
                required_capabilities_json="[]",
                authority_snapshot_json=authority_json,
                created_from_message_id=authority.created_from_message_id,
                creation_source_key=creation_source_key,
                next_run_at=next_run_at,
                last_run_at=None,
                run_count=0,
                max_runs=max_runs,
                consecutive_failures=0,
                claimed_by=None,
                claimed_until=None,
                created_at=timestamp,
                updated_at=timestamp,
                canonical_creator_person_id=None
                if creator_person_id == "self"
                else creator_person_id,
                canonical_target_person_id=target_person,
                canonical_target_space_id=target_space,
                canonical_presence_id=presence.id,
            )
            active.add(row)
            await active.flush()
            version = AutomationVersionModel(
                automation_id=row.id,
                version=1,
                script_json=script_json,
                script_hash=definition.script_hash,
                authority_snapshot_json=authority_json,
                canonical_target_person_id=target_person,
                canonical_target_space_id=target_space,
                canonical_presence_id=presence.id,
                updated_by=authority.creator_user_id,
                created_at=timestamp,
            )
            active.add(version)
            await active.flush()
            return _automation_record(row, version.id, definition=definition)

    async def get(
        self, automation_id: int, *, session: AsyncSession | None = None
    ) -> AutomationRecord | None:
        async with optional_session(self._database, session, write=False) as active:
            row = await active.get(AutomationModel, automation_id)
            version_id = await _current_version_id(active, automation_id)
        return _automation_record(row, version_id) if row is not None else None

    async def get_for_run(
        self, run_id: int | None, *, session: AsyncSession | None = None
    ) -> AutomationRecord | None:
        """Read the accepted definition through its original immutable version."""

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
        if pair is None:
            return None
        owner, version = pair
        if version.authority_snapshot_json is None:
            return None
        script = json.loads(version.script_json)
        return _automation_record(owner, version.id).model_copy(
            update={
                "script": script,
                "name": script["name"],
                "timezone": script["timezone"],
                "schedule": TypeAdapter(Schedule).validate_python(
                    script.get("trigger", script.get("schedule"))
                ),
                "script_hash": version.script_hash,
                "authority_snapshot": json.loads(version.authority_snapshot_json),
                "canonical_target_person_id": version.canonical_target_person_id,
                "canonical_target_space_id": version.canonical_target_space_id,
                "canonical_presence_id": version.canonical_presence_id,
            }
        )

    async def get_by_creation_key(
        self,
        creator_person_id: str,
        creation_source_key: str,
        *,
        session: AsyncSession | None = None,
    ) -> AutomationRecord | None:
        """Resolve a delegated create retry without producing another task."""

        query = select(AutomationModel).where(
            self._owner_clause(creator_person_id),
            AutomationModel.creation_source_key == creation_source_key,
        )
        async with optional_session(self._database, session, write=False) as active:
            row = await active.scalar(query.order_by(AutomationModel.id.asc()).limit(1))
        return _automation_record(row) if row is not None else None

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

    async def list_for_creator(
        self,
        creator_person_id: str,
        *,
        include_terminal: bool = True,
        limit: int = 100,
    ) -> tuple[AutomationRecord, ...]:
        query = select(AutomationModel).where(self._owner_clause(creator_person_id))
        if not include_terminal:
            query = query.where(
                AutomationModel.status.in_(
                    [AutomationStatus.ACTIVE.value, AutomationStatus.PAUSED.value]
                )
            )
        async with self._database.read_session() as session:
            rows = (
                await session.scalars(
                    query.order_by(AutomationModel.updated_at.desc()).limit(max(1, min(limit, 200)))
                )
            ).all()
        return tuple(_automation_record(row) for row in rows)

    async def list_directory(
        self,
        *,
        statuses: tuple[AutomationStatus, ...],
        limit: int = 50,
        offset: int = 0,
        include_script: bool = False,
    ) -> tuple[AutomationDirectoryEntry, ...]:
        """List a bounded, owner-independent task directory for the main Agent."""

        query = _directory_statement(include_script=include_script).where(
            AutomationModel.status.in_([status.value for status in statuses])
        )
        current = set(statuses) <= {AutomationStatus.ACTIVE, AutomationStatus.PAUSED}
        order = (
            (
                AutomationModel.next_run_at.is_(None),
                AutomationModel.next_run_at.asc(),
                AutomationModel.id.asc(),
            )
            if current
            else (AutomationModel.updated_at.desc(), AutomationModel.id.desc())
        )
        async with self._database.read_session() as session:
            rows = (
                await session.execute(
                    query.order_by(*order).offset(max(0, offset)).limit(max(1, min(limit, 101)))
                )
            ).all()
        return tuple(
            _automation_directory_entry(row, include_script=include_script) for row in rows
        )

    async def get_directory_entry(self, automation_id: int) -> AutomationDirectoryEntry | None:
        """Read one task and its creator projection in a single database query."""

        query = _directory_statement().where(AutomationModel.id == automation_id)
        async with self._database.read_session() as session:
            row = (await session.execute(query)).one_or_none()
        if row is None:
            return None
        return _automation_directory_entry(row)

    async def find_equivalent_directory_entries(
        self, task: TaskSpec, *, max_runs: int | None
    ) -> tuple[AutomationDirectoryEntry, ...]:
        """Stream each current plan once and return matching directory entries."""

        expected = task.model_dump(mode="json", exclude={"name"}, exclude_none=True)
        query = (
            _directory_statement(include_script=True)
            .where(
                AutomationModel.status.in_(
                    (AutomationStatus.ACTIVE.value, AutomationStatus.PAUSED.value)
                ),
                AutomationModel.max_runs == max_runs,
            )
            .order_by(
                AutomationModel.next_run_at.is_(None),
                AutomationModel.next_run_at,
                AutomationModel.id,
            )
        )
        async with self._database.read_session() as session:
            matches = []
            async for row in await session.stream(query.execution_options(yield_per=128)):
                actual = json.loads(row[0].script_json)
                actual.pop("name", None)
                if actual == expected:
                    matches.append(_automation_directory_entry(row, include_script=True))
            return tuple(matches)

    async def list_current_for_creator(
        self,
        creator_person_id: str,
        *,
        limit: int = 100,
    ) -> tuple[AutomationRecord, ...]:
        """List schedulable tasks only, ordered for stable presentation."""

        query = (
            select(AutomationModel)
            .where(
                self._owner_clause(creator_person_id),
                AutomationModel.status.in_(
                    [AutomationStatus.ACTIVE.value, AutomationStatus.PAUSED.value]
                ),
            )
            .order_by(
                AutomationModel.next_run_at.is_(None),
                AutomationModel.next_run_at.asc(),
                AutomationModel.id.asc(),
            )
            .limit(max(1, min(limit, 200)))
        )
        async with self._database.read_session() as session:
            rows = (await session.scalars(query)).all()
        return tuple(_automation_record(row) for row in rows)

    async def list_terminal_for_creator(
        self,
        creator_person_id: str,
        *,
        limit: int = 100,
    ) -> tuple[AutomationRecord, ...]:
        """List completed, cancelled, failed, or blocked tasks as history."""

        terminal = [
            AutomationStatus.COMPLETED.value,
            AutomationStatus.CANCELLED.value,
            AutomationStatus.FAILED.value,
            AutomationStatus.BLOCKED.value,
        ]
        query = (
            select(AutomationModel)
            .where(
                self._owner_clause(creator_person_id),
                AutomationModel.status.in_(terminal),
            )
            .order_by(AutomationModel.updated_at.desc(), AutomationModel.id.desc())
            .limit(max(1, min(limit, 200)))
        )
        async with self._database.read_session() as session:
            rows = (await session.scalars(query)).all()
        return tuple(_automation_record(row) for row in rows)

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
                    consecutive_failures=0,
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
        session: AsyncSession | None = None,
    ) -> AutomationRecord | None:
        task, authority = definition.task, definition.authority
        timestamp = _aware_utc(now)
        async with optional_session(self._database, session, write=True) as active:
            if creator_person_id == "self":
                if authority.principal_kind != "self":
                    raise ValueError("invalid_self_automation_authority")
            else:
                authority_creator = await active.get(CanonicalPersonModel, creator_person_id)
                if (
                    authority_creator is None
                    or authority.canonical_creator_person_id is None
                    or not await same_person_id(
                        active, authority.canonical_creator_person_id, creator_person_id
                    )
                    or authority.principal_kind != "person"
                ):
                    raise ValueError("更新授权与自动化永久创建者不一致")
            row = await active.scalar(
                select(AutomationModel).where(
                    AutomationModel.id == automation_id,
                    self._owner_clause(creator_person_id),
                )
            )
            if row is None or row.status in {
                AutomationStatus.CANCELLED.value,
                AutomationStatus.COMPLETED.value,
            }:
                return None
            latest_version = int(
                await active.scalar(
                    select(func.max(AutomationVersionModel.version)).where(
                        AutomationVersionModel.automation_id == automation_id
                    )
                )
                or 0
            )
            script_json = definition.script_json
            row.name = task.name
            row.timezone = cast(str, task.timezone)
            row.schedule_json = definition.schedule_json
            row.script_json = script_json
            row.script_hash = definition.script_hash
            row.required_capabilities_json = "[]"
            row.authority_snapshot_json = definition.authority_json
            if next_run_at is not None:
                row.next_run_at = next_run_at
            row.consecutive_failures = 0
            row.updated_at = timestamp
            target_person, target_space = _canonical_send_targets(task, authority)
            row.canonical_target_person_id = target_person
            row.canonical_target_space_id = target_space
            version = AutomationVersionModel(
                automation_id=automation_id,
                version=latest_version + 1,
                script_json=script_json,
                script_hash=definition.script_hash,
                authority_snapshot_json=definition.authority_json,
                canonical_target_person_id=target_person,
                canonical_target_space_id=target_space,
                canonical_presence_id=row.canonical_presence_id,
                updated_by=authority.creator_user_id,
                created_at=timestamp,
            )
            active.add(version)
            await active.flush()
            return _automation_record(row, version.id, definition=definition)

    async def claim_due(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
        enabled: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> tuple[AutomationRecord, ...]:
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

        async with self._database.read_session() as session:
            candidate_ids = (
                await session.scalars(
                    select(AutomationModel.id)
                    .where(
                        due_at(timestamp),
                        or_(
                            AutomationModel.claimed_until.is_(None),
                            AutomationModel.claimed_until < timestamp,
                        ),
                    )
                    .order_by(AutomationModel.next_run_at.asc(), AutomationModel.id.asc())
                )
            ).all()
        claimed: list[AutomationRecord] = []
        for offset in range(0, len(candidate_ids), 128):
            page = candidate_ids[offset : offset + 128]
            async with self._database.immediate_session() as session:
                claimed_at = _aware_utc(clock() if clock is not None else datetime.now(UTC))
                rows = (
                    await session.scalars(
                        update(AutomationModel)
                        .where(
                            AutomationModel.id.in_(page),
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
                versions: dict[int, int] = (
                    dict(
                        (
                            await session.execute(
                                select(
                                    AutomationVersionModel.automation_id, AutomationVersionModel.id
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
            claimed.extend(
                _automation_record(by_id[identity], versions.get(identity))
                for identity in page
                if identity in by_id
            )
        return tuple(claimed)

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
                    return None
            version = await session.get(AutomationVersionModel, version_id or current_version)
            if version is None:
                raise ValueError("automation_version_unavailable")
            if version.authority_snapshot_json is None:
                # This is a new admission from the retained current definition,
                # never a reconstruction of an already accepted historical run.
                version = AutomationVersionModel(
                    automation_id=owner.id,
                    version=version.version + 1,
                    script_json=owner.script_json,
                    script_hash=owner.script_hash,
                    authority_snapshot_json=owner.authority_snapshot_json,
                    canonical_target_person_id=owner.canonical_target_person_id,
                    canonical_target_space_id=owner.canonical_target_space_id,
                    canonical_presence_id=owner.canonical_presence_id,
                    updated_by=owner.creator_user_id,
                    created_at=started,
                )
                session.add(version)
                await session.flush()
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
                    steps_completed=0,
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
                    )
                )
            ).all()
        for run in runs:
            await self.finish_run(
                run.id,
                status=RunStatus.UNCERTAIN,
                steps_completed=run.steps_completed,
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
                steps_completed=0,
                llm_calls=row["model_requests"],
                tool_calls=row["tool_calls"],
                messages_sent=row["sent_messages"],
                error_category=None if row["state"] == "completed" else row["state"],
                summary={"work_id": row["work_id"], "work_state": row["state"]},
                finished_at=datetime.now(UTC),
            )

    async def finish_run(
        self,
        run_id: int,
        *,
        status: RunStatus,
        steps_completed: int,
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
                TypeAdapter(Schedule).validate_json(accepted.schedule),
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
            steps_completed = max(steps_completed, run.steps_completed)
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
                    steps_completed=steps_completed,
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
            row.consecutive_failures = (
                0 if status is RunStatus.SUCCEEDED else row.consecutive_failures + 1
            )
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
    ) -> tuple[AutomationRunRecord, ...]:
        async with self._database.read_session() as session:
            rows = (
                await session.scalars(
                    select(AutomationRunModel)
                    .where(AutomationRunModel.automation_id == automation_id)
                    .order_by(AutomationRunModel.created_at.desc())
                    .limit(max(1, min(limit, 100)))
                )
            ).all()
        return tuple(_run_record(row) for row in rows)

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


def _canonical_send_targets(
    task: TaskSpec,
    authority: DelegatedAuthority,
) -> tuple[str | None, str | None]:
    if task.delivery.target != "self_private" and authority.canonical_space_id:
        return None, authority.canonical_space_id
    target_person = authority.canonical_creator_person_id
    if target_person is None:
        raise ValueError("发送目标没有对应的永久主体")
    return target_person, None


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


def _automation_record(
    row: AutomationModel,
    version_id: int | None = None,
    *,
    definition: PreparedTaskDefinition | None = None,
) -> AutomationRecord:
    return AutomationRecord(
        id=row.id,
        version_id=version_id,
        claimed_by=row.claimed_by,
        creator_kind=row.creator_kind,
        creator_user_id=row.creator_user_id,
        bot_user_id=row.bot_user_id,
        name=row.name,
        status=AutomationStatus(row.status),
        timezone=row.timezone,
        script=definition.script if definition is not None else json.loads(row.script_json),
        schedule=definition.task.trigger
        if definition is not None
        else TypeAdapter(Schedule).validate_json(row.schedule_json),
        script_hash=row.script_hash,
        authority_snapshot=definition.authority_snapshot
        if definition is not None
        else json.loads(row.authority_snapshot_json),
        created_from_message_id=row.created_from_message_id,
        next_run_at=_aware_utc(row.next_run_at) if row.next_run_at else None,
        last_run_at=_aware_utc(row.last_run_at) if row.last_run_at else None,
        run_count=row.run_count,
        max_runs=row.max_runs,
        consecutive_failures=row.consecutive_failures,
        created_at=_aware_utc(row.created_at),
        updated_at=_aware_utc(row.updated_at),
        canonical_creator_person_id=row.canonical_creator_person_id,
        canonical_target_person_id=row.canonical_target_person_id,
        canonical_target_space_id=row.canonical_target_space_id,
        canonical_presence_id=row.canonical_presence_id,
    )


def _directory_statement(*, include_script: bool = False) -> Any:
    columns = (
        [AutomationModel]
        if include_script
        else [getattr(AutomationModel, field) for field in AutomationDirectoryRecord.model_fields]
    )
    return select(
        *columns,
        CanonicalPersonModel.display_name,
        person_root_id(AutomationModel.canonical_creator_person_id),
    ).outerjoin(
        CanonicalPersonModel,
        CanonicalPersonModel.id == person_root_id(AutomationModel.canonical_creator_person_id),
    )


def _automation_directory_entry(
    row: Any, *, include_script: bool = False
) -> AutomationDirectoryEntry:
    record = (
        _automation_record(row[0])
        if include_script
        else AutomationDirectoryRecord(
            **{
                field: getattr(row, field)
                for field in AutomationDirectoryRecord.model_fields
                if field != "next_run_at"
            },
            next_run_at=_aware_utc(row.next_run_at) if row.next_run_at else None,
        )
    )
    display_name = row[-2]
    return AutomationDirectoryEntry(
        record=record,
        creator=AutomationCreatorIdentity(
            person_id=row[-1],
            external_account_id=record.creator_user_id,
            display_name="SELF" if record.creator_kind == "self" else display_name or None,
            kind=record.creator_kind,
        ),
    )


def _run_record(row: AutomationRunModel) -> AutomationRunRecord:
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
