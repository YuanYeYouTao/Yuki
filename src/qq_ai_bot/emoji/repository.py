"""Async persistence repository for emoji assets, scopes, jobs, and usage."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from qq_ai_bot.emoji.db_models import (
    EmojiAssetModel,
    EmojiJobModel,
    EmojiScopeStateModel,
    EmojiUsageEventModel,
)
from qq_ai_bot.emoji.models import (
    EmojiAnalysis,
    EmojiAsset,
    EmojiLifecycleStatus,
    EmojiScopeState,
    StoredEmojiMedia,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.unit_of_work import optional_session

EmojiJobType = Literal["analyze", "reanalyze", "rebuild_preview"]


@dataclass(frozen=True, slots=True)
class EmojiJob:
    id: int
    emoji_id: str
    job_type: EmojiJobType
    attempts: int


class EmojiRepository:
    """Keep every emoji state mutation explicit and transactionally consistent."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(self, emoji_id: str, *, session: AsyncSession | None = None) -> EmojiAsset | None:
        async with optional_session(self._database, session, write=False) as active:
            row = await active.get(EmojiAssetModel, emoji_id)
            return self._asset(row) if row is not None else None

    async def get_by_hash(self, sha256: str) -> EmojiAsset | None:
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(EmojiAssetModel).where(EmojiAssetModel.sha256 == sha256)
            )
            return self._asset(row) if row is not None else None

    async def near_duplicates(
        self,
        perceptual_hash: str,
        *,
        max_distance: int,
        exclude_id: str | None = None,
    ) -> tuple[EmojiAsset, ...]:
        if not 0 <= max_distance <= 64:
            raise ValueError("near duplicate distance must be between zero and 64")
        try:
            target = int(perceptual_hash, 16)
        except ValueError as exc:
            raise ValueError("invalid perceptual hash") from exc
        statement = select(EmojiAssetModel).where(EmojiAssetModel.perceptual_hash.is_not(None))
        if exclude_id is not None:
            statement = statement.where(EmojiAssetModel.id != exclude_id)
        async with self._database.sessions() as session:
            rows = (await session.scalars(statement)).all()
        matches: list[EmojiAsset] = []
        for row in rows:
            try:
                distance = (target ^ int(row.perceptual_hash or "", 16)).bit_count()
            except ValueError:
                continue
            if distance <= max_distance:
                matches.append(self._asset(row))
        return tuple(matches)

    async def resolve_id(self, value: str) -> EmojiAsset | None:
        """Resolve one exact UUID or an unambiguous displayed prefix."""

        normalized = value.strip().casefold()
        if not normalized:
            return None
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(EmojiAssetModel)
                    .where(func.lower(EmojiAssetModel.id).like(f"{normalized}%"))
                    .limit(2)
                )
            ).all()
            if len(rows) != 1:
                return None
            return self._asset(rows[0])

    async def record_candidate(
        self,
        media: StoredEmojiMedia,
        *,
        source_event_id: int | None,
        user_id: str,
        group_id: str | None,
        source_sub_type: str = "",
        source_emoji_id: str = "",
        source_package_id: str = "",
        now: datetime | None = None,
    ) -> tuple[EmojiAsset, bool]:
        timestamp = now or datetime.now(UTC)
        asset_id = str(uuid.uuid4())
        values = {
            "id": asset_id,
            "sha256": media.sha256,
            "perceptual_hash": media.perceptual_hash,
            "relative_path": media.relative_path,
            "preview_relative_path": media.preview_relative_path,
            "image_format": media.image_format,
            "mime_type": media.mime_type,
            "byte_size": media.byte_size,
            "width": media.width,
            "height": media.height,
            "frame_count": media.frame_count,
            "animated": media.animated,
            "status": EmojiLifecycleStatus.CANDIDATE.value,
            "source_event_id": source_event_id,
            "first_seen_user_id": user_id,
            "first_seen_group_id": group_id,
            "source_sub_type": source_sub_type[:64],
            "source_emoji_id": source_emoji_id[:128],
            "source_package_id": source_package_id[:128],
            "seen_count": 1,
            "first_seen_at": timestamp,
            "last_seen_at": timestamp,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        async with self._database.sessions() as session, session.begin():
            statement = (
                insert(EmojiAssetModel)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[EmojiAssetModel.sha256],
                    set_={
                        "seen_count": EmojiAssetModel.seen_count + 1,
                        "last_seen_at": timestamp,
                        "updated_at": timestamp,
                        "relative_path": media.relative_path,
                        "preview_relative_path": media.preview_relative_path,
                        "perceptual_hash": media.perceptual_hash,
                        "missing_since": None,
                        "status": func.iif(
                            EmojiAssetModel.status == EmojiLifecycleStatus.MISSING.value,
                            EmojiLifecycleStatus.CANDIDATE.value,
                            EmojiAssetModel.status,
                        ),
                    },
                )
            )
            result = await session.execute(statement)
            row = await session.scalar(
                select(EmojiAssetModel).where(EmojiAssetModel.sha256 == media.sha256)
            )
            if row is None:
                raise RuntimeError("emoji candidate upsert did not return a row")
            created = row.id == asset_id and _rowcount(result) == 1
            return self._asset(row), created

    async def save_analysis(
        self,
        emoji_id: str,
        analysis: EmojiAnalysis,
        *,
        status: EmojiLifecycleStatus,
        now: datetime | None = None,
    ) -> EmojiAsset:
        timestamp = now or datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(EmojiAssetModel, emoji_id)
            if row is None:
                raise LookupError("emoji asset not found")
            row.description = analysis.description
            row.emotion_tags_json = json.dumps(analysis.emotion_tags, ensure_ascii=False)
            row.usage_scenarios_json = json.dumps(analysis.usage_scenarios, ensure_ascii=False)
            row.ocr_text = analysis.ocr_text
            row.intensity = analysis.intensity
            row.confidence = analysis.confidence
            row.analysis_version = analysis.analysis_version
            enabled_scope_count = await session.scalar(
                select(func.count())
                .select_from(EmojiScopeStateModel)
                .where(
                    EmojiScopeStateModel.emoji_id == emoji_id,
                    EmojiScopeStateModel.enabled.is_(True),
                )
            )
            if status is EmojiLifecycleStatus.RECOGNIZED and enabled_scope_count:
                row.status = EmojiLifecycleStatus.ADOPTED.value
            else:
                row.status = status.value
            if status is EmojiLifecycleStatus.REJECTED:
                await session.execute(
                    delete(EmojiScopeStateModel).where(EmojiScopeStateModel.emoji_id == emoji_id)
                )
            row.updated_at = timestamp
            await session.flush()
            return self._asset(row)

    async def set_status(
        self,
        emoji_id: str,
        status: EmojiLifecycleStatus,
        *,
        now: datetime | None = None,
        session: AsyncSession | None = None,
    ) -> EmojiAsset:
        timestamp = now or datetime.now(UTC)
        async with optional_session(self._database, session, write=True) as active:
            row = await active.get(EmojiAssetModel, emoji_id)
            if row is None:
                raise LookupError("emoji asset not found")
            row.status = status.value
            row.updated_at = timestamp
            row.missing_since = timestamp if status is EmojiLifecycleStatus.MISSING else None
            if status is not EmojiLifecycleStatus.ADOPTED:
                await active.execute(
                    delete(EmojiScopeStateModel).where(EmojiScopeStateModel.emoji_id == emoji_id)
                )
            await active.flush()
            return self._asset(row)

    async def set_pinned(
        self, emoji_id: str, pinned: bool, *, session: AsyncSession | None = None
    ) -> EmojiAsset:
        async with optional_session(self._database, session, write=True) as active:
            row = await active.get(EmojiAssetModel, emoji_id)
            if row is None:
                raise LookupError("emoji asset not found")
            row.pinned = pinned
            row.updated_at = datetime.now(UTC)
            await active.flush()
            return self._asset(row)

    async def adopt_scope(
        self,
        emoji_id: str,
        *,
        scope_type: Literal["global", "group"],
        scope_id: str = "",
        weight: float = 1.0,
        now: datetime | None = None,
    ) -> EmojiScopeState:
        if scope_type == "global" and scope_id:
            raise ValueError("global emoji scope_id must be empty")
        if scope_type == "group" and not scope_id:
            raise ValueError("group emoji scope_id is required")
        if weight < 0:
            raise ValueError("emoji scope weight must be non-negative")
        timestamp = now or datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            asset = await session.get(EmojiAssetModel, emoji_id)
            if asset is None:
                raise LookupError("emoji asset not found")
            if asset.status in {
                EmojiLifecycleStatus.BANNED.value,
                EmojiLifecycleStatus.MISSING.value,
            }:
                raise ValueError("banned or missing emoji cannot be adopted")
            statement = insert(EmojiScopeStateModel).values(
                emoji_id=emoji_id,
                scope_type=scope_type,
                scope_id=scope_id,
                enabled=True,
                weight=weight,
                adopted_at=timestamp,
                updated_at=timestamp,
            )
            statement = statement.on_conflict_do_update(
                index_elements=[
                    EmojiScopeStateModel.emoji_id,
                    EmojiScopeStateModel.scope_type,
                    EmojiScopeStateModel.scope_id,
                ],
                set_={"enabled": True, "weight": weight, "updated_at": timestamp},
            )
            await session.execute(statement)
            asset.status = EmojiLifecycleStatus.ADOPTED.value
            asset.updated_at = timestamp
            row = await session.scalar(
                select(EmojiScopeStateModel).where(
                    EmojiScopeStateModel.emoji_id == emoji_id,
                    EmojiScopeStateModel.scope_type == scope_type,
                    EmojiScopeStateModel.scope_id == scope_id,
                )
            )
            if row is None:
                raise RuntimeError("emoji scope upsert did not return a row")
            return self._scope(row)

    async def remove_scope(
        self,
        emoji_id: str,
        *,
        scope_type: Literal["global", "group"],
        scope_id: str = "",
    ) -> bool:
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                delete(EmojiScopeStateModel).where(
                    EmojiScopeStateModel.emoji_id == emoji_id,
                    EmojiScopeStateModel.scope_type == scope_type,
                    EmojiScopeStateModel.scope_id == scope_id,
                )
            )
            remaining = await session.scalar(
                select(func.count())
                .select_from(EmojiScopeStateModel)
                .where(EmojiScopeStateModel.emoji_id == emoji_id)
            )
            if _rowcount(result) and not remaining:
                await session.execute(
                    update(EmojiAssetModel)
                    .where(EmojiAssetModel.id == emoji_id)
                    .values(
                        status=EmojiLifecycleStatus.RECOGNIZED.value, updated_at=datetime.now(UTC)
                    )
                )
            return bool(_rowcount(result))

    async def list_assets(
        self,
        *,
        status: EmojiLifecycleStatus | None = None,
        limit: int = 50,
    ) -> tuple[EmojiAsset, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        statement = select(EmojiAssetModel)
        if status is not None:
            statement = statement.where(EmojiAssetModel.status == status.value)
        statement = statement.order_by(EmojiAssetModel.updated_at.desc()).limit(limit)
        async with self._database.sessions() as session:
            rows = (await session.scalars(statement)).all()
            return tuple(self._asset(row) for row in rows)

    async def selectable(
        self,
        *,
        actor_user_id: str,
        group_id: str | None,
        cooldown_after: datetime,
        scope_cooldown_after: datetime | None,
        limit: int,
    ) -> tuple[tuple[EmojiAsset, float], ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        enabled_scope = aliased(EmojiScopeStateModel, name="enabled_scope")
        scope_filter = enabled_scope.scope_type == "global"
        if group_id:
            scope_filter = or_(
                scope_filter,
                (enabled_scope.scope_type == "group") & (enabled_scope.scope_id == group_id),
            )
        recent_scope = None
        if scope_cooldown_after is not None:
            recent_scope = (
                select(func.count())
                .select_from(EmojiUsageEventModel)
                .where(EmojiUsageEventModel.created_at > scope_cooldown_after)
            )
            if group_id is not None:
                recent_scope = recent_scope.where(EmojiUsageEventModel.group_id == group_id)
            else:
                recent_scope = recent_scope.where(
                    EmojiUsageEventModel.group_id.is_(None),
                    EmojiUsageEventModel.actor_user_id == actor_user_id,
                )
        statement = (
            select(EmojiAssetModel, func.max(enabled_scope.weight))
            .join(enabled_scope, enabled_scope.emoji_id == EmojiAssetModel.id)
            .where(
                EmojiAssetModel.status == EmojiLifecycleStatus.ADOPTED.value,
                enabled_scope.enabled.is_(True),
                scope_filter,
                or_(
                    EmojiAssetModel.last_used_at.is_(None),
                    EmojiAssetModel.last_used_at <= cooldown_after,
                ),
            )
            .group_by(EmojiAssetModel.id)
            .order_by(EmojiAssetModel.pinned.desc(), EmojiAssetModel.last_used_at.asc())
            .limit(limit)
        )
        if group_id is not None:
            disabled_group = aliased(EmojiScopeStateModel, name="disabled_group")
            disabled_override = (
                select(disabled_group.id)
                .select_from(disabled_group)
                .where(
                    disabled_group.emoji_id == EmojiAssetModel.id,
                    disabled_group.scope_type == "group",
                    disabled_group.scope_id == group_id,
                    disabled_group.enabled.is_(False),
                )
                .correlate(EmojiAssetModel)
                .exists()
            )
            statement = statement.where(~disabled_override)
        async with self._database.sessions() as session:
            if recent_scope is not None and await session.scalar(recent_scope):
                return ()
            rows = (await session.execute(statement)).all()
            return tuple((self._asset(row), float(weight or 0)) for row, weight in rows)

    async def adopted_count(self, *, group_id: str | None = None) -> int:
        statement = select(func.count(func.distinct(EmojiScopeStateModel.emoji_id))).where(
            EmojiScopeStateModel.enabled.is_(True)
        )
        if group_id is None:
            statement = statement.where(EmojiScopeStateModel.scope_type == "global")
        else:
            statement = statement.where(
                EmojiScopeStateModel.scope_type == "group",
                EmojiScopeStateModel.scope_id == group_id,
            )
        async with self._database.sessions() as session:
            return int(await session.scalar(statement) or 0)

    async def has_enabled_scope(
        self,
        emoji_id: str,
        *,
        scope_type: Literal["global", "group"],
        scope_id: str,
    ) -> bool:
        statement = (
            select(func.count())
            .select_from(EmojiScopeStateModel)
            .where(
                EmojiScopeStateModel.emoji_id == emoji_id,
                EmojiScopeStateModel.scope_type == scope_type,
                EmojiScopeStateModel.scope_id == scope_id,
                EmojiScopeStateModel.enabled.is_(True),
            )
        )
        async with self._database.sessions() as session:
            return bool(await session.scalar(statement))

    async def enabled_in_scope(self, emoji_id: str, *, group_id: str | None) -> bool:
        scope_filter = EmojiScopeStateModel.scope_type == "global"
        if group_id is not None:
            scope_filter = or_(
                scope_filter,
                (EmojiScopeStateModel.scope_type == "group")
                & (EmojiScopeStateModel.scope_id == group_id),
            )
        statement = (
            select(func.count())
            .select_from(EmojiScopeStateModel)
            .join(EmojiAssetModel, EmojiAssetModel.id == EmojiScopeStateModel.emoji_id)
            .where(
                EmojiScopeStateModel.emoji_id == emoji_id,
                EmojiScopeStateModel.enabled.is_(True),
                EmojiAssetModel.status == EmojiLifecycleStatus.ADOPTED.value,
                scope_filter,
            )
        )
        async with self._database.sessions() as session:
            if group_id is not None:
                disabled = await session.scalar(
                    select(func.count())
                    .select_from(EmojiScopeStateModel)
                    .where(
                        EmojiScopeStateModel.emoji_id == emoji_id,
                        EmojiScopeStateModel.scope_type == "group",
                        EmojiScopeStateModel.scope_id == group_id,
                        EmojiScopeStateModel.enabled.is_(False),
                    )
                )
                if disabled:
                    return False
            return bool(await session.scalar(statement))

    async def set_group_enabled(self, emoji_id: str, *, group_id: str, enabled: bool) -> None:
        if not group_id:
            raise ValueError("group_id is required")
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            asset = await session.get(EmojiAssetModel, emoji_id)
            if asset is None:
                raise LookupError("emoji asset not found")
            if asset.status in {
                EmojiLifecycleStatus.BANNED.value,
                EmojiLifecycleStatus.MISSING.value,
                EmojiLifecycleStatus.REJECTED.value,
            }:
                raise ValueError("emoji is not eligible for a group scope")
            statement = insert(EmojiScopeStateModel).values(
                emoji_id=emoji_id,
                scope_type="group",
                scope_id=group_id,
                enabled=enabled,
                weight=1.0,
                adopted_at=now,
                updated_at=now,
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        EmojiScopeStateModel.emoji_id,
                        EmojiScopeStateModel.scope_type,
                        EmojiScopeStateModel.scope_id,
                    ],
                    set_={"enabled": enabled, "updated_at": now},
                )
            )
            if enabled and asset.status == EmojiLifecycleStatus.RECOGNIZED.value:
                asset.status = EmojiLifecycleStatus.ADOPTED.value
                asset.updated_at = now

    async def replaceable(
        self,
        *,
        scope_type: Literal["global", "group"],
        scope_id: str,
    ) -> tuple[EmojiAsset, ...]:
        statement = (
            select(EmojiAssetModel)
            .join(EmojiScopeStateModel, EmojiScopeStateModel.emoji_id == EmojiAssetModel.id)
            .where(
                EmojiScopeStateModel.scope_type == scope_type,
                EmojiScopeStateModel.scope_id == scope_id,
                EmojiAssetModel.pinned.is_(False),
            )
            .order_by(EmojiAssetModel.last_used_at.asc(), EmojiAssetModel.updated_at.asc())
        )
        async with self._database.sessions() as session:
            rows = (await session.scalars(statement)).all()
            return tuple(self._asset(row) for row in rows)

    async def enqueue(self, emoji_id: str, job_type: EmojiJobType = "analyze") -> bool:
        timestamp = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            statement = insert(EmojiJobModel).values(
                emoji_id=emoji_id,
                job_type=job_type,
                status="pending",
                attempts=0,
                next_attempt_at=timestamp,
                created_at=timestamp,
                updated_at=timestamp,
            )
            statement = statement.on_conflict_do_nothing()
            result = await session.execute(statement)
            return bool(_rowcount(result))

    async def claim_jobs(
        self,
        *,
        worker_id: str,
        limit: int,
        lease_seconds: int,
    ) -> tuple[EmojiJob, ...]:
        if limit <= 0 or lease_seconds <= 0:
            raise ValueError("job limits must be positive")
        now = datetime.now(UTC)
        lease = now + timedelta(seconds=lease_seconds)
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(EmojiJobModel)
                .where(
                    EmojiJobModel.status == "processing",
                    EmojiJobModel.claimed_until < now,
                )
                .values(status="pending", claimed_until=None, claimed_by=None, updated_at=now)
            )
            ids = tuple(
                await session.scalars(
                    select(EmojiJobModel.id)
                    .where(
                        EmojiJobModel.status == "pending",
                        EmojiJobModel.next_attempt_at <= now,
                    )
                    .order_by(EmojiJobModel.created_at)
                    .limit(limit)
                )
            )
            if not ids:
                return ()
            await session.execute(
                update(EmojiJobModel)
                .where(EmojiJobModel.id.in_(ids), EmojiJobModel.status == "pending")
                .values(
                    status="processing", claimed_until=lease, claimed_by=worker_id, updated_at=now
                )
            )
            rows = (
                await session.scalars(
                    select(EmojiJobModel).where(
                        EmojiJobModel.id.in_(ids), EmojiJobModel.claimed_by == worker_id
                    )
                )
            ).all()
            return tuple(
                EmojiJob(row.id, row.emoji_id, row.job_type, row.attempts)  # type: ignore[arg-type]
                for row in rows
            )

    async def complete_job(self, job_id: int) -> None:
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(EmojiJobModel)
                .where(EmojiJobModel.id == job_id)
                .values(
                    status="completed",
                    claimed_until=None,
                    claimed_by=None,
                    error_category=None,
                    updated_at=datetime.now(UTC),
                )
            )

    async def fail_job(
        self,
        job_id: int,
        *,
        error_category: str,
        max_attempts: int,
        retry_delay_seconds: float,
    ) -> None:
        if max_attempts <= 0 or retry_delay_seconds < 0:
            raise ValueError("retry policy is invalid")
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(EmojiJobModel, job_id)
            if row is None:
                return
            row.attempts += 1
            row.status = "failed" if row.attempts >= max_attempts else "pending"
            row.next_attempt_at = now + timedelta(seconds=retry_delay_seconds)
            row.claimed_until = None
            row.claimed_by = None
            row.error_category = error_category[:64]
            row.updated_at = now

    async def mark_used(
        self,
        emoji_id: str,
        *,
        actor_user_id: str | None,
        group_id: str | None,
        trigger_message_id: str,
        source: str,
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(EmojiAssetModel)
                .where(EmojiAssetModel.id == emoji_id)
                .values(
                    use_count=EmojiAssetModel.use_count + 1,
                    last_used_at=now,
                    updated_at=now,
                )
            )
            session.add(
                EmojiUsageEventModel(
                    emoji_id=emoji_id,
                    actor_user_id=actor_user_id,
                    group_id=group_id,
                    trigger_message_id=trigger_message_id,
                    source=source[:32],
                    created_at=now,
                )
            )

    async def counts(self) -> dict[str, int]:
        async with self._database.sessions() as session:
            rows = await session.execute(
                select(EmojiAssetModel.status, func.count()).group_by(EmojiAssetModel.status)
            )
            counts = {str(status): int(count) for status, count in rows}
            counts["jobs_pending"] = int(
                await session.scalar(
                    select(func.count())
                    .select_from(EmojiJobModel)
                    .where(EmojiJobModel.status.in_(("pending", "processing")))
                )
                or 0
            )
            return counts

    async def cleanup_candidates(self, *, before: datetime) -> tuple[EmojiAsset, ...]:
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(EmojiAssetModel).where(
                        EmojiAssetModel.status.in_(
                            (
                                EmojiLifecycleStatus.CANDIDATE.value,
                                EmojiLifecycleStatus.REJECTED.value,
                            )
                        ),
                        EmojiAssetModel.pinned.is_(False),
                        EmojiAssetModel.updated_at < before,
                    )
                )
            ).all()
            return tuple(self._asset(row) for row in rows)

    async def delete_asset(self, emoji_id: str) -> bool:
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                delete(EmojiAssetModel).where(
                    EmojiAssetModel.id == emoji_id,
                    EmojiAssetModel.pinned.is_(False),
                    EmojiAssetModel.status != EmojiLifecycleStatus.ADOPTED.value,
                )
            )
            return bool(_rowcount(result))

    @staticmethod
    def _asset(row: EmojiAssetModel) -> EmojiAsset:
        return EmojiAsset(
            id=row.id,
            sha256=row.sha256,
            perceptual_hash=row.perceptual_hash,
            relative_path=row.relative_path,
            preview_relative_path=row.preview_relative_path,
            image_format=row.image_format,
            mime_type=row.mime_type,
            byte_size=row.byte_size,
            width=row.width,
            height=row.height,
            frame_count=row.frame_count,
            animated=row.animated,
            status=EmojiLifecycleStatus(row.status),
            description=row.description,
            emotion_tags=tuple(json.loads(row.emotion_tags_json)),
            usage_scenarios=tuple(json.loads(row.usage_scenarios_json)),
            ocr_text=row.ocr_text,
            intensity=row.intensity,
            confidence=row.confidence,
            analysis_version=row.analysis_version,
            pinned=row.pinned,
            seen_count=row.seen_count,
            use_count=row.use_count,
            source_event_id=row.source_event_id,
            first_seen_user_id=row.first_seen_user_id,
            first_seen_group_id=row.first_seen_group_id,
            first_seen_at=row.first_seen_at,
            last_seen_at=row.last_seen_at,
            last_used_at=row.last_used_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _scope(row: EmojiScopeStateModel) -> EmojiScopeState:
        return EmojiScopeState(
            emoji_id=row.emoji_id,
            scope_type=row.scope_type,  # type: ignore[arg-type]
            scope_id=row.scope_id,
            enabled=row.enabled,
            weight=row.weight,
            adopted_at=row.adopted_at,
            updated_at=row.updated_at,
        )


def _rowcount(result: object) -> int:
    value = getattr(result, "rowcount", 0)
    return value if isinstance(value, int) else 0
