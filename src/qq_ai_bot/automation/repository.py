"""Transactional persistence and lease coordination for automations."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.automation.authority import DelegatedAuthority
from qq_ai_bot.automation.models import (
    AutomationRecord,
    AutomationRunRecord,
    AutomationScript,
    AutomationStatus,
    RunStatus,
)
from qq_ai_bot.automation.validator import ValidatedAutomation, collect_send_targets
from qq_ai_bot.identity.canonical_repository import (
    IDENTITY_PLATFORM,
    active_person_id_for,
    active_space_id_for,
    presence_id_for,
)
from qq_ai_bot.identity.db_models import CanonicalPersonModel, IdentityBindingModel
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    AutomationModel,
    AutomationRunModel,
    AutomationStepRunModel,
    AutomationVersionModel,
)
from qq_ai_bot.persistence.unit_of_work import optional_session


class AutomationRepository:
    """Own automation rows, immutable versions, runs, step audits, and leases."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(
        self,
        validated: ValidatedAutomation,
        authority: DelegatedAuthority,
        *,
        creator_person_id: str,
        max_runs: int | None,
        misfire_grace_seconds: int,
        now: datetime,
        creation_source_key: str | None = None,
        session: AsyncSession | None = None,
    ) -> AutomationRecord:
        script_json = validated.script.model_dump_json(exclude_none=True)
        schedule_json = validated.script.schedule.model_dump_json(exclude_none=True)
        authority_json = authority.model_dump_json()
        timestamp = _aware_utc(now)
        async with optional_session(self._database, session, write=True) as active:
            creator = await active_person_id_for(active, authority.creator_user_id)
            if creator != creator_person_id:
                raise ValueError("创建者没有对应的永久主体")
            target_person, target_space = await _bind_canonical_send_targets(
                active,
                validated,
                authority,
                now=timestamp,
            )
            presence = await presence_id_for(active, authority.bot_user_id)
            row = AutomationModel(
                creator_user_id=authority.creator_user_id,
                bot_user_id=authority.bot_user_id,
                name=validated.script.name,
                status=AutomationStatus.ACTIVE.value,
                timezone=validated.script.timezone,
                schedule_json=schedule_json,
                script_json=script_json,
                script_hash=validated.script_hash,
                required_capabilities_json=json.dumps(
                    validated.required_capabilities, separators=(",", ":")
                ),
                authority_snapshot_json=authority_json,
                created_from_message_id=authority.created_from_message_id,
                creation_source_key=creation_source_key,
                next_run_at=validated.next_run_at,
                last_run_at=None,
                run_count=0,
                max_runs=max_runs,
                consecutive_failures=0,
                misfire_grace_seconds=misfire_grace_seconds,
                claimed_by=None,
                claimed_until=None,
                created_at=timestamp,
                updated_at=timestamp,
                canonical_creator_person_id=creator_person_id,
                canonical_target_person_id=target_person,
                canonical_target_space_id=target_space,
                canonical_presence_id=presence,
            )
            active.add(row)
            await active.flush()
            active.add(
                AutomationVersionModel(
                    automation_id=row.id,
                    version=1,
                    script_json=script_json,
                    script_hash=validated.script_hash,
                    updated_by=authority.creator_user_id,
                    created_at=timestamp,
                )
            )
            await active.flush()
            return _automation_record(row)

    async def get(
        self, automation_id: int, *, session: AsyncSession | None = None
    ) -> AutomationRecord | None:
        async with optional_session(self._database, session, write=False) as active:
            row = await active.get(AutomationModel, automation_id)
        return _automation_record(row) if row is not None else None

    async def resolve_active_creator_person(
        self,
        external_account_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> str | None:
        """Resolve a live QQ binding to its enabled permanent Person."""

        async with optional_session(self._database, session, write=False) as active:
            return await active_person_id_for(active, external_account_id)

    async def active_creator_accounts(
        self,
        creator_person_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> tuple[str, ...]:
        """Return deterministic active QQ provenance for an enabled Person."""

        async with optional_session(self._database, session, write=False) as active:
            person = await active.get(CanonicalPersonModel, creator_person_id)
            if person is None or not person.enabled:
                return ()
            accounts = (
                await active.scalars(
                    select(IdentityBindingModel.external_account_id)
                    .where(
                        IdentityBindingModel.person_id == creator_person_id,
                        IdentityBindingModel.platform == IDENTITY_PLATFORM,
                        IdentityBindingModel.status == "active",
                    )
                    .order_by(
                        IdentityBindingModel.created_at.asc(),
                        IdentityBindingModel.id.asc(),
                    )
                )
            ).all()
            return tuple(str(account) for account in accounts)

    async def preferred_active_creator_account(
        self,
        creator_person_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> str | None:
        accounts = await self.active_creator_accounts(creator_person_id, session=session)
        return accounts[0] if accounts else None

    async def get_by_creation_key(
        self,
        creator_person_id: str,
        creation_source_key: str,
    ) -> AutomationRecord | None:
        """Resolve a delegated create retry without producing another task."""

        query = select(AutomationModel).where(
            AutomationModel.canonical_creator_person_id == creator_person_id,
            AutomationModel.creation_source_key == creation_source_key,
        )
        async with self._database.sessions() as session:
            row = await session.scalar(query.order_by(AutomationModel.id.asc()).limit(1))
        return _automation_record(row) if row is not None else None

    async def list_for_creator(
        self,
        creator_person_id: str,
        *,
        include_terminal: bool = True,
        limit: int = 100,
    ) -> tuple[AutomationRecord, ...]:
        query = select(AutomationModel).where(
            AutomationModel.canonical_creator_person_id == creator_person_id
        )
        if not include_terminal:
            query = query.where(
                AutomationModel.status.in_(
                    [AutomationStatus.ACTIVE.value, AutomationStatus.PAUSED.value]
                )
            )
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    query.order_by(AutomationModel.updated_at.desc()).limit(max(1, min(limit, 200)))
                )
            ).all()
        return tuple(_automation_record(row) for row in rows)

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
                AutomationModel.canonical_creator_person_id == creator_person_id,
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
        async with self._database.sessions() as session:
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
                AutomationModel.canonical_creator_person_id == creator_person_id,
                AutomationModel.status.in_(terminal),
            )
            .order_by(AutomationModel.updated_at.desc(), AutomationModel.id.desc())
            .limit(max(1, min(limit, 200)))
        )
        async with self._database.sessions() as session:
            rows = (await session.scalars(query)).all()
        return tuple(_automation_record(row) for row in rows)

    async def active_count(self, creator_person_id: str | None = None) -> int:
        query = select(func.count(AutomationModel.id)).where(
            AutomationModel.status == AutomationStatus.ACTIVE.value
        )
        if creator_person_id is not None:
            query = query.where(AutomationModel.canonical_creator_person_id == creator_person_id)
        async with self._database.sessions() as session:
            return int(await session.scalar(query) or 0)

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
            "claimed_by": None,
            "claimed_until": None,
        }
        if status in {AutomationStatus.CANCELLED, AutomationStatus.COMPLETED}:
            values["next_run_at"] = None
        async with optional_session(self._database, session, write=True) as active:
            result = await active.execute(
                update(AutomationModel)
                .where(
                    AutomationModel.id == automation_id,
                    AutomationModel.canonical_creator_person_id == creator_person_id,
                )
                .values(**values)
            )
        return bool(cast(CursorResult[Any], result).rowcount)

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
                    AutomationModel.canonical_creator_person_id == creator_person_id,
                    AutomationModel.status.in_(
                        [AutomationStatus.PAUSED.value, AutomationStatus.FAILED.value]
                    ),
                )
                .values(
                    status=AutomationStatus.ACTIVE.value,
                    next_run_at=_aware_utc(next_run_at),
                    consecutive_failures=0,
                    claimed_by=None,
                    claimed_until=None,
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
                    AutomationModel.canonical_creator_person_id == creator_person_id,
                    AutomationModel.status.not_in(
                        [AutomationStatus.CANCELLED.value, AutomationStatus.COMPLETED.value]
                    ),
                )
                .values(
                    status=AutomationStatus.ACTIVE.value,
                    next_run_at=timestamp,
                    claimed_by=None,
                    claimed_until=None,
                    updated_at=timestamp,
                )
            )
        return bool(cast(CursorResult[Any], result).rowcount)

    async def update_script(
        self,
        automation_id: int,
        *,
        creator_person_id: str,
        validated: ValidatedAutomation,
        authority: DelegatedAuthority,
        now: datetime,
        session: AsyncSession | None = None,
    ) -> AutomationRecord | None:
        timestamp = _aware_utc(now)
        async with optional_session(self._database, session, write=True) as active:
            authority_creator = await active_person_id_for(active, authority.creator_user_id)
            if authority_creator != creator_person_id:
                raise ValueError("更新授权与自动化永久创建者不一致")
            row = await active.scalar(
                select(AutomationModel).where(
                    AutomationModel.id == automation_id,
                    AutomationModel.canonical_creator_person_id == creator_person_id,
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
            script_json = validated.script.model_dump_json(exclude_none=True)
            row.name = validated.script.name
            row.timezone = validated.script.timezone
            row.schedule_json = validated.script.schedule.model_dump_json(exclude_none=True)
            row.script_json = script_json
            row.script_hash = validated.script_hash
            row.required_capabilities_json = json.dumps(validated.required_capabilities)
            row.authority_snapshot_json = authority.model_dump_json()
            row.next_run_at = validated.next_run_at
            row.status = AutomationStatus.ACTIVE.value
            row.consecutive_failures = 0
            row.claimed_by = None
            row.claimed_until = None
            row.updated_at = timestamp
            target_person, target_space = await _bind_canonical_send_targets(
                active,
                validated,
                authority,
                now=timestamp,
            )
            row.canonical_target_person_id = target_person
            row.canonical_target_space_id = target_space
            active.add(
                AutomationVersionModel(
                    automation_id=automation_id,
                    version=latest_version + 1,
                    script_json=script_json,
                    script_hash=validated.script_hash,
                    updated_by=authority.creator_user_id,
                    created_at=timestamp,
                )
            )
            await active.flush()
            return _automation_record(row)

    async def claim_due(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
        limit: int = 10,
    ) -> tuple[AutomationRecord, ...]:
        timestamp = _aware_utc(now)
        lease_until = timestamp + timedelta(seconds=lease_seconds)
        async with self._database.sessions() as session:
            candidate_ids = (
                await session.scalars(
                    select(AutomationModel.id)
                    .where(
                        AutomationModel.status == AutomationStatus.ACTIVE.value,
                        AutomationModel.next_run_at.is_not(None),
                        AutomationModel.next_run_at <= timestamp,
                        or_(
                            AutomationModel.claimed_until.is_(None),
                            AutomationModel.claimed_until < timestamp,
                        ),
                    )
                    .order_by(AutomationModel.next_run_at.asc(), AutomationModel.id.asc())
                    .limit(max(1, min(limit, 50)))
                )
            ).all()
        claimed: list[AutomationRecord] = []
        for automation_id in candidate_ids:
            async with self._database.sessions() as session, session.begin():
                result = await session.execute(
                    update(AutomationModel)
                    .where(
                        AutomationModel.id == automation_id,
                        AutomationModel.status == AutomationStatus.ACTIVE.value,
                        AutomationModel.next_run_at.is_not(None),
                        AutomationModel.next_run_at <= timestamp,
                        or_(
                            AutomationModel.claimed_until.is_(None),
                            AutomationModel.claimed_until < timestamp,
                        ),
                    )
                    .values(claimed_by=worker_id, claimed_until=lease_until)
                )
                if not cast(CursorResult[Any], result).rowcount:
                    continue
                row = await session.get(AutomationModel, automation_id)
                if row is not None:
                    claimed.append(_automation_record(row))
        return tuple(claimed)

    async def release_claim(
        self,
        automation_id: int,
        *,
        worker_id: str,
        next_run_at: datetime | None = None,
    ) -> None:
        values: dict[str, Any] = {"claimed_by": None, "claimed_until": None}
        if next_run_at is not None:
            values["next_run_at"] = _aware_utc(next_run_at)
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(AutomationModel)
                .where(
                    AutomationModel.id == automation_id,
                    AutomationModel.claimed_by == worker_id,
                )
                .values(**values)
            )

    async def create_run(
        self,
        automation_id: int,
        *,
        scheduled_for: datetime,
        actual_started_at: datetime,
    ) -> AutomationRunRecord | None:
        scheduled = _aware_utc(scheduled_for)
        started = _aware_utc(actual_started_at)
        key = f"{automation_id}:{scheduled.isoformat()}"
        try:
            async with self._database.sessions() as session, session.begin():
                row = AutomationRunModel(
                    automation_id=automation_id,
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
                session.add(row)
                await session.flush()
                return _run_record(row)
        except IntegrityError:
            return None

    async def record_step(
        self,
        *,
        run_id: int,
        step_id: str,
        capability: str,
        status: str,
        input_summary: dict[str, Any],
        output_summary: dict[str, Any],
        started_at: datetime,
        finished_at: datetime,
        error_category: str | None,
    ) -> None:
        async with self._database.sessions() as session, session.begin():
            session.add(
                AutomationStepRunModel(
                    run_id=run_id,
                    step_id=step_id,
                    capability=capability,
                    status=status,
                    input_summary_json=_redacted_json(input_summary),
                    output_summary_json=_redacted_json(output_summary),
                    started_at=_aware_utc(started_at),
                    finished_at=_aware_utc(finished_at),
                    error_category=error_category,
                )
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
    ) -> None:
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(AutomationRunModel)
                .where(AutomationRunModel.id == run_id)
                .values(
                    status=status.value,
                    steps_completed=steps_completed,
                    llm_calls=llm_calls,
                    tool_calls=tool_calls,
                    messages_sent=messages_sent,
                    error_category=error_category,
                    result_summary_json=_redacted_json(summary),
                    finished_at=_aware_utc(finished_at),
                )
            )

    async def finish_automation_run(
        self,
        automation_id: int,
        *,
        worker_id: str,
        status: RunStatus,
        next_run_at: datetime | None,
        now: datetime,
        max_consecutive_failures: int,
    ) -> None:
        timestamp = _aware_utc(now)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(AutomationModel, automation_id)
            if row is None or row.claimed_by != worker_id:
                return
            row.last_run_at = timestamp
            row.run_count += 1
            row.claimed_by = None
            row.claimed_until = None
            row.updated_at = timestamp
            if status is RunStatus.SUCCEEDED:
                row.consecutive_failures = 0
                if row.max_runs is not None and row.run_count >= row.max_runs:
                    row.status = AutomationStatus.COMPLETED.value
                    row.next_run_at = None
                elif next_run_at is None:
                    row.status = AutomationStatus.COMPLETED.value
                    row.next_run_at = None
                else:
                    row.status = AutomationStatus.ACTIVE.value
                    row.next_run_at = _aware_utc(next_run_at)
            elif status is RunStatus.MISSED:
                row.next_run_at = _aware_utc(next_run_at) if next_run_at else None
                row.status = (
                    AutomationStatus.ACTIVE.value
                    if next_run_at is not None
                    else AutomationStatus.COMPLETED.value
                )
            elif status is RunStatus.BLOCKED:
                row.status = AutomationStatus.BLOCKED.value
                row.next_run_at = None
            else:
                row.consecutive_failures += 1
                if next_run_at is None or row.consecutive_failures >= max_consecutive_failures:
                    row.status = AutomationStatus.FAILED.value
                    row.next_run_at = None
                else:
                    row.status = AutomationStatus.ACTIVE.value
                    row.next_run_at = _aware_utc(next_run_at) if next_run_at else None

    async def run_history(
        self,
        automation_id: int,
        *,
        limit: int = 20,
    ) -> tuple[AutomationRunRecord, ...]:
        async with self._database.sessions() as session:
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
        async with self._database.sessions() as session:
            return await session.scalar(select(func.max(AutomationRunModel.finished_at)))

    async def next_due_at(self) -> datetime | None:
        async with self._database.sessions() as session:
            return await session.scalar(
                select(func.min(AutomationModel.next_run_at)).where(
                    AutomationModel.status == AutomationStatus.ACTIVE.value
                )
            )

    async def cleanup_runs(self, *, before: datetime) -> int:
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                delete(AutomationRunModel).where(
                    AutomationRunModel.created_at < _aware_utc(before),
                    AutomationRunModel.status != RunStatus.RUNNING.value,
                )
            )
        return int(cast(CursorResult[Any], result).rowcount or 0)


