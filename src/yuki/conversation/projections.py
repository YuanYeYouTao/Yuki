"""Atomic bounded current model-input chunks, never delivery facts."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Set
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import Select, and_, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.observation_models import ContextSelectionModel
from yuki.conversation.projection_models import PromptProjectionChunkModel, PromptProjectionModel
from yuki.execution_trace.phases import collect_phase_metrics, model_detail
from yuki.persistence.database import Database

if TYPE_CHECKING:
    from yuki.conversation.frozen_fragments import FrozenFragments
    from yuki.conversation.prompt_history import PromptHistorySource

SUMMARY_RENDERER_VERSION = 2
REBUILD_REASONS = frozenset(
    {"bootstrap", "reset", "compact", "capacity", "deleted_event", "source_changed"}
)


class ProjectionConflict(ValueError):
    """Obtain a fresh source/snapshot before preparing new input."""


class ProjectionCapacityError(ValueError):
    """Compaction or cleanup must establish an explicit context boundary."""


def current_checkpoint_query(conversation_id: str) -> Select[tuple[PromptProjectionModel]]:
    """A checkpoint survives eviction of its recyclable raw chunks."""
    return (
        select(PromptProjectionModel)
        .join(CanonicalConversationModel)
        .where(
            PromptProjectionModel.view_key == conversation_id,
            PromptProjectionModel.generation == CanonicalConversationModel.generation,
            PromptProjectionModel.source_revision
            == CanonicalConversationModel.prompt_source_revision,
            PromptProjectionModel.starts_after_event_id
            == CanonicalConversationModel.starts_after_event_id,
            PromptProjectionModel.selected_summary_text.is_not(None),
            func.trim(PromptProjectionModel.selected_summary_text) != "",
            PromptProjectionModel.selected_summary_coverage
            >= CanonicalConversationModel.starts_after_event_id,
            PromptProjectionModel.selected_summary_coverage
            <= CanonicalConversationModel.last_event_id,
        )
    )


@dataclass(frozen=True, slots=True)
class ProjectionSnapshot:
    epoch_id: str
    revision: int
    generation: int
    context_key: str
    fragments: FrozenFragments
    rebuild_reason: str
    source_revision: int
    selected_summary_text: str | None = None
    selected_summary_coverage: int = 0
    _prefix_stamp: tuple[object, ...] | None = field(default=None, repr=False, compare=False)
    _prefix_origin: tuple[str, object, tuple[object, ...]] | None = field(
        default=None, repr=False, compare=False
    )
    selected_summary_kind: str | None = None
    selected_summary_renderer: int | None = None
    tail_ordinal: int = 0
    byte_size: int = 2

    def items(self) -> list[dict[str, Any]]:
        """Mutable DTO export at journal/external boundaries only."""
        return list(self.fragments.items)

    @property
    def payload_json(self) -> str:
        return json.dumps(self.items(), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, slots=True)
class ProjectionPublication:
    publish: Callable[[AsyncSession], Awaitable[ProjectionSnapshot]]

    async def __call__(self, session: AsyncSession) -> ProjectionSnapshot:
        return await self.publish(session)


@dataclass(slots=True)
class _CachedView:
    snapshot: ProjectionSnapshot | None = None
    source: PromptHistorySource | None = None

    @property
    def byte_size(self) -> int:
        return (self.snapshot.byte_size if self.snapshot else 0) + (
            self.source.estimated_bytes if self.source else 0
        )


class PromptProjectionRepository:
    def __init__(
        self,
        database: Database,
        *,
        max_view_bytes: int = 8 * 1024 * 1024,
        total_bytes: int = 16 * 1024 * 1024,
        maximum_views: int = 128,
    ) -> None:
        if min(max_view_bytes, total_bytes, maximum_views) <= 0:
            raise ValueError("invalid projection budget")
        self.database = database
        self.view_bytes = min(max_view_bytes, total_bytes)
        self.total_bytes, self.maximum_views = total_bytes, maximum_views
        self._views: OrderedDict[str, _CachedView] = OrderedDict()

    def history_source(self, view_key: str) -> PromptHistorySource | None:
        entry = self._views.get(view_key)
        if entry is not None:
            self._views.move_to_end(view_key)
        return entry.source if entry else None

    def remember_history_source(self, view_key: str, source: PromptHistorySource) -> None:
        entry = self._views.setdefault(view_key, _CachedView())
        entry.source = source
        self._views.move_to_end(view_key)
        self._trim_views()

    def _trim_views(self) -> None:
        # Both derived representations share the original view/LRU budgets.
        # Oversized source metadata is recyclable; it never gates execution.
        for entry in self._views.values():
            if entry.source is not None and (
                not entry.source.reconcile(entry.snapshot) or entry.byte_size > self.view_bytes
            ):
                entry.source = None
        used = sum(entry.byte_size for entry in self._views.values())
        while len(self._views) > self.maximum_views or used > self.total_bytes:
            _, retired = self._views.popitem(last=False)
            used -= retired.byte_size

    async def read(
        self, view_key: str, *, session: AsyncSession | None = None
    ) -> ProjectionSnapshot | None:
        if session is None:
            async with self.database.read_session() as reader:
                await reader.execute(text("BEGIN"))
                return await self.read(view_key, session=reader)
        row = await session.scalar(
            select(PromptProjectionModel)
            .join(CanonicalConversationModel)
            .where(
                PromptProjectionModel.view_key == view_key,
                PromptProjectionModel.invalidated_reason.is_(None),
                PromptProjectionModel.generation == CanonicalConversationModel.generation,
                PromptProjectionModel.source_revision
                == CanonicalConversationModel.prompt_source_revision,
                PromptProjectionModel.starts_after_event_id
                == CanonicalConversationModel.starts_after_event_id,
            )
        )
        if row is None:
            entry = self._views.get(view_key)
            if entry is not None:
                if entry.source is not None and entry.source.reconcile(None):
                    entry.snapshot = None
                else:
                    self._views.pop(view_key)
            return None
        return await self.read_row(session, row)

    async def read_row(
        self, session: AsyncSession, row: PromptProjectionModel
    ) -> ProjectionSnapshot:
        from yuki.conversation.frozen_fragments import FrozenFragments

        entry = self._views.get(row.view_key)
        previous = entry.snapshot if entry else None
        stamp = _prefix_version(row)
        if previous is not None and previous._prefix_stamp == stamp:
            self._views.move_to_end(row.view_key)
            return previous
        same_epoch = (
            previous is not None
            and previous.epoch_id == row.epoch_id
            and previous.tail_ordinal <= row.tail_ordinal
            and previous.source_revision == row.source_revision
            and previous.generation == row.generation
        )
        fragments = previous.fragments if same_epoch and previous else FrozenFragments.load([])
        after = previous.tail_ordinal if same_epoch and previous else 0
        if row.invalidated_reason is None:
            payloads = await session.scalars(
                select(PromptProjectionChunkModel.payload_json)
                .where(
                    PromptProjectionChunkModel.view_key == row.view_key,
                    PromptProjectionChunkModel.epoch_id == row.epoch_id,
                    PromptProjectionChunkModel.ordinal > after,
                    PromptProjectionChunkModel.ordinal <= row.tail_ordinal,
                )
                .order_by(PromptProjectionChunkModel.ordinal)
            )
            for payload in payloads:
                block = FrozenFragments.load(json.loads(payload))
                fragments = fragments.extend_blocks((tuple(block.iter_fragments()),))
            if len(fragments) != row.item_count:
                raise ProjectionConflict("current projection chunks are incomplete")
        else:
            fragments = FrozenFragments.load([])
        snapshot = _snapshot(row, fragments)
        if row.invalidated_reason is None:
            entry = self._views.setdefault(row.view_key, _CachedView())
            entry.snapshot = snapshot
            self._views.move_to_end(row.view_key)
            self._trim_views()
        return snapshot

    async def invalidation_reason(self, view_key: str) -> str | None:
        async with self.database.read_session() as session:
            return await session.scalar(
                select(PromptProjectionModel.invalidated_reason).where(
                    PromptProjectionModel.view_key == view_key
                )
            )

    async def read_checkpoint(self, conversation_id: str) -> ProjectionSnapshot | None:
        async with self.database.read_session() as session:
            await session.execute(text("BEGIN"))
            row = await session.scalar(current_checkpoint_query(conversation_id))
            return await self.read_row(session, row) if row else None

    async def prepare_commit(
        self,
        *,
        view_key: str,
        conversation_id: str,
        generation: int,
        expected_source_revision: int,
        starts_after_event_id: int,
        context_key: str,
        fragments: FrozenFragments,
        expected_epoch: str | None = None,
        expected_revision: int = 0,
        rebuild_reason: str | None = None,
        selected_summary_text: str | None = None,
        selected_summary_coverage: int = 0,
        selected_summary_kind: str | None = None,
        selected_summary_renderer: int | None = None,
        previous_snapshot: ProjectionSnapshot | None = None,
        write: bool = True,
    ) -> ProjectionPublication:
        """Prepare new immutable batches; publish under the original writer CAS."""
        if rebuild_reason is not None and rebuild_reason not in REBUILD_REASONS:
            raise ValueError("invalid projection rebuild reason")
        if rebuild_reason is None:
            previous_snapshot = previous_snapshot or await self.read(view_key)
            if previous_snapshot is None:
                raise ProjectionConflict("missing projection requires explicit bootstrap")
            origin = previous_snapshot._prefix_origin
            if (
                origin is None
                or origin[0] != view_key
                or origin[1] is not previous_snapshot.fragments
                or origin[2] is not previous_snapshot._prefix_stamp
            ):
                raise ProjectionConflict("projection snapshot origin changed")
            blocks = fragments.delta_from(previous_snapshot.fragments)
            size, count, tail = (
                previous_snapshot.byte_size,
                len(previous_snapshot.fragments),
                previous_snapshot.tail_ordinal,
            )
        else:
            blocks = tuple(fragments.iter_blocks())
            size, count, tail = 2, 0, 0
        encoded, additions = [], []
        for block in blocks:
            items = [fragment.item() for fragment in block]
            if not items:
                continue
            payload = json.dumps(items, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            size += len(payload.encode("utf-8")) - 2 + (1 if count else 0)
            count += len(items)
            encoded.append(payload)
            additions.extend(items)
        collect_phase_metrics(item_count=count, payload_bytes=size)
        if size > self.view_bytes:
            raise ProjectionCapacityError("projection view budget exceeded")
        from yuki.conversation.observations import privacy_generation

        prepared_sources = []
        with model_detail("projection_sources_read"):
            async with self.database.read_session() as reader:
                await reader.execute(text("BEGIN"))
                prepared_privacy = await privacy_generation(reader)
                owner = await reader.get(CanonicalConversationModel, conversation_id)
                if owner is None or (
                    owner.generation,
                    owner.starts_after_event_id,
                    owner.prompt_source_revision,
                ) != (generation, starts_after_event_id, expected_source_revision):
                    raise ProjectionConflict("projection source changed")
                candidates: dict[str, dict[str, Any]] = {}
                for item in additions:
                    key = _selection_key(item)
                    if key in candidates and _encode_item(candidates[key]) != _encode_item(item):
                        raise ProjectionConflict("selected representation identity conflict")
                    candidates[key] = item
                keys = list(candidates)
                existing: dict[str, Any] = {}
                for offset in range(0, len(keys), 256):
                    rows = await reader.execute(
                        select(
                            ContextSelectionModel.source_key,
                            ContextSelectionModel.conversation_id,
                            ContextSelectionModel.generation,
                            ContextSelectionModel.event_ids_json,
                            ContextSelectionModel.payload_json,
                        ).where(
                            ContextSelectionModel.view_key == view_key,
                            ContextSelectionModel.source_key.in_(keys[offset : offset + 256]),
                        )
                    )
                    existing.update((row.source_key, row) for row in rows)
                for key, item in candidates.items():
                    row = existing.get(key)
                    ids = item["event_ids"]
                    if row is not None:
                        if (row.conversation_id, row.generation, row.event_ids_json) != (
                            conversation_id,
                            generation,
                            json.dumps(ids),
                        ):
                            raise ProjectionConflict("selected representation owner changed")
                        if rebuild_reason is None and row.payload_json != _encode_item(item):
                            raise ProjectionConflict("selected representation is immutable")
                        continue
                    prepared_sources.append(
                        dict(
                            view_key=view_key,
                            conversation_id=conversation_id,
                            generation=generation,
                            source_key=key,
                            event_ids_json=json.dumps(ids),
                            min_event_id=min(ids, default=0),
                            max_event_id=max(ids, default=0),
                            payload_json=_encode_item(item),
                            created_at=datetime.now(UTC),
                        )
                    )

        existing_sources = json.dumps(
            [
                (key, row.conversation_id, row.generation, row.event_ids_json, row.payload_json)
                for key, row in sorted(existing.items())
            ]
        )
        existing_keys = tuple(sorted(existing))

        async def publish(session: AsyncSession) -> ProjectionSnapshot:
            source = await session.get(CanonicalConversationModel, conversation_id)
            if source is None or (
                source.generation,
                source.starts_after_event_id,
                source.prompt_source_revision,
            ) != (generation, starts_after_event_id, expected_source_revision):
                raise ProjectionConflict("projection source changed")
            if await privacy_generation(session) != prepared_privacy:
                raise ProjectionConflict("projection privacy changed")
            if existing_keys:
                wanted = func.json_each(existing_sources).table_valued("value")
                selected = ContextSelectionModel
                current_sources = tuple(
                    await session.execute(
                        select(
                            selected.source_key,
                            and_(
                                selected.conversation_id.is_(
                                    func.json_extract(wanted.c.value, "$[1]")
                                ),
                                selected.generation.is_(func.json_extract(wanted.c.value, "$[2]")),
                                selected.event_ids_json.is_(
                                    func.json_extract(wanted.c.value, "$[3]")
                                ),
                                selected.payload_json.is_(
                                    func.json_extract(wanted.c.value, "$[4]")
                                ),
                            ),
                        )
                        .select_from(selected)
                        .join(
                            wanted, selected.source_key == func.json_extract(wanted.c.value, "$[0]")
                        )
                        .where(selected.view_key == view_key)
                        .order_by(selected.source_key)
                    )
                )
                if tuple(row[0] for row in current_sources) != existing_keys or not all(
                    row[1] for row in current_sources
                ):
                    raise ProjectionConflict("selected source changed after preparation")
            old = await session.get(PromptProjectionModel, view_key)
            if old is None:
                if expected_epoch is not None or expected_revision or rebuild_reason is None:
                    raise ProjectionConflict("missing projection requires explicit bootstrap")
            else:
                stale_reset = (
                    rebuild_reason == "reset"
                    and expected_epoch is None
                    and expected_revision == 0
                    and (old.generation, old.starts_after_event_id)
                    != (generation, starts_after_event_id)
                )
                invalidated = (
                    old.invalidated_reason is not None
                    and rebuild_reason == old.invalidated_reason
                    and expected_epoch is None
                    and expected_revision == 0
                )
                if old.conversation_id != conversation_id or (
                    not (stale_reset or invalidated)
                    and (old.epoch_id, old.revision) != (expected_epoch, expected_revision)
                ):
                    raise ProjectionConflict("projection revision changed")
                if (
                    old.selected_summary_text
                    and (old.generation, old.starts_after_event_id, old.source_revision)
                    == (generation, starts_after_event_id, expected_source_revision)
                    and (
                        old.selected_summary_coverage > selected_summary_coverage
                        or (
                            old.selected_summary_coverage == selected_summary_coverage
                            and old.selected_summary_text != selected_summary_text
                        )
                    )
                ):
                    raise ProjectionConflict("a newer checkpoint already owns this history")
                if rebuild_reason is None:
                    if (
                        previous_snapshot is None
                        or previous_snapshot._prefix_stamp != _prefix_version(old)
                    ):
                        raise ProjectionConflict("projection changed after preparation")
                    if (
                        old.invalidated_reason,
                        old.generation,
                        old.starts_after_event_id,
                        old.context_key,
                    ) != (None, generation, starts_after_event_id, context_key):
                        raise ProjectionConflict("projection change requires a new epoch")
                if (
                    rebuild_reason is None
                    and old.selected_summary_text
                    and (
                        old.selected_summary_text,
                        old.selected_summary_coverage,
                        old.selected_summary_kind,
                        old.selected_summary_renderer,
                    )
                    != (
                        selected_summary_text,
                        selected_summary_coverage,
                        selected_summary_kind,
                        selected_summary_renderer,
                    )
                ):
                    raise ProjectionConflict("summary representation requires a new epoch")
            same_summary = old is not None and (
                old.selected_summary_text,
                old.selected_summary_coverage,
                old.selected_summary_kind,
                old.selected_summary_renderer,
            ) == (
                selected_summary_text,
                selected_summary_coverage,
                selected_summary_kind,
                selected_summary_renderer,
            )
            if not write or (not encoded and rebuild_reason is None and same_summary):
                assert previous_snapshot is not None
                return previous_snapshot
            used, views = (
                await session.execute(
                    select(
                        func.coalesce(func.sum(PromptProjectionModel.byte_size), 0), func.count()
                    ).where(
                        PromptProjectionModel.view_key != view_key,
                        PromptProjectionModel.invalidated_reason.is_(None),
                    )
                )
            ).one()
            if used + size > self.total_bytes or views >= self.maximum_views:
                victims = (
                    await session.execute(
                        select(PromptProjectionModel.view_key, PromptProjectionModel.byte_size)
                        .where(
                            PromptProjectionModel.view_key != view_key,
                            PromptProjectionModel.invalidated_reason.is_(None),
                        )
                        .order_by(PromptProjectionModel.updated_at, PromptProjectionModel.view_key)
                    )
                ).all()
                for victim in victims:
                    if used + size <= self.total_bytes and views < self.maximum_views:
                        break
                    used -= victim.byte_size
                    views -= 1
                    await session.execute(
                        update(PromptProjectionModel)
                        .where(PromptProjectionModel.view_key == victim.view_key)
                        .values(
                            tail_ordinal=0,
                            item_count=0,
                            byte_size=2,
                            invalidated_reason="capacity",
                            revision=PromptProjectionModel.revision + 1,
                            updated_at=datetime.now(UTC),
                        )
                    )
            if used + size > self.total_bytes or views >= self.maximum_views:
                raise ProjectionCapacityError("projection global budget exceeded")
            row = old or PromptProjectionModel(view_key=view_key, conversation_id=conversation_id)
            row.generation, row.starts_after_event_id = generation, starts_after_event_id
            row.source_revision, row.context_key = expected_source_revision, context_key
            row.epoch_id = str(uuid4()) if rebuild_reason else row.epoch_id
            row.revision = 1 if rebuild_reason else row.revision + 1
            row.rebuild_reason = rebuild_reason or row.rebuild_reason
            row.invalidated_reason = None
            row.tail_ordinal, row.item_count, row.byte_size = tail + len(encoded), count, size
            row.selected_summary_text, row.selected_summary_coverage = (
                selected_summary_text,
                selected_summary_coverage,
            )
            row.selected_summary_kind, row.selected_summary_renderer = (
                selected_summary_kind,
                selected_summary_renderer,
            )
            row.updated_at = datetime.now(UTC)
            session.add(row)
            await session.flush()
            for ordinal, payload in enumerate(encoded, tail + 1):
                session.add(
                    PromptProjectionChunkModel(
                        view_key=view_key,
                        epoch_id=row.epoch_id,
                        ordinal=ordinal,
                        payload_json=payload,
                    )
                )
            if prepared_sources:
                from sqlalchemy.dialects.sqlite import insert

                try:
                    for offset in range(0, len(prepared_sources), 64):
                        await session.execute(
                            insert(ContextSelectionModel).values(
                                prepared_sources[offset : offset + 64]
                            )
                        )
                except IntegrityError as exc:
                    raise ProjectionConflict(
                        "selected representation publication conflict"
                    ) from exc
            await session.flush()
            return _snapshot(row, fragments)

        return ProjectionPublication(publish)

    async def selected(
        self,
        *,
        view_key: str,
        conversation_id: str,
        generation: int,
        visible_event_ids: Set[int] | None = None,
    ) -> list[dict[str, Any]]:
        query = select(
            ContextSelectionModel.id,
            ContextSelectionModel.event_ids_json,
        ).where(
            ContextSelectionModel.view_key == view_key,
            ContextSelectionModel.conversation_id == conversation_id,
            ContextSelectionModel.generation == generation,
        )
        if visible_event_ids is not None:
            empty_ids = query.where(ContextSelectionModel.max_event_id == 0)
            if visible_event_ids:
                first, last = min(visible_event_ids), max(visible_event_ids)
                candidates = empty_ids.union_all(
                    query.where(
                        ContextSelectionModel.max_event_id.between(first, last),
                        ContextSelectionModel.min_event_id >= first,
                    )
                ).subquery()
                query = select(candidates.c.id, candidates.c.event_ids_json)
            else:
                query = empty_ids
        async with self.database.read_session() as reader:
            await reader.execute(text("BEGIN"))
            rows = await reader.execute(query.order_by("id"))
            needed = [
                identity
                for identity, ids in rows
                if visible_event_ids is None or set(json.loads(ids)) <= visible_event_ids
            ]
            payloads: dict[int, dict[str, Any]] = {}
            for offset in range(0, len(needed), 256):
                selected = await reader.execute(
                    select(ContextSelectionModel.id, ContextSelectionModel.payload_json).where(
                        ContextSelectionModel.id.in_(needed[offset : offset + 256])
                    )
                )
                payloads.update((identity, json.loads(payload)) for identity, payload in selected)
            return [payloads[identity] for identity in needed]

    async def invalidate_view(self, view_key: str, *, reason: str) -> None:
        if reason not in REBUILD_REASONS:
            raise ValueError("invalid projection rebuild reason")
        async with self.database.immediate_session() as session:
            await session.execute(
                update(PromptProjectionModel)
                .where(
                    PromptProjectionModel.view_key == view_key,
                    PromptProjectionModel.invalidated_reason.is_(None),
                )
                .values(
                    tail_ordinal=0,
                    item_count=0,
                    byte_size=2,
                    invalidated_reason=reason,
                    revision=PromptProjectionModel.revision + 1,
                    updated_at=datetime.now(UTC),
                )
            )
        self._views.pop(view_key, None)


def _encode_item(item: dict[str, Any]) -> str:
    return json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _selection_key(item: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(item["event_ids"]).encode("utf-8")).hexdigest()


def _prefix_version(row: PromptProjectionModel) -> tuple[object, ...]:
    return (
        row.view_key,
        row.conversation_id,
        row.epoch_id,
        row.revision,
        row.generation,
        row.source_revision,
        row.starts_after_event_id,
        row.context_key,
        row.invalidated_reason,
    )


def _snapshot(row: PromptProjectionModel, fragments: FrozenFragments) -> ProjectionSnapshot:
    stamp = _prefix_version(row)
    return ProjectionSnapshot(
        row.epoch_id,
        row.revision,
        row.generation,
        row.context_key,
        fragments,
        row.rebuild_reason,
        row.source_revision,
        row.selected_summary_text,
        row.selected_summary_coverage,
        stamp,
        (row.view_key, fragments, stamp),
        selected_summary_kind=row.selected_summary_kind,
        selected_summary_renderer=row.selected_summary_renderer,
        tail_ordinal=row.tail_ordinal,
        byte_size=row.byte_size,
    )
