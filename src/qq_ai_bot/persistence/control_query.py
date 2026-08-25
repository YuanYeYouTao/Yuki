"""Read-only persistence adapter for control-plane query projections."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Protocol, TypeVar

from sqlalchemy import Integer, Select, event, func, literal_column, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from qq_ai_bot import __version__
from qq_ai_bot.control_plane.operations import OperationRef, OperationStatus, StateEpoch
from qq_ai_bot.control_plane.paging import Page, PageRequest
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_cursors import (
    decode_integer_cursor_key,
    decode_resource_cursor,
    decode_time_id_key,
    encode_query_cursor,
    encode_time_id_key,
)
from qq_ai_bot.control_plane.query_types import (
    AuditEventView,
    BackfillConflictView,
    BackfillOperationView,
    ControlQueryError,
    ConversationView,
    CountSnapshot,
    IdentityBindingView,
    IdentityResolution,
    ManagementHealthView,
    PendingRestartView,
    PersonActiveRouteView,
    PersonView,
    PresenceConnectionState,
    PresenceView,
    QueryCursorPhase,
    QueryResourceKind,
    QueueSummary,
    RouteKind,
    SpaceActiveRouteView,
    SpaceBindingIngestRouteView,
    SpaceBindingView,
    SpaceView,
    SystemSnapshot,
    YukiSummaryView,
    classify_route_reference,
    mask_external_id,
    sanitize_projected_display,
)
from qq_ai_bot.control_plane.tokens import require_opaque_token
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    PersonActiveRouteModel,
    SpaceActiveRouteModel,
    SpaceBindingIngestRouteModel,
)
from qq_ai_bot.conversation.rollup.db_models import ConversationScopeModel
from qq_ai_bot.domain.identity import (
    ConversationGeneration,
    ConversationId,
    IdentityBindingId,
    PersonId,
    PresenceId,
    RouteGeneration,
    SpaceBindingId,
    SpaceId,
)
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBackfillRunModel,
    IdentityBindingModel,
    IdentityConflictModel,
    IdentityRuntimeStateModel,
    PresenceModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    AdminOperationEventModel,
    GroupModel,
    MemoryJobModel,
    PersonModel,
    RuntimeConfigOverrideModel,
)


class _HasId(Protocol):
    id: str


class _AuditRow(Protocol):
    id: int
    capability: object
    operation: object
    target_type: object
    success: object
    error_category: object
    duration_seconds: int | float
    created_at: datetime | None


class _ConflictRow(Protocol):
    id: int
    subject_kind: object
    conflict_kind: object
    status: object
    error_category: object


_T = TypeVar("_T")
_IdT = TypeVar("_IdT", bound=_HasId)
_BACKFILL_STATUS = {
    "pending": OperationStatus.QUEUED,
    "running": OperationStatus.RUNNING,
    "succeeded": OperationStatus.SUCCEEDED,
    "failed": OperationStatus.FAILED,
    "cancelled": OperationStatus.CANCELLED,
}


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value


def _now() -> datetime:
    return datetime.now(UTC)


def _safe_token(value: object, *, fallback: str, max_length: int) -> str:
    if type(value) is not str or not value:
        return fallback
    try:
        return require_opaque_token(value, name="token", max_length=max_length)
    except (TypeError, ValueError):
        return fallback


def project_audit_event(row: _AuditRow, *, snapshot_at: datetime) -> AuditEventView:
    """Project stored audit data. Corrupt rows fail closed as state_mismatch."""

    if type(snapshot_at) is not datetime:
        raise TypeError("snapshot_at must be datetime")
    try:
        return AuditEventView(
            audit_id=int(row.id),
            capability=_safe_token(row.capability, fallback="unspecified", max_length=64),
            operation=_safe_token(row.operation, fallback="unspecified", max_length=128),
            target_type=_safe_token(row.target_type, fallback="unspecified", max_length=64),
            success=bool(row.success),
            error_category=(
                None
                if row.error_category is None
                else _safe_token(row.error_category, fallback="unclassified", max_length=64)
            ),
            duration_seconds=float(row.duration_seconds),
            created_at=_as_aware(row.created_at) or snapshot_at,
        )
    except (TypeError, ValueError) as exc:
        raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH)) from exc


def _project_conflict(row: _ConflictRow) -> BackfillConflictView:
    try:
        return BackfillConflictView(
            conflict_id=int(row.id),
            subject_kind=_safe_token(row.subject_kind, fallback="account", max_length=16),
            conflict_kind=_safe_token(row.conflict_kind, fallback="unclassified", max_length=32),
            status=_safe_token(row.status, fallback="open", max_length=16),
            error_category=(
                None
                if row.error_category is None
                else _safe_token(row.error_category, fallback="unclassified", max_length=64)
            ),
        )
    except (TypeError, ValueError) as exc:
        raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH)) from exc


def _legacy_rowid(value: object) -> int:
    if type(value) is int and type(value) is not bool:
        if value < 1:
            raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH))
        return value
    if type(value) is str:
        try:
            rowid = int(value)
        except ValueError as exc:
            raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH)) from exc
        if value != str(rowid) or rowid < 1:
            raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH))
        return rowid
    raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH))


def _unresolved_after(phase: QueryCursorPhase, key: str | None) -> int | None:
    if phase is not QueryCursorPhase.UNRESOLVED or key is None:
        return None
    return decode_integer_cursor_key(key, minimum=0)


def _backfill_progress(status: str, processed: int, skipped: int, conflicts: int) -> float:
    if status == "pending":
        return 0.0
    if status in {"succeeded", "cancelled"}:
        return 1.0
    total = processed + skipped + conflicts
    if total <= 0:
        return 0.0
    return min(1.0, processed / total)


class ControlQueryAdapter:
    """Keyset reader over existing identity and conversation tables."""

    def __init__(self, database: Database) -> None:
        if type(database) is not Database:
            raise TypeError("database must be Database")
        self._database = database

    @asynccontextmanager
    async def _reader(self) -> AsyncIterator[AsyncSession]:
        async with self._database.sessions() as session:
            session.autoflush = False

            def _reject_flush(*_args: object, **_kwargs: object) -> None:
                raise RuntimeError("control query adapter must not flush")

            sync_session = session.sync_session
            event.listen(sync_session, "before_flush", _reject_flush)
            try:
                yield session
            finally:
                event.remove(sync_session, "before_flush", _reject_flush)
                await session.rollback()

    async def _runtime(self, session: AsyncSession) -> tuple[StateEpoch, int]:
        rows = list(await session.scalars(select(IdentityRuntimeStateModel)))
        if len(rows) != 1:
            raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH))
        row = rows[0]
        if row.id != 1 or row.state not in {item.value for item in StateEpoch}:
            raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH))
        return StateEpoch(row.state), int(row.revision)

    def _cursor_state(
        self,
        request: PageRequest,
        kind: QueryResourceKind,
        *,
        epoch: StateEpoch,
    ) -> tuple[QueryCursorPhase, str | None]:
        if type(request) is not PageRequest:
            raise TypeError("request must be PageRequest")
        if request.cursor is None:
            start = (
                QueryCursorPhase.TIME_ID
                if kind is QueryResourceKind.AUDIT
                else QueryCursorPhase.CANONICAL
            )
            return start, None
        return decode_resource_cursor(request.cursor, expected_kind=kind, epoch=epoch)

    async def _keyset(
        self,
        session: AsyncSession,
        model: type[_T],
        column: InstrumentedAttribute[str],
        after: str | None,
        limit: int,
    ) -> tuple[list[_T], bool]:
        stmt = select(model)
        if after is not None:
            stmt = stmt.where(column > after)
        stmt = stmt.order_by(column.asc()).limit(limit)
        rows = list(await session.scalars(stmt))
        has_more = len(rows) == limit
        return (rows[:-1] if has_more else rows), has_more

    async def _unresolved_people(
        self, session: AsyncSession, after: int | None, limit: int
    ) -> tuple[list[tuple[PersonModel, int]], bool]:
        rowid = literal_column("rowid", Integer)
        stmt = select(PersonModel, rowid).where(
            PersonModel.canonical_person_id.is_(None),
            PersonModel.is_bot.is_(False),
        )
        if after is not None:
            stmt = stmt.where(rowid > after)
        stmt = stmt.order_by(rowid.asc()).limit(limit)
        rows = list(await session.execute(stmt))
        has_more = len(rows) == limit
        selected = rows[:-1] if has_more else rows
        return [(row[0], _legacy_rowid(row[1])) for row in selected], has_more

    async def _unresolved_groups(
        self, session: AsyncSession, after: int | None, limit: int
    ) -> tuple[list[tuple[GroupModel, int]], bool]:
        rowid = literal_column("rowid", Integer)
        stmt = select(GroupModel, rowid).where(GroupModel.canonical_space_id.is_(None))
        if after is not None:
            stmt = stmt.where(rowid > after)
        stmt = stmt.order_by(rowid.asc()).limit(limit)
        rows = list(await session.execute(stmt))
        has_more = len(rows) == limit
        selected = rows[:-1] if has_more else rows
        return [(row[0], _legacy_rowid(row[1])) for row in selected], has_more

    async def _load_by_ids(
        self,
        session: AsyncSession,
        model: type[_IdT],
        column: InstrumentedAttribute[str],
        ids: Sequence[str],
    ) -> dict[str, _IdT]:
        if not ids:
            return {}
        rows = await session.scalars(select(model).where(column.in_(tuple(ids))))
        return {row.id: row for row in rows}

    async def _count(self, session: AsyncSession, stmt: Select[tuple[int]]) -> int:
        value = await session.scalar(stmt)
        return int(value or 0)

    async def _queue(self, session: AsyncSession) -> QueueSummary:
        return QueueSummary(
            backfill_pending=await self._count(
                session,
                select(func.count())
                .select_from(IdentityBackfillRunModel)
                .where(IdentityBackfillRunModel.status == "pending"),
            ),
            backfill_running=await self._count(
                session,
                select(func.count())
                .select_from(IdentityBackfillRunModel)
                .where(IdentityBackfillRunModel.status == "running"),
            ),
            memory_jobs_pending=await self._count(
                session,
                select(func.count())
                .select_from(MemoryJobModel)
                .where(MemoryJobModel.status == "pending"),
            ),
            memory_jobs_processing=await self._count(
                session,
                select(func.count())
                .select_from(MemoryJobModel)
                .where(MemoryJobModel.status == "processing"),
            ),
        )

    async def _pending_restart(self, session: AsyncSession) -> PendingRestartView:
        rows = list(
            await session.scalars(
                select(RuntimeConfigOverrideModel.config_key)
                .where(RuntimeConfigOverrideModel.apply_mode == "restart_required")
                .distinct()
                .order_by(RuntimeConfigOverrideModel.config_key.asc())
            )
        )
        keys: list[str] = []
        for raw in rows:
            token = _safe_token(raw, fallback="", max_length=128)
            if token:
                keys.append(token)
        unique = tuple(dict.fromkeys(keys))
        return PendingRestartView(unique, len(unique))

    async def _counts(self, session: AsyncSession) -> dict[str, CountSnapshot]:
        unresolved_people = (
            select(func.count())
            .select_from(PersonModel)
            .where(PersonModel.canonical_person_id.is_(None), PersonModel.is_bot.is_(False))
        )
        unresolved_groups = (
            select(func.count())
            .select_from(GroupModel)
            .where(GroupModel.canonical_space_id.is_(None))
        )
        unresolved_scopes = (
            select(func.count())
            .select_from(ConversationScopeModel)
            .where(ConversationScopeModel.canonical_conversation_id.is_(None))
        )
        return {
            "persons": CountSnapshot(
                await self._count(session, select(func.count()).select_from(CanonicalPersonModel)),
                await self._count(session, unresolved_people),
            ),
            "identity_bindings": CountSnapshot(
                await self._count(session, select(func.count()).select_from(IdentityBindingModel)),
                await self._count(session, unresolved_people),
            ),
            "spaces": CountSnapshot(
                await self._count(session, select(func.count()).select_from(CanonicalSpaceModel)),
                await self._count(session, unresolved_groups),
            ),
            "space_bindings": CountSnapshot(
                await self._count(session, select(func.count()).select_from(SpaceBindingModel)),
                await self._count(session, unresolved_groups),
            ),
            "presences": CountSnapshot(
                await self._count(session, select(func.count()).select_from(PresenceModel)),
                0,
            ),
            "conversations": CountSnapshot(
                await self._count(
                    session, select(func.count()).select_from(CanonicalConversationModel)
                ),
                await self._count(session, unresolved_scopes),
            ),
        }

    async def read_system(self) -> SystemSnapshot:
        async with self._reader() as session:
            epoch, revision = await self._runtime(session)
            counts = await self._counts(session)
            return SystemSnapshot(
                version=__version__,
                identity_state=epoch,
                identity_revision=revision,
                persons=counts["persons"],
                identity_bindings=counts["identity_bindings"],
                spaces=counts["spaces"],
                space_bindings=counts["space_bindings"],
                presences=counts["presences"],
                conversations=counts["conversations"],
                queue=await self._queue(session),
                pending_restart=await self._pending_restart(session),
            )

    async def read_yuki(self) -> YukiSummaryView:
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            presence_count = await self._count(
                session, select(func.count()).select_from(PresenceModel)
            )
            return YukiSummaryView(
                yuki_count=1, presence_count=presence_count, identity_state=epoch
            )

    async def read_health(self) -> ManagementHealthView:
        reachable = await self._database.ping()
        async with self._reader() as session:
            epoch, revision = await self._runtime(session)
            return ManagementHealthView(
                identity_state=epoch,
                identity_revision=revision,
                database="ok" if reachable else "unavailable",
                queue=await self._queue(session),
            )

    async def _binding_counts(
        self, session: AsyncSession, person_ids: Sequence[str]
    ) -> dict[str, int]:
        if not person_ids:
            return {}
        rows = await session.execute(
            select(IdentityBindingModel.person_id, func.count())
            .where(IdentityBindingModel.person_id.in_(tuple(person_ids)))
            .group_by(IdentityBindingModel.person_id)
        )
        return {str(person_id): int(count) for person_id, count in rows}

    async def _space_binding_counts(
        self, session: AsyncSession, space_ids: Sequence[str]
    ) -> dict[str, int]:
        if not space_ids:
            return {}
        rows = await session.execute(
            select(SpaceBindingModel.space_id, func.count())
            .where(SpaceBindingModel.space_id.in_(tuple(space_ids)))
            .group_by(SpaceBindingModel.space_id)
        )
        return {str(space_id): int(count) for space_id, count in rows}

    async def _space_external_ids(
        self, session: AsyncSession, space_ids: Sequence[str]
    ) -> dict[str, tuple[str, ...]]:
        if not space_ids:
            return {}
        rows = await session.execute(
            select(SpaceBindingModel.space_id, SpaceBindingModel.external_space_id).where(
                SpaceBindingModel.space_id.in_(tuple(space_ids))
            )
        )
        grouped: dict[str, list[str]] = {}
        for space_id, external_id in rows:
            grouped.setdefault(str(space_id), []).append(str(external_id))
        return {space_id: tuple(tokens) for space_id, tokens in grouped.items()}

    def _page(
        self,
        items: list[_T],
        *,
        kind: QueryResourceKind,
        phase: QueryCursorPhase,
        next_key: str | None,
        snapshot_at: datetime,
    ) -> Page[_T]:
        cursor = None if next_key is None else encode_query_cursor(kind, phase, next_key)
        return Page(items, next_cursor=cursor, snapshot_at=snapshot_at)

    async def list_persons(self, request: PageRequest) -> Page[PersonView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            phase, key = self._cursor_state(request, QueryResourceKind.PERSON, epoch=epoch)
            items: list[PersonView] = []
            if phase is QueryCursorPhase.CANONICAL:
                rows, more = await self._keyset(
                    session,
                    CanonicalPersonModel,
                    CanonicalPersonModel.id,
                    key,
                    request.limit + 1,
                )
                counts = await self._binding_counts(session, [row.id for row in rows])
                items.extend(
                    PersonView(
                        person_id=PersonId.parse(row.id),
                        resolution=IdentityResolution.CANONICAL,
                        enabled=bool(row.enabled),
                        revision=int(row.revision),
                        created_at=_as_aware(row.created_at),
                        updated_at=_as_aware(row.updated_at),
                        binding_count=counts.get(row.id, 0),
                    )
                    for row in rows
                )
                if more:
                    return self._page(
                        items,
                        kind=QueryResourceKind.PERSON,
                        phase=QueryCursorPhase.CANONICAL,
                        next_key=rows[-1].id,
                        snapshot_at=snapshot_at,
                    )
            if epoch is StateEpoch.V2:
                return self._page(
                    items,
                    kind=QueryResourceKind.PERSON,
                    phase=QueryCursorPhase.CANONICAL,
                    next_key=None,
                    snapshot_at=snapshot_at,
                )
            remaining = request.limit - len(items)
            if remaining <= 0:
                peek, _more = await self._unresolved_people(session, None, 1)
                return self._page(
                    items,
                    kind=QueryResourceKind.PERSON,
                    phase=QueryCursorPhase.UNRESOLVED,
                    next_key="0" if peek else None,
                    snapshot_at=snapshot_at,
                )
            after = _unresolved_after(phase, key)
            unresolved, more = await self._unresolved_people(session, after, remaining + 1)
            items.extend(
                PersonView(
                    person_id=None,
                    resolution=IdentityResolution.UNRESOLVED,
                    enabled=bool(row.enabled),
                    revision=None,
                    created_at=_as_aware(row.first_seen_at),
                    updated_at=_as_aware(row.last_seen_at),
                    binding_count=0,
                )
                for row, _rowid in unresolved
            )
            next_key = str(unresolved[-1][1]) if more and unresolved else None
            return self._page(
                items,
                kind=QueryResourceKind.PERSON,
                phase=QueryCursorPhase.UNRESOLVED,
                next_key=next_key,
                snapshot_at=snapshot_at,
            )

    async def list_identity_bindings(
        self,
        request: PageRequest,
        *,
        reveal_external: bool,
    ) -> Page[IdentityBindingView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            phase, key = self._cursor_state(request, QueryResourceKind.BINDING, epoch=epoch)
            items: list[IdentityBindingView] = []
            if phase is QueryCursorPhase.CANONICAL:
                rows, more = await self._keyset(
                    session,
                    IdentityBindingModel,
                    IdentityBindingModel.id,
                    key,
                    request.limit + 1,
                )
                items.extend(
                    IdentityBindingView(
                        binding_id=IdentityBindingId.parse(row.id),
                        person_id=PersonId.parse(row.person_id),
                        resolution=IdentityResolution.CANONICAL,
                        platform=row.platform,
                        external=mask_external_id(row.external_account_id, reveal=reveal_external),
                        display_name=sanitize_projected_display(
                            row.display_name,
                            external_ids=(row.external_account_id,),
                            reveal=reveal_external,
                        ),
                        status=row.status,
                        revision=int(row.revision),
                    )
                    for row in rows
                )
                if more:
                    return self._page(
                        items,
                        kind=QueryResourceKind.BINDING,
                        phase=QueryCursorPhase.CANONICAL,
                        next_key=rows[-1].id,
                        snapshot_at=snapshot_at,
                    )
            if epoch is StateEpoch.V2:
                return self._page(
                    items,
                    kind=QueryResourceKind.BINDING,
                    phase=QueryCursorPhase.CANONICAL,
                    next_key=None,
                    snapshot_at=snapshot_at,
                )
            remaining = request.limit - len(items)
            if remaining <= 0:
                peek, _more = await self._unresolved_people(session, None, 1)
                return self._page(
                    items,
                    kind=QueryResourceKind.BINDING,
                    phase=QueryCursorPhase.UNRESOLVED,
                    next_key="0" if peek else None,
                    snapshot_at=snapshot_at,
                )
            after = _unresolved_after(phase, key)
            unresolved, more = await self._unresolved_people(session, after, remaining + 1)
            items.extend(
                IdentityBindingView(
                    binding_id=None,
                    person_id=None,
                    resolution=IdentityResolution.UNRESOLVED,
                    platform=IDENTITY_PLATFORM,
                    external=mask_external_id(row.user_id, reveal=reveal_external),
                    display_name=sanitize_projected_display(
                        row.nickname,
                        external_ids=(row.user_id,),
                        reveal=reveal_external,
                    ),
                    status="active" if row.enabled else "disabled",
                    revision=None,
                )
                for row, _rowid in unresolved
            )
            next_key = str(unresolved[-1][1]) if more and unresolved else None
            return self._page(
                items,
                kind=QueryResourceKind.BINDING,
                phase=QueryCursorPhase.UNRESOLVED,
                next_key=next_key,
                snapshot_at=snapshot_at,
            )

    async def list_spaces(
        self,
        request: PageRequest,
        *,
        reveal_external: bool,
    ) -> Page[SpaceView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            phase, key = self._cursor_state(request, QueryResourceKind.SPACE, epoch=epoch)
            items: list[SpaceView] = []
            if phase is QueryCursorPhase.CANONICAL:
                rows, more = await self._keyset(
                    session,
                    CanonicalSpaceModel,
                    CanonicalSpaceModel.id,
                    key,
                    request.limit + 1,
                )
                space_ids = [row.id for row in rows]
                counts = await self._space_binding_counts(session, space_ids)
                externals = (
                    {} if reveal_external else await self._space_external_ids(session, space_ids)
                )
                items.extend(
                    SpaceView(
                        space_id=SpaceId.parse(row.id),
                        resolution=IdentityResolution.CANONICAL,
                        name=sanitize_projected_display(
                            row.name,
                            external_ids=externals.get(row.id, ()),
                            reveal=reveal_external,
                        ),
                        enabled=bool(row.enabled),
                        autonomous_enabled=bool(row.autonomous_enabled),
                        require_mention=bool(row.require_mention),
                        revision=int(row.revision),
                        binding_count=counts.get(row.id, 0),
                    )
                    for row in rows
                )
                if more:
                    return self._page(
                        items,
                        kind=QueryResourceKind.SPACE,
                        phase=QueryCursorPhase.CANONICAL,
                        next_key=rows[-1].id,
                        snapshot_at=snapshot_at,
                    )
            if epoch is StateEpoch.V2:
                return self._page(
                    items,
                    kind=QueryResourceKind.SPACE,
                    phase=QueryCursorPhase.CANONICAL,
                    next_key=None,
                    snapshot_at=snapshot_at,
                )
            remaining = request.limit - len(items)
            if remaining <= 0:
                peek, _more = await self._unresolved_groups(session, None, 1)
                return self._page(
                    items,
                    kind=QueryResourceKind.SPACE,
                    phase=QueryCursorPhase.UNRESOLVED,
                    next_key="0" if peek else None,
                    snapshot_at=snapshot_at,
                )
            after = _unresolved_after(phase, key)
            unresolved, more = await self._unresolved_groups(session, after, remaining + 1)
            items.extend(
                SpaceView(
                    space_id=None,
                    resolution=IdentityResolution.UNRESOLVED,
                    name=sanitize_projected_display(
                        row.name,
                        external_ids=(row.group_id,),
                        reveal=reveal_external,
                    ),
                    enabled=bool(row.enabled),
                    autonomous_enabled=bool(row.autonomous_enabled),
                    require_mention=bool(row.require_mention),
                    revision=None,
                    binding_count=0,
                )
                for row, _rowid in unresolved
            )
            next_key = str(unresolved[-1][1]) if more and unresolved else None
            return self._page(
                items,
                kind=QueryResourceKind.SPACE,
                phase=QueryCursorPhase.UNRESOLVED,
                next_key=next_key,
                snapshot_at=snapshot_at,
            )

    async def list_space_bindings(
        self,
        request: PageRequest,
        *,
        reveal_external: bool,
    ) -> Page[SpaceBindingView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            phase, key = self._cursor_state(request, QueryResourceKind.SPACE_BINDING, epoch=epoch)
            items: list[SpaceBindingView] = []
            if phase is QueryCursorPhase.CANONICAL:
                rows, more = await self._keyset(
                    session,
                    SpaceBindingModel,
                    SpaceBindingModel.id,
                    key,
                    request.limit + 1,
                )
                items.extend(
                    SpaceBindingView(
                        binding_id=SpaceBindingId.parse(row.id),
                        space_id=SpaceId.parse(row.space_id),
                        resolution=IdentityResolution.CANONICAL,
                        platform=row.platform,
                        external=mask_external_id(row.external_space_id, reveal=reveal_external),
                        display_name=sanitize_projected_display(
                            row.display_name,
                            external_ids=(row.external_space_id,),
                            reveal=reveal_external,
                        ),
                        status=row.status,
                        revision=int(row.revision),
                    )
                    for row in rows
                )
                if more:
                    return self._page(
                        items,
                        kind=QueryResourceKind.SPACE_BINDING,
                        phase=QueryCursorPhase.CANONICAL,
                        next_key=rows[-1].id,
                        snapshot_at=snapshot_at,
                    )
            if epoch is StateEpoch.V2:
                return self._page(
                    items,
                    kind=QueryResourceKind.SPACE_BINDING,
                    phase=QueryCursorPhase.CANONICAL,
                    next_key=None,
                    snapshot_at=snapshot_at,
                )
            remaining = request.limit - len(items)
            if remaining <= 0:
                peek, _more = await self._unresolved_groups(session, None, 1)
                return self._page(
                    items,
                    kind=QueryResourceKind.SPACE_BINDING,
                    phase=QueryCursorPhase.UNRESOLVED,
                    next_key="0" if peek else None,
                    snapshot_at=snapshot_at,
                )
            after = _unresolved_after(phase, key)
            unresolved, more = await self._unresolved_groups(session, after, remaining + 1)
            items.extend(
                SpaceBindingView(
                    binding_id=None,
                    space_id=None,
                    resolution=IdentityResolution.UNRESOLVED,
                    platform=IDENTITY_PLATFORM,
                    external=mask_external_id(row.group_id, reveal=reveal_external),
                    display_name=sanitize_projected_display(
                        row.name,
                        external_ids=(row.group_id,),
                        reveal=reveal_external,
                    ),
                    status="active" if row.enabled else "disabled",
                    revision=None,
                )
                for row, _rowid in unresolved
            )
            next_key = str(unresolved[-1][1]) if more and unresolved else None
            return self._page(
                items,
                kind=QueryResourceKind.SPACE_BINDING,
                phase=QueryCursorPhase.UNRESOLVED,
                next_key=next_key,
                snapshot_at=snapshot_at,
            )

    async def list_presences(
        self,
        request: PageRequest,
        *,
        reveal_external: bool,
    ) -> Page[PresenceView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            phase, key = self._cursor_state(request, QueryResourceKind.PRESENCE, epoch=epoch)
            if phase is not QueryCursorPhase.CANONICAL:
                raise ControlQueryError(Problem(ProblemCode.VALIDATION_ERROR))
            rows, more = await self._keyset(
                session,
                PresenceModel,
                PresenceModel.id,
                key,
                request.limit + 1,
            )
            items = [
                PresenceView(
                    presence_id=PresenceId.parse(row.id),
                    platform=row.platform,
                    external=mask_external_id(row.external_account_id, reveal=reveal_external),
                    enabled=bool(row.enabled),
                    ingest_eligible=bool(row.ingest_eligible),
                    revision=int(row.revision),
                    connection_state=PresenceConnectionState.UNAVAILABLE,
                    connection_problem=Problem(ProblemCode.OPERATION_UNAVAILABLE),
                )
                for row in rows
            ]
            return self._page(
                items,
                kind=QueryResourceKind.PRESENCE,
                phase=QueryCursorPhase.CANONICAL,
                next_key=rows[-1].id if more else None,
                snapshot_at=snapshot_at,
            )

    async def list_conversations(self, request: PageRequest) -> Page[ConversationView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            phase, key = self._cursor_state(request, QueryResourceKind.CONVERSATION, epoch=epoch)
            items: list[ConversationView] = []
            if phase is QueryCursorPhase.CANONICAL:
                rows, more = await self._keyset(
                    session,
                    CanonicalConversationModel,
                    CanonicalConversationModel.id,
                    key,
                    request.limit + 1,
                )
                items.extend(
                    ConversationView(
                        conversation_id=ConversationId.parse(row.id),
                        resolution=IdentityResolution.CANONICAL,
                        kind=row.kind,
                        person_id=None if row.person_id is None else PersonId.parse(row.person_id),
                        space_id=None if row.space_id is None else SpaceId.parse(row.space_id),
                        generation=ConversationGeneration(int(row.generation)),
                        last_event_id=int(row.last_event_id),
                        starts_after_event_id=int(row.starts_after_event_id),
                        covered_through_event_id=int(row.covered_through_event_id),
                        last_generation_change_event_id=int(row.last_generation_change_event_id),
                        uncovered_event_count=int(row.uncovered_event_count),
                        revision=int(row.revision),
                    )
                    for row in rows
                )
                if more:
                    return self._page(
                        items,
                        kind=QueryResourceKind.CONVERSATION,
                        phase=QueryCursorPhase.CANONICAL,
                        next_key=rows[-1].id,
                        snapshot_at=snapshot_at,
                    )
            if epoch is StateEpoch.V2:
                return self._page(
                    items,
                    kind=QueryResourceKind.CONVERSATION,
                    phase=QueryCursorPhase.CANONICAL,
                    next_key=None,
                    snapshot_at=snapshot_at,
                )
            remaining = request.limit - len(items)
            if remaining <= 0:
                peek_stmt = (
                    select(ConversationScopeModel.id)
                    .where(ConversationScopeModel.canonical_conversation_id.is_(None))
                    .order_by(ConversationScopeModel.id.asc())
                    .limit(1)
                )
                peek = await session.scalar(peek_stmt)
                return self._page(
                    items,
                    kind=QueryResourceKind.CONVERSATION,
                    phase=QueryCursorPhase.UNRESOLVED,
                    next_key="0" if peek is not None else None,
                    snapshot_at=snapshot_at,
                )
            after = (
                decode_integer_cursor_key(key, minimum=0)
                if phase is QueryCursorPhase.UNRESOLVED and key is not None
                else 0
            )
            stmt = select(ConversationScopeModel).where(
                ConversationScopeModel.canonical_conversation_id.is_(None)
            )
            if after:
                stmt = stmt.where(ConversationScopeModel.id > after)
            stmt = stmt.order_by(ConversationScopeModel.id.asc()).limit(remaining + 1)
            unresolved = list(await session.scalars(stmt))
            more = len(unresolved) == remaining + 1
            if more:
                unresolved = unresolved[:-1]
            items.extend(
                ConversationView(
                    conversation_id=None,
                    resolution=IdentityResolution.UNRESOLVED,
                    kind=row.scope_type,
                    person_id=None,
                    space_id=None,
                    generation=ConversationGeneration(int(row.generation)),
                    last_event_id=int(row.last_event_id),
                    starts_after_event_id=int(row.starts_after_event_id),
                    covered_through_event_id=None,
                    last_generation_change_event_id=int(row.last_generation_change_event_id),
                    uncovered_event_count=int(row.uncovered_event_count),
                    revision=None,
                )
                for row in unresolved
            )
            next_key = str(unresolved[-1].id) if more and unresolved else None
            return self._page(
                items,
                kind=QueryResourceKind.CONVERSATION,
                phase=QueryCursorPhase.UNRESOLVED,
                next_key=next_key,
                snapshot_at=snapshot_at,
            )

    async def list_person_active_routes(self, request: PageRequest) -> Page[PersonActiveRouteView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            _phase, key = self._cursor_state(request, QueryResourceKind.PERSON_ROUTE, epoch=epoch)
            rows, more = await self._keyset(
                session,
                PersonActiveRouteModel,
                PersonActiveRouteModel.person_id,
                key,
                request.limit + 1,
            )
            binding_ids = [row.identity_binding_id for row in rows]
            presence_ids = [row.presence_id for row in rows]
            bindings = await self._load_by_ids(
                session, IdentityBindingModel, IdentityBindingModel.id, binding_ids
            )
            presences = await self._load_by_ids(
                session, PresenceModel, PresenceModel.id, presence_ids
            )
            items = []
            for row in rows:
                binding = bindings.get(row.identity_binding_id)
                presence = presences.get(row.presence_id)
                items.append(
                    PersonActiveRouteView(
                        kind=RouteKind.PERSON_ACTIVE,
                        person_id=PersonId.parse(row.person_id),
                        identity_binding_id=IdentityBindingId.parse(row.identity_binding_id),
                        presence_id=PresenceId.parse(row.presence_id),
                        route_generation=RouteGeneration(int(row.route_generation)),
                        paused=bool(row.paused),
                        revision=int(row.revision),
                        reference_state=classify_route_reference(
                            expected_owner_id=row.person_id,
                            actual_owner_id=None if binding is None else binding.person_id,
                            binding_platform=None if binding is None else binding.platform,
                            presence_platform=None if presence is None else presence.platform,
                        ),
                    )
                )
            return self._page(
                items,
                kind=QueryResourceKind.PERSON_ROUTE,
                phase=QueryCursorPhase.CANONICAL,
                next_key=rows[-1].person_id if more else None,
                snapshot_at=snapshot_at,
            )

    async def list_space_binding_ingest_routes(
        self, request: PageRequest
    ) -> Page[SpaceBindingIngestRouteView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            _phase, key = self._cursor_state(request, QueryResourceKind.INGEST_ROUTE, epoch=epoch)
            rows, more = await self._keyset(
                session,
                SpaceBindingIngestRouteModel,
                SpaceBindingIngestRouteModel.space_binding_id,
                key,
                request.limit + 1,
            )
            binding_ids = [row.space_binding_id for row in rows]
            presence_ids = [row.ingest_presence_id for row in rows]
            bindings = await self._load_by_ids(
                session, SpaceBindingModel, SpaceBindingModel.id, binding_ids
            )
            presences = await self._load_by_ids(
                session, PresenceModel, PresenceModel.id, presence_ids
            )
            items = []
            for row in rows:
                binding = bindings.get(row.space_binding_id)
                presence = presences.get(row.ingest_presence_id)
                items.append(
                    SpaceBindingIngestRouteView(
                        kind=RouteKind.SPACE_BINDING_INGEST,
                        space_binding_id=SpaceBindingId.parse(row.space_binding_id),
                        ingest_presence_id=PresenceId.parse(row.ingest_presence_id),
                        route_generation=RouteGeneration(int(row.route_generation)),
                        paused=bool(row.paused),
                        revision=int(row.revision),
                        reference_state=classify_route_reference(
                            expected_owner_id=None,
                            actual_owner_id=None if binding is None else binding.id,
                            binding_platform=None if binding is None else binding.platform,
                            presence_platform=None if presence is None else presence.platform,
                        ),
                    )
                )
            return self._page(
                items,
                kind=QueryResourceKind.INGEST_ROUTE,
                phase=QueryCursorPhase.CANONICAL,
                next_key=rows[-1].space_binding_id if more else None,
                snapshot_at=snapshot_at,
            )

    async def list_space_active_routes(self, request: PageRequest) -> Page[SpaceActiveRouteView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            _phase, key = self._cursor_state(request, QueryResourceKind.SPACE_ROUTE, epoch=epoch)
            rows, more = await self._keyset(
                session,
                SpaceActiveRouteModel,
                SpaceActiveRouteModel.space_id,
                key,
                request.limit + 1,
            )
            binding_ids = [row.space_binding_id for row in rows]
            presence_ids = [row.presence_id for row in rows]
            bindings = await self._load_by_ids(
                session, SpaceBindingModel, SpaceBindingModel.id, binding_ids
            )
            presences = await self._load_by_ids(
                session, PresenceModel, PresenceModel.id, presence_ids
            )
            items = []
            for row in rows:
                binding = bindings.get(row.space_binding_id)
                presence = presences.get(row.presence_id)
                items.append(
                    SpaceActiveRouteView(
                        kind=RouteKind.SPACE_ACTIVE,
                        space_id=SpaceId.parse(row.space_id),
                        space_binding_id=SpaceBindingId.parse(row.space_binding_id),
                        presence_id=PresenceId.parse(row.presence_id),
                        route_generation=RouteGeneration(int(row.route_generation)),
                        paused=bool(row.paused),
                        revision=int(row.revision),
                        reference_state=classify_route_reference(
                            expected_owner_id=row.space_id,
                            actual_owner_id=None if binding is None else binding.space_id,
                            binding_platform=None if binding is None else binding.platform,
                            presence_platform=None if presence is None else presence.platform,
                        ),
                    )
                )
            return self._page(
                items,
                kind=QueryResourceKind.SPACE_ROUTE,
                phase=QueryCursorPhase.CANONICAL,
                next_key=rows[-1].space_id if more else None,
                snapshot_at=snapshot_at,
            )

    async def list_audit_events(self, request: PageRequest) -> Page[AuditEventView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            _phase, key = self._cursor_state(request, QueryResourceKind.AUDIT, epoch=epoch)
            stmt = select(
                AdminOperationEventModel.id,
                AdminOperationEventModel.capability,
                AdminOperationEventModel.operation,
                AdminOperationEventModel.target_type,
                AdminOperationEventModel.success,
                AdminOperationEventModel.error_category,
                AdminOperationEventModel.duration_seconds,
                AdminOperationEventModel.created_at,
            )
            if key is not None:
                created_at, row_id = decode_time_id_key(key)
                stmt = stmt.where(
                    tuple_(AdminOperationEventModel.created_at, AdminOperationEventModel.id)
                    > (created_at, row_id)
                )
            stmt = stmt.order_by(
                AdminOperationEventModel.created_at.asc(),
                AdminOperationEventModel.id.asc(),
            ).limit(request.limit + 1)
            rows = list(await session.execute(stmt))
            more = len(rows) == request.limit + 1
            if more:
                rows = rows[:-1]
            items = [project_audit_event(row, snapshot_at=snapshot_at) for row in rows]
            next_key = None
            if more and rows:
                last = rows[-1]
                try:
                    next_key = encode_time_id_key(
                        _as_aware(last.created_at) or snapshot_at, int(last.id)
                    )
                except (TypeError, ValueError) as exc:
                    raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH)) from exc
            return self._page(
                items,
                kind=QueryResourceKind.AUDIT,
                phase=QueryCursorPhase.TIME_ID,
                next_key=next_key,
                snapshot_at=snapshot_at,
            )

    def _operation_from_run(self, row: IdentityBackfillRunModel) -> BackfillOperationView:
        status = _BACKFILL_STATUS.get(row.status)
        if status is None:
            raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH))
        error_category = None
        if status is OperationStatus.FAILED:
            error_category = _safe_token(row.error_category, fallback="", max_length=64)
            if not error_category:
                raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH))
        elif row.error_category is not None:
            raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH))
        created = _as_aware(row.created_at)
        updated = _as_aware(row.updated_at)
        if created is None or updated is None:
            raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH))
        try:
            return BackfillOperationView(
                operation=OperationRef(
                    operation_id=f"backfill-{row.id}",
                    status=status,
                    progress=_backfill_progress(
                        row.status,
                        int(row.processed_count),
                        int(row.skipped_count),
                        int(row.conflicts_count),
                    ),
                    state_epoch=StateEpoch.V1,
                    error_category=error_category,
                    created_at=created,
                    updated_at=updated,
                ),
                mode=_safe_token(row.mode, fallback="dry_run", max_length=16),
                conflict_count=int(row.conflicts_count),
            )
        except (TypeError, ValueError) as exc:
            raise ControlQueryError(Problem(ProblemCode.STATE_MISMATCH)) from exc

    async def list_backfill_operations(self, request: PageRequest) -> Page[BackfillOperationView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            _phase, key = self._cursor_state(request, QueryResourceKind.OPERATION, epoch=epoch)
            after = decode_integer_cursor_key(key, minimum=1) if key is not None else 0
            stmt = select(IdentityBackfillRunModel)
            if after:
                stmt = stmt.where(IdentityBackfillRunModel.id > after)
            stmt = stmt.order_by(IdentityBackfillRunModel.id.asc()).limit(request.limit + 1)
            rows = list(await session.scalars(stmt))
            more = len(rows) == request.limit + 1
            if more:
                rows = rows[:-1]
            items = [self._operation_from_run(row) for row in rows]
            return self._page(
                items,
                kind=QueryResourceKind.OPERATION,
                phase=QueryCursorPhase.CANONICAL,
                next_key=str(rows[-1].id) if more else None,
                snapshot_at=snapshot_at,
            )

    async def list_backfill_conflicts(self, request: PageRequest) -> Page[BackfillConflictView]:
        snapshot_at = _now()
        async with self._reader() as session:
            epoch, _revision = await self._runtime(session)
            _phase, key = self._cursor_state(request, QueryResourceKind.CONFLICT, epoch=epoch)
            after = decode_integer_cursor_key(key, minimum=1) if key is not None else 0
            stmt = select(
                IdentityConflictModel.id,
                IdentityConflictModel.subject_kind,
                IdentityConflictModel.conflict_kind,
                IdentityConflictModel.status,
                IdentityConflictModel.error_category,
            )
            if after:
                stmt = stmt.where(IdentityConflictModel.id > after)
            stmt = stmt.order_by(IdentityConflictModel.id.asc()).limit(request.limit + 1)
            rows = list(await session.execute(stmt))
            more = len(rows) == request.limit + 1
            if more:
                rows = rows[:-1]
            items = [_project_conflict(row) for row in rows]
            return self._page(
                items,
                kind=QueryResourceKind.CONFLICT,
                phase=QueryCursorPhase.CANONICAL,
                next_key=str(rows[-1].id) if more else None,
                snapshot_at=snapshot_at,
            )