async def _bind_canonical_send_targets(
    session: AsyncSession,
    validated: ValidatedAutomation,
    authority: DelegatedAuthority,
    *,
    now: datetime,
) -> tuple[str | None, str | None]:
    collected = collect_send_targets(
        validated.script,
        creator_user_id=authority.creator_user_id,
        current_group_id=authority.current_group_id,
    )
    if collected.kind is None:
        return await _default_canonical_target(session, authority)
    if collected.kind == "person":
        owners: set[str] = set()
        for raw in collected.raw_ids:
            found = await active_person_id_for(session, raw)
            if found is None:
                raise ValueError("发送目标没有对应的永久主体")
            owners.add(found)
        if len(owners) != 1:
            raise ValueError("一条自动化只能绑定一个永久发送目标")
        return owners.pop(), None
    owners = set()
    for raw in collected.raw_ids:
        found = await active_space_id_for(session, raw)
        if found is None:
            raise ValueError("发送目标没有对应的永久空间")
        owners.add(found)
    if len(owners) != 1:
        raise ValueError("一条自动化只能绑定一个永久发送目标")
    return None, owners.pop()


async def _default_canonical_target(
    session: AsyncSession,
    authority: DelegatedAuthority,
) -> tuple[str | None, str | None]:
    """Bind non-sending automations to their canonical creation context."""

    if authority.current_group_id:
        target_space = await active_space_id_for(session, authority.current_group_id)
        if target_space is None:
            raise ValueError("发送目标没有对应的永久空间")
        return None, target_space
    target_person = await active_person_id_for(session, authority.creator_user_id)
    if target_person is None:
        raise ValueError("发送目标没有对应的永久主体")
    return target_person, None


