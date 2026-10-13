"""Atomic bounded storage for frozen model-input sequences, never delivery facts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.observation_models import ContextSelectionModel
from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.execution_trace.phases import collect_phase_metrics, model_detail
from qq_ai_bot.persistence.database import Database

SUMMARY_RENDERER_VERSION = 1

REBUILD_REASONS = frozenset(
    {
        "bootstrap",
        "reset",
        "rollup",
        "rollup_ready",
        "capacity",
        "deleted_event",
        "source_changed",
    }
)


class ProjectionConflict(ValueError):
    """The caller must obtain a fresh source/snapshot before preparing new input."""


class ProjectionCapacityError(ValueError):
    """Compaction or cleanup must establish an explicit new context boundary."""


@dataclass(frozen=True, slots=True)
class ProjectionSnapshot:
    epoch_id: str
    revision: int
    generation: int
    context_key: str
    payload_json: str
    rebuild_reason: str
    source_revision: int
    selected_summary_text: str | None = None
    selected_summary_coverage: int = 0
    _prefix_stamp: tuple[object, ...] | None = field(default=None, repr=False, compare=False)
    _prefix_origin: tuple[str, str, tuple[object, ...]] | None = field(
        default=None, repr=False, compare=False
    )

    selected_summary_kind: str | None = None
    selected_summary_renderer: int | None = None

    def items(self) -> list[dict[str, Any]]:
        # Each consumer gets a copy; changing it cannot mutate the committed view.
        return list(json.loads(self.payload_json))


@dataclass(frozen=True, slots=True)
class ProjectionPublication:
    publish: Callable[[AsyncSession], Awaitable[ProjectionSnapshot]]

    async def __call__(self, session: AsyncSession) -> ProjectionSnapshot:
        return await self.publish(session)


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

    async def read(self, view_key: str) -> ProjectionSnapshot | None:
        async with self.database.sessions() as session:
            row = await session.scalar(
                select(PromptProjectionModel)
                .join(
                    CanonicalConversationModel,
                    PromptProjectionModel.conversation_id == CanonicalConversationModel.id,
                )
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
            return _snapshot(row) if row else None

    async def invalidation_reason(self, view_key: str) -> str | None:
        async with self.database.sessions() as session:
            return await session.scalar(
                select(PromptProjectionModel.invalidated_reason).where(
                    PromptProjectionModel.view_key == view_key,
                )
            )

    async def prepare_commit(
        self,
        *,
        view_key: str,
        conversation_id: str,
        generation: int,
        expected_source_revision: int,
        starts_after_event_id: int,
        context_key: str,
        items: list[dict[str, Any]],
        expected_epoch: str | None = None,
        expected_revision: int = 0,
        rebuild_reason: str | None = None,
        selected_summary_text: str | None = None,
        selected_summary_coverage: int = 0,
        selected_summary_kind: str | None = None,
        selected_summary_renderer: int | None = None,
        previous_snapshot: ProjectionSnapshot | None = None,
        previous_item_count: int | None = None,
    ) -> ProjectionPublication:
        """Append exact items, or explicitly replace an epoch under a compare-and-swap.

        The caller supplies canonical Conversation history already selected by
        the ledger. This publication preserves its original event renderings.
        """
        if rebuild_reason is not None and rebuild_reason not in REBUILD_REASONS:
            raise ValueError("invalid projection rebuild reason")
        items = deepcopy(items)
        if not all(isinstance(item, dict) for item in items):
            raise ProjectionCapacityError("projection item limit exceeded")
        payload = json.dumps(items, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        size = len(payload.encode("utf-8"))
        collect_phase_metrics(item_count=len(items), payload_bytes=size)
        if size > self.view_bytes:
            raise ProjectionCapacityError("projection view budget exceeded")
        prepared_prefix = (
            await self._prepare_prefix(
                view_key,
                items,
                previous_snapshot=previous_snapshot,
                previous_item_count=previous_item_count,
            )
            if rebuild_reason is None
            else None
        )
        # Freeze and encode selected-source rows before taking the writer. Their
        # order is the already prepared model order, never source timestamps.
        from qq_ai_bot.conversation.frozen_fragments import FrozenFragments
        from qq_ai_bot.conversation.observations import privacy_generation

        FrozenFragments.load(items)
        prepared_sources = []

        # Read canonical source facts before taking the writer. Source mutation
        # and privacy are rechecked by the publication CAS.
        with model_detail("projection_sources_read"):
            async with self.database.sessions() as source_reader:
                await source_reader.execute(text("BEGIN"))
                prepared_privacy = await privacy_generation(source_reader)
                owner = await source_reader.get(CanonicalConversationModel, conversation_id)
                if owner is None or (owner.generation, owner.starts_after_event_id) != (
                    generation,
                    starts_after_event_id,
                ):
                    raise ProjectionConflict("projection source generation changed")
                if owner.prompt_source_revision != expected_source_revision:
                    raise ProjectionConflict("projection source revision changed")
                candidates: dict[str, dict[str, Any]] = {}
                for item in items:
                    key = _selection_key(item)
                    previous_item = candidates.get(key)
                    if previous_item is not None:
                        if _encode_item(previous_item) != _encode_item(item):
                            raise ProjectionConflict("selected representation identity conflict")
                        continue
                    candidates[key] = item
                existing: dict[str, Any] = {}
                keys = list(candidates)
                for offset in range(0, len(keys), 256):
                    rows = await source_reader.execute(
                        select(
                            ContextSelectionModel.source_key,
                            ContextSelectionModel.id,
                            ContextSelectionModel.conversation_id,
                            ContextSelectionModel.generation,
                            ContextSelectionModel.event_ids_json,
                        ).where(
                            ContextSelectionModel.view_key == view_key,
                            ContextSelectionModel.source_key.in_(keys[offset : offset + 256]),
                        )
                    )
                    existing.update((row.source_key, row) for row in rows)
                frozen_keys = prepared_prefix[2] if prepared_prefix is not None else frozenset()
                inspect_ids = []
                for key, row in existing.items():
                    item = candidates[key]
                    if (
                        row.conversation_id,
                        row.generation,
                        row.event_ids_json,
                    ) != (
                        conversation_id,
                        generation,
                        json.dumps(item["event_ids"]),
                    ):
                        raise ProjectionConflict("selected representation owner changed")
                    # Existing frozen prefix bytes were already verified against
                    # the previous projection. Explicit epochs may select raw
                    # canonical history again; retain its original selection
                    # provenance rather than rewriting it or rejecting that boundary.
                    if rebuild_reason is None and key not in frozen_keys:
                        inspect_ids.append(row.id)
                for offset in range(0, len(inspect_ids), 256):
                    rows = await source_reader.execute(
                        select(
                            ContextSelectionModel.source_key, ContextSelectionModel.payload_json
                        ).where(ContextSelectionModel.id.in_(inspect_ids[offset : offset + 256]))
                    )
                    for key, stored_payload in rows:
                        if stored_payload != _encode_item(candidates[key]):
                            raise ProjectionConflict("selected representation is immutable")
                # Keep actual submitted order, never SQL result/key order. Only
                # genuinely missing rows carry their full payload into the writer.
                for key, item in candidates.items():
                    if key in existing:
                        continue
                    prepared_sources.append(
                        dict(
                            view_key=view_key,
                            conversation_id=conversation_id,
                            generation=generation,
                            source_key=key,
                            event_ids_json=json.dumps(item["event_ids"]),
                            payload_json=_encode_item(item),
                            created_at=datetime.now(UTC),
                        )
                    )

        async def publish(session: AsyncSession) -> ProjectionSnapshot:
            # The caller owns a short writer; preparation above does all payload IO.
            source = await session.get(CanonicalConversationModel, conversation_id)
            if source is None or (source.generation, source.starts_after_event_id) != (
                generation,
                starts_after_event_id,
            ):
                raise ProjectionConflict("projection source generation changed")
            if source.prompt_source_revision != expected_source_revision:
                raise ProjectionConflict("projection source revision changed")
            if await privacy_generation(session) != prepared_privacy:
                raise ProjectionConflict("projection privacy changed")
            old = await session.get(
                PromptProjectionModel, view_key, options=[defer(PromptProjectionModel.payload_json)]
            )
            if old is None:
                if expected_epoch is not None or expected_revision != 0 or rebuild_reason is None:
                    raise ProjectionConflict("missing projection requires explicit bootstrap")
            else:
                stale_reset = (
                    rebuild_reason == "reset"
                    and expected_epoch is None
                    and expected_revision == 0
                    and (old.generation, old.starts_after_event_id)
                    != (generation, starts_after_event_id)
                )
                invalidated_rebuild = (
                    old.invalidated_reason is not None
                    and rebuild_reason == old.invalidated_reason
                    and expected_epoch is None
                    and expected_revision == 0
                )
                if old.conversation_id != conversation_id or (
                    not (stale_reset or invalidated_rebuild)
                    and (old.epoch_id, old.revision) != (expected_epoch, expected_revision)
                ):
                    raise ProjectionConflict("projection revision changed")
                if rebuild_reason is None:
                    if old.invalidated_reason is not None:
                        raise ProjectionConflict("invalidated projection requires a new epoch")
                    if (
                        old.generation,
                        old.starts_after_event_id,
                        old.context_key,
                    ) != (
                        generation,
                        starts_after_event_id,
                        context_key,
                    ):
                        raise ProjectionConflict("projection change requires a new epoch")
                    if prepared_prefix is None or prepared_prefix[0] != _prefix_version(old):
                        raise ProjectionConflict("projection changed after prefix preparation")
                    if not prepared_prefix[1]:
                        raise ProjectionConflict("committed projection prefix cannot be rewritten")
            used, count = (
                await session.execute(
                    select(
                        func.coalesce(func.sum(PromptProjectionModel.byte_size), 0),
                        func.count().filter(PromptProjectionModel.invalidated_reason.is_(None)),
                    ).where(PromptProjectionModel.view_key != view_key)
                )
            ).one()
            others = (
                (
                    await session.execute(
                        select(
                            PromptProjectionModel.view_key,
                            PromptProjectionModel.byte_size,
                            PromptProjectionModel.invalidated_reason,
                        )
                        .where(PromptProjectionModel.view_key != view_key)
                        .order_by(PromptProjectionModel.updated_at, PromptProjectionModel.view_key)
                    )
                ).all()
                if used + size > self.total_bytes or count >= self.maximum_views
                else []
            )
            for victim in others:
                if used + size <= self.total_bytes and count < self.maximum_views:
                    break
                if victim.invalidated_reason is not None:
                    if used + size > self.total_bytes:
                        used -= victim.byte_size
                        await session.execute(
                            delete(PromptProjectionModel).where(
                                PromptProjectionModel.view_key == victim.view_key
                            )
                        )
                    continue
                used -= victim.byte_size - 2
                count -= 1
                await session.execute(
                    update(PromptProjectionModel)
                    .where(PromptProjectionModel.view_key == victim.view_key)
                    .values(
                        payload_json="[]",
                        byte_size=2,
                        invalidated_reason="capacity",
                        revision=PromptProjectionModel.revision + 1,
                        updated_at=datetime.now(UTC),
                    )
                )
            await session.flush()
            markers = list(
                (
                    await session.execute(
                        select(PromptProjectionModel.view_key, PromptProjectionModel.byte_size)
                        .where(
                            PromptProjectionModel.view_key != view_key,
                            PromptProjectionModel.invalidated_reason.is_not(None),
                        )
                        .order_by(
                            PromptProjectionModel.updated_at.desc(),
                            PromptProjectionModel.view_key,
                        )
                        .offset(self.maximum_views)
                    )
                ).all()
            )
            for marker in markers:
                used -= marker.byte_size
                await session.execute(
                    delete(PromptProjectionModel).where(
                        PromptProjectionModel.view_key == marker.view_key
                    )
                )
            if used + size > self.total_bytes or count >= self.maximum_views:
                raise ProjectionCapacityError("projection global budget exceeded")
            if old is not None and not rebuild_reason and old.selected_summary_text is not None:
                if (
                    old.selected_summary_text,
                    old.selected_summary_coverage,
                    old.selected_summary_kind,
                    old.selected_summary_renderer,
                ) != (
                    selected_summary_text,
                    selected_summary_coverage,
                    selected_summary_kind,
                    selected_summary_renderer,
                ):
                    raise ProjectionConflict("summary representation requires a new epoch")
            row = old or PromptProjectionModel(view_key=view_key, conversation_id=conversation_id)
            row.generation, row.starts_after_event_id = generation, starts_after_event_id
            row.source_revision = expected_source_revision
            row.context_key = context_key
            row.epoch_id = str(uuid4()) if rebuild_reason else row.epoch_id
            row.revision = 1 if rebuild_reason else row.revision + 1
            row.rebuild_reason = rebuild_reason or row.rebuild_reason
            row.invalidated_reason = None
            row.payload_json, row.byte_size = payload, size
            row.selected_summary_text = selected_summary_text
            row.selected_summary_coverage = selected_summary_coverage
            row.selected_summary_kind = selected_summary_kind
            row.selected_summary_renderer = selected_summary_renderer
            row.updated_at = datetime.now(UTC)
            session.add(row)
            if prepared_sources:
                from sqlalchemy.dialects.sqlite import insert

                try:
                    # Seven bound columns per row; keep a batch below even the
                    # legacy SQLite 999-variable build limit.
                    for offset in range(0, len(prepared_sources), 64):
                        await session.execute(
                            insert(ContextSelectionModel).values(
                                prepared_sources[offset : offset + 64]
                            )
                        )
                except IntegrityError as exc:
                    # Valid same-view publishers must pass the projection CAS.
                    # Never silently hide a changed immutable selection if a
                    # conflicting insert nevertheless reaches this boundary.
                    raise ProjectionConflict(
                        "selected representation publication conflict"
                    ) from exc
            await session.flush()
            return _snapshot(row)

        return ProjectionPublication(publish)

    async def selected(
        self,
        *,
        view_key: str,
        conversation_id: str,
        generation: int,
        visible_event_ids: frozenset[int] | None = None,
    ) -> list[dict[str, Any]]:
        async with self.database.sessions() as session:
            await session.execute(text("BEGIN"))
            needed = []
            after = 0
            while True:
                page = (
                    await session.execute(
                        select(
                            ContextSelectionModel.id,
                            ContextSelectionModel.event_ids_json,
                        )
                        .where(
                            ContextSelectionModel.view_key == view_key,
                            ContextSelectionModel.conversation_id == conversation_id,
                            ContextSelectionModel.generation == generation,
                            ContextSelectionModel.id > after,
                        )
                        .order_by(ContextSelectionModel.id)
                        .limit(256)
                    )
                ).all()
                if not page:
                    break
                for row in page:
                    if (
                        visible_event_ids is not None
                        and not set(json.loads(row.event_ids_json)) <= visible_event_ids
                    ):
                        continue
                    needed.append(row.id)
                after = page[-1].id
            payloads: dict[int, dict[str, Any]] = {}
            for offset in range(0, len(needed), 256):
                loaded = await session.execute(
                    select(
                        ContextSelectionModel.id,
                        ContextSelectionModel.payload_json,
                    ).where(ContextSelectionModel.id.in_(needed[offset : offset + 256]))
                )
                for identity, payload in loaded:
                    item = json.loads(payload)
                    payloads[identity] = item
            return [payloads[identity] for identity in needed]

    async def _prepare_prefix(
        self,
        view_key: str,
        items: list[dict[str, Any]],
        *,
        previous_snapshot: ProjectionSnapshot | None = None,
        previous_item_count: int | None = None,
    ) -> tuple[tuple[object, ...], bool, frozenset[str]] | None:
        """Compare immutable wire data before reserving the SQLite writer."""
        if previous_snapshot is not None and previous_snapshot._prefix_stamp is not None:
            # This immutable repository snapshot is already held by preparation.
            # Publication still compares its complete stamp under the writer.
            origin = previous_snapshot._prefix_origin
            if (
                origin is None
                or origin[0] != view_key
                or origin[1] is not previous_snapshot.payload_json
                or origin[2] is not previous_snapshot._prefix_stamp
            ):
                raise ProjectionConflict("projection snapshot origin changed")
            stamp, old_payload = previous_snapshot._prefix_stamp, previous_snapshot.payload_json
            count = (
                previous_item_count
                if previous_item_count is not None
                else len(previous_snapshot.items())
            )
        else:
            async with self.database.sessions() as session:
                old = await session.get(PromptProjectionModel, view_key)
                if old is None:
                    return None
                stamp, old_payload = _prefix_version(old), old.payload_json
                count = len(json.loads(old_payload))
        prefix_items = items[:count]
        # Preserve the existing frozen-prefix serialization check. This is an
        # implementation constraint of the current store, not a new wire rule.
        prefix = json.dumps(
            prefix_items, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        keys = (
            frozenset(_selection_key(item) for item in prefix_items)
            if all("event_ids" in item for item in prefix_items)
            else frozenset()
        )
        return stamp, prefix == old_payload, keys

    async def invalidate_view(self, view_key: str, *, reason: str) -> None:
        """Retire an input representation while keeping its next-epoch reason."""
        if reason not in REBUILD_REASONS:
            raise ValueError("invalid projection rebuild reason")
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                update(PromptProjectionModel)
                .where(
                    PromptProjectionModel.view_key == view_key,
                    PromptProjectionModel.invalidated_reason.is_(None),
                )
                .values(
                    payload_json="[]",
                    byte_size=2,
                    invalidated_reason=reason,
                    revision=PromptProjectionModel.revision + 1,
                    updated_at=datetime.now(UTC),
                )
            )


def _encode_item(item: dict[str, Any]) -> str:
    return json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _selection_key(item: dict[str, Any]) -> str:
    # Identity is the original canonical event group, not the current reader.
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


def _snapshot(row: PromptProjectionModel) -> ProjectionSnapshot:
    stamp = _prefix_version(row)
    return ProjectionSnapshot(
        row.epoch_id,
        row.revision,
        row.generation,
        row.context_key,
        row.payload_json,
        row.rebuild_reason,
        row.source_revision,
        row.selected_summary_text,
        row.selected_summary_coverage,
        stamp,
        # Keep the original immutable string reference, without another body
        # copy or encode. dataclasses.replace must not substitute a new prefix.
        (row.view_key, row.payload_json, stamp),
        selected_summary_kind=row.selected_summary_kind,
        selected_summary_renderer=row.selected_summary_renderer,
    )
