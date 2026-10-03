"""Atomic bounded storage for frozen model-input sequences, never delivery facts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.persistence.database import Database

REBUILD_REASONS = frozenset(
    {
        "bootstrap",
        "reset",
        "rollup",
        "rollup_ready",
        "capacity",
        "contract_changed",
        "read_scope_changed",
        "deleted_event",
        "source_changed",
        "protocol_changed",
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
    contract_revision: str
    payload_json: str
    rebuild_reason: str
    source_revision: int
    selected_summary_text: str | None = None
    selected_summary_coverage: int = 0

    def items(self) -> list[dict[str, Any]]:
        # Each consumer gets a copy; changing it cannot mutate the committed view.
        return list(json.loads(self.payload_json))


@dataclass(frozen=True, slots=True)
class ProjectionPublication:
    publish: Callable[[AsyncSession], Awaitable[ProjectionSnapshot]]
    observation_sources: tuple[tuple[str, int], ...]

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
        reclaim: bool = False,
    ) -> None:
        if min(max_view_bytes, total_bytes, maximum_views) <= 0:
            raise ValueError("invalid projection budget")
        self.database = database
        self.view_bytes = min(max_view_bytes, total_bytes)
        self.total_bytes, self.maximum_views = total_bytes, maximum_views
        self.reclaim = reclaim

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

    async def commit(
        self,
        *,
        view_key: str,
        conversation_id: str,
        generation: int,
        expected_source_revision: int,
        starts_after_event_id: int,
        context_key: str,
        contract_revision: str,
        items: list[dict[str, Any]],
        expected_epoch: str | None = None,
        expected_revision: int = 0,
        rebuild_reason: str | None = None,
        actor_id: str = "",
        read_scope: str = "",
        selected_summary_text: str | None = None,
        selected_summary_coverage: int = 0,
        current_snapshot: dict[str, Any] | None = None,
        snapshot_event_id: int | None = None,
        snapshot_fragment_index: int | None = None,
    ) -> ProjectionSnapshot:
        publication = await self.prepare_commit(
            view_key=view_key,
            conversation_id=conversation_id,
            generation=generation,
            expected_source_revision=expected_source_revision,
            starts_after_event_id=starts_after_event_id,
            context_key=context_key,
            contract_revision=contract_revision,
            items=items,
            expected_epoch=expected_epoch,
            expected_revision=expected_revision,
            rebuild_reason=rebuild_reason,
            actor_id=actor_id,
            read_scope=read_scope,
            selected_summary_text=selected_summary_text,
            selected_summary_coverage=selected_summary_coverage,
            current_snapshot=current_snapshot,
            snapshot_event_id=snapshot_event_id,
            snapshot_fragment_index=snapshot_fragment_index,
        )
        async with self.database.immediate_session() as session:
            return await publication(session)

    async def prepare_commit(
        self,
        *,
        view_key: str,
        conversation_id: str,
        generation: int,
        expected_source_revision: int,
        starts_after_event_id: int,
        context_key: str,
        contract_revision: str,
        items: list[dict[str, Any]],
        expected_epoch: str | None = None,
        expected_revision: int = 0,
        rebuild_reason: str | None = None,
        actor_id: str = "",
        read_scope: str = "",
        selected_summary_text: str | None = None,
        selected_summary_coverage: int = 0,
        current_snapshot: dict[str, Any] | None = None,
        snapshot_event_id: int | None = None,
        snapshot_fragment_index: int | None = None,
    ) -> ProjectionPublication:
        """Append exact items, or explicitly replace an epoch under a compare-and-swap.

        Caller supplies its read-policy view key; this method grants no history
        access and must only receive already bounded, selected model-input items.
        """
        for key in (view_key, context_key, contract_revision):
            if len(key) != 64 or any(char not in "0123456789abcdef" for char in key):
                raise ValueError("projection keys must be SHA-256 fingerprints")
        if rebuild_reason is not None and rebuild_reason not in REBUILD_REASONS:
            raise ValueError("invalid projection rebuild reason")
        items = deepcopy(items)
        prepared_snapshot: dict[str, Any] | None = None
        if current_snapshot and actor_id and read_scope:
            from qq_ai_bot.conversation.observations import encode

            snapshot_payload = encode(current_snapshot)
            snapshot_key = (
                "snapshot:"
                + hashlib.sha256(
                    encode(
                        [
                            view_key,
                            conversation_id,
                            generation,
                            actor_id,
                            read_scope,
                            snapshot_event_id,
                            snapshot_payload,
                        ]
                    ).encode()
                ).hexdigest()
            )
            snapshot_id = str(uuid5(NAMESPACE_URL, snapshot_key))
            prepared_snapshot = dict(
                id=snapshot_id,
                conversation_id=conversation_id,
                generation=generation,
                actor_id=actor_id,
                read_scope=read_scope,
                source_key=snapshot_key,
                source_event_id=snapshot_event_id,
                version=1,
                payload_json=snapshot_payload,
                parent_sources_json="[]",
                summary_view_key=view_key,
                created_at=datetime.now(UTC),
            )
            for item in reversed(items):
                if snapshot_event_id is not None and snapshot_event_id in item["event_ids"]:
                    item["observation_id"], item["observation_version"] = snapshot_id, 1
                    break
            if snapshot_event_id is None and snapshot_fragment_index is not None:
                if not 0 <= snapshot_fragment_index < len(items):
                    raise ProjectionConflict("snapshot fragment is missing")
                item = items[snapshot_fragment_index]
                if item["event_ids"] or "observation_id" in item:
                    raise ProjectionConflict("snapshot fragment is not a fresh envelope")
                item["observation_id"], item["observation_version"] = snapshot_id, 1
        if len(items) > 262_144 or not all(isinstance(item, dict) for item in items):
            raise ProjectionCapacityError("projection item limit exceeded")
        payload = json.dumps(items, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        size = len(payload.encode("utf-8"))
        if size > self.view_bytes:
            raise ProjectionCapacityError("projection view budget exceeded")
        prepared_prefix = (
            await self._prepare_prefix(view_key, payload) if rebuild_reason is None else None
        )
        # Freeze and encode selected-source rows before taking the writer. Their
        # order is the already prepared model order, never source timestamps.
        from qq_ai_bot.conversation.frozen_fragments import FrozenFragments
        from qq_ai_bot.conversation.observation_models import (
            ContextObservationModel,
            ContextSelectionModel,
        )
        from qq_ai_bot.conversation.observations import privacy_generation, validate_observations

        async with self.database.sessions() as prepared_session:
            prepared_privacy = await privacy_generation(prepared_session)
        if prepared_snapshot is not None:
            prepared_snapshot["privacy_generation"] = prepared_privacy

        fragments = FrozenFragments.load(items) if actor_id and read_scope else None
        prepared_sources = []
        summary_parents: dict[str, tuple[str, ...]] = {}
        if fragments is not None and fragments.observation_sources:
            from qq_ai_bot.conversation.observation_models import ContextObservationModel

            async with self.database.sessions() as prepared_session:
                summaries = (
                    await prepared_session.execute(
                        select(
                            ContextObservationModel.id,
                            ContextObservationModel.parent_sources_json,
                            ContextObservationModel.summary_view_key,
                        ).where(
                            ContextObservationModel.id.in_(
                                [identity for identity, _ in fragments.observation_sources]
                            )
                        )
                    )
                ).all()
            for identity, parent_json, summary_view in summaries:
                if summary_view is not None:
                    if summary_view != view_key:
                        raise ProjectionConflict("observation summary view changed")
                    summary_parents[identity] = tuple(
                        parent_id for parent_id, _ in json.loads(parent_json)
                    )
        if actor_id and read_scope:
            for item in items:
                encoded = json.dumps(
                    item, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                )
                # Event identity, not prose, deduplicates an observed chat. Data-
                # only fragments are distinct frozen envelopes/observations.
                key = json.dumps(item["event_ids"]) if item["event_ids"] else encoded
                prepared_sources.append(
                    dict(
                        view_key=view_key,
                        conversation_id=conversation_id,
                        generation=generation,
                        actor_id=actor_id,
                        read_scope=read_scope,
                        source_key=hashlib.sha256(key.encode("utf-8")).hexdigest(),
                        event_ids_json=json.dumps(item["event_ids"]),
                        observation_sources_json=json.dumps(
                            [[item["observation_id"], item["observation_version"]]]
                            if "observation_id" in item
                            else []
                        ),
                        payload_json=encoded,
                        created_at=datetime.now(UTC),
                    )
                )

        if prepared_snapshot is not None:
            # Source publication is not observation. Only the dispatch CAS below
            # adds selection/coverage, and readers exclude unselected snapshots.
            async def checked_snapshot(session: AsyncSession) -> bool:
                owner = await session.get(CanonicalConversationModel, conversation_id)
                if (
                    owner is None
                    or (owner.generation, owner.starts_after_event_id, owner.prompt_source_revision)
                    != (generation, starts_after_event_id, expected_source_revision)
                    or await privacy_generation(session) != prepared_privacy
                ):
                    raise ProjectionConflict("snapshot source changed before publication")
                if snapshot_event_id is not None:
                    from qq_ai_bot.persistence.models import ChatEventModel

                    anchor = await session.get(ChatEventModel, snapshot_event_id)
                    if anchor is None or anchor.canonical_conversation_id != conversation_id:
                        raise ProjectionConflict("snapshot event source changed")
                stored = await session.get(ContextObservationModel, prepared_snapshot["id"])
                if stored is not None and stored.payload_json != prepared_snapshot["payload_json"]:
                    raise ProjectionConflict("snapshot source changed")
                return stored is not None

            async with self.database.sessions() as source_reader:
                from sqlalchemy import text

                await source_reader.execute(text("BEGIN"))
                exists = await checked_snapshot(source_reader)
            if not exists:
                async with self.database.immediate_session() as source_writer:
                    if not await checked_snapshot(source_writer):
                        source_writer.add(ContextObservationModel(**prepared_snapshot))

        # Traverse parent references in a read snapshot, never in the journal
        # writer. Every mutation/deletion advances the canonical source revision
        # checked by publish, and privacy has its own scalar fence.
        async with self.database.sessions() as source_reader:
            from sqlalchemy import text

            await source_reader.execute(text("BEGIN"))
            if not await validate_observations(
                source_reader,
                conversation_id,
                generation,
                actor_id,
                read_scope,
                fragments.observation_sources if fragments is not None else (),
            ):
                raise ProjectionConflict("projection observation source changed")
            owner = await source_reader.get(CanonicalConversationModel, conversation_id)
            if owner is None or owner.prompt_source_revision != expected_source_revision:
                raise ProjectionConflict("projection source revision changed")

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
                        old.contract_revision,
                    ) != (
                        generation,
                        starts_after_event_id,
                        context_key,
                        contract_revision,
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
                        func.count().filter(PromptProjectionModel.invalidated_reason.is_(None))
                        if self.reclaim
                        else func.count(),
                    ).where(PromptProjectionModel.view_key != view_key)
                )
            ).one()
            if self.reclaim:
                # Capacity decisions need metadata, never another view's payload.
                others = (
                    (
                        await session.execute(
                            select(
                                PromptProjectionModel.view_key,
                                PromptProjectionModel.byte_size,
                                PromptProjectionModel.invalidated_reason,
                            )
                            .where(PromptProjectionModel.view_key != view_key)
                            .order_by(
                                PromptProjectionModel.updated_at, PromptProjectionModel.view_key
                            )
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
            row = old or PromptProjectionModel(view_key=view_key, conversation_id=conversation_id)
            row.generation, row.starts_after_event_id = generation, starts_after_event_id
            row.source_revision = expected_source_revision
            row.context_key, row.contract_revision = context_key, contract_revision
            row.epoch_id = str(uuid4()) if rebuild_reason else row.epoch_id
            row.revision = 1 if rebuild_reason else row.revision + 1
            row.rebuild_reason = rebuild_reason or row.rebuild_reason
            row.invalidated_reason = None
            row.payload_json, row.byte_size = payload, size
            row.selected_summary_text = selected_summary_text
            row.selected_summary_coverage = selected_summary_coverage
            row.updated_at = datetime.now(UTC)
            session.add(row)
            if prepared_sources:
                from sqlalchemy.dialects.sqlite import insert

                await session.execute(
                    insert(ContextSelectionModel)
                    .values(prepared_sources)
                    .on_conflict_do_nothing(index_elements=["view_key", "source_key"])
                )
            if summary_parents:
                from qq_ai_bot.mcp.repository import ToolArtifactRepository

                for parents in summary_parents.values():
                    for parent in parents:
                        await ToolArtifactRepository.release_refs(session, "observation", parent)
            await session.flush()
            return _snapshot(row)

        return ProjectionPublication(
            publish, fragments.observation_sources if fragments is not None else ()
        )

    async def _prepare_prefix(
        self, view_key: str, payload: str
    ) -> tuple[tuple[object, ...], bool] | None:
        """Compare immutable wire data before reserving the SQLite writer."""
        async with self.database.sessions() as session:
            old = await session.get(PromptProjectionModel, view_key)
            if old is None:
                return None
            previous = json.loads(old.payload_json)
            # Compare serialization, not dict equality: key order is wire data.
            prefix = json.dumps(
                json.loads(payload)[: len(previous)],
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            return _prefix_version(old), prefix == old.payload_json

    async def invalidate(self, conversation_id: str) -> None:
        """Remove model-input copies when a source is deleted/reset; leave the ledger intact."""
        async with self.database.sessions() as session:
            await session.execute(
                delete(PromptProjectionModel).where(
                    PromptProjectionModel.conversation_id == conversation_id,
                )
            )
            await session.commit()

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


def _prefix_version(row: PromptProjectionModel) -> tuple[object, ...]:
    return (
        row.conversation_id,
        row.epoch_id,
        row.revision,
        row.generation,
        row.source_revision,
        row.starts_after_event_id,
        row.context_key,
        row.contract_revision,
        row.invalidated_reason,
    )


def _snapshot(row: PromptProjectionModel) -> ProjectionSnapshot:
    return ProjectionSnapshot(
        row.epoch_id,
        row.revision,
        row.generation,
        row.context_key,
        row.contract_revision,
        row.payload_json,
        row.rebuild_reason,
        row.source_revision,
        row.selected_summary_text,
        row.selected_summary_coverage,
    )