def _automation_record(row: AutomationModel) -> AutomationRecord:
    return AutomationRecord(
        id=row.id,
        creator_user_id=row.creator_user_id,
        bot_user_id=row.bot_user_id,
        name=row.name,
        status=AutomationStatus(row.status),
        timezone=row.timezone,
        script=AutomationScript.model_validate_json(row.script_json),
        script_hash=row.script_hash,
        required_capabilities=tuple(json.loads(row.required_capabilities_json)),
        authority_snapshot=json.loads(row.authority_snapshot_json),
        created_from_message_id=row.created_from_message_id,
        next_run_at=_aware_utc(row.next_run_at) if row.next_run_at else None,
        last_run_at=_aware_utc(row.last_run_at) if row.last_run_at else None,
        run_count=row.run_count,
        max_runs=row.max_runs,
        consecutive_failures=row.consecutive_failures,
        misfire_grace_seconds=row.misfire_grace_seconds,
        created_at=_aware_utc(row.created_at),
        updated_at=_aware_utc(row.updated_at),
        canonical_creator_person_id=row.canonical_creator_person_id,
        canonical_target_person_id=row.canonical_target_person_id,
        canonical_target_space_id=row.canonical_target_space_id,
        canonical_presence_id=row.canonical_presence_id,
    )


def _run_record(row: AutomationRunModel) -> AutomationRunRecord:
    return AutomationRunRecord(
        id=row.id,
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

    def redact(item: Any, depth: int = 0) -> Any:
        if depth > 4:
            return "[truncated]"
        if isinstance(item, dict):
            return {
                str(key)[:128]: "[redacted]"
                if str(key).casefold() in sensitive
                else redact(child, depth + 1)
                for key, child in list(item.items())[:50]
            }
        if isinstance(item, list):
            return [redact(child, depth + 1) for child in item[:20]]
        if isinstance(item, str):
            return item[:1000]
        return item

    return json.dumps(redact(value), ensure_ascii=False, separators=(",", ":"))[:16000]
