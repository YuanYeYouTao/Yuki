"""Atomic bounded storage for frozen model-input sequences, never delivery facts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select, text, update

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.persistence.database import Database

REBUILD_REASONS = frozenset(
    {
        "bootstrap",
        "reset",
        "rollup",
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

    def items(self) -> list[dict[str, Any]]:
        # Each consumer gets a copy; changing it cannot mutate the committed view.
        return list(json.loads(self.payload_json))


class PromptProjectionRepository:
    def __init__(
        self,
        database: Database,
        *,
        max_context_characters: int,
        total_bytes: int = 16 * 1024 * 1024,
        maximum_views: int = 128,
        reclaim: bool = False,
    ) -> None:
        if min(max_context_characters, total_bytes, maximum_views) <= 0:
            raise ValueError("invalid projection budget")
        self.database = database
        self.view_bytes = min(max_context_characters * 4, 1024 * 1024)
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
    ) -> ProjectionSnapshot:
        """Append exact items, or explicitly replace an epoch under a compare-and-swap.

        Caller supplies its read-policy view key; this method grants no history
        access and must only receive already bounded, selected model-input items.
        """
        for key in (view_key, context_key, contract_revision):
            if len(key) != 64 or any(char not in "0123456789abcdef" for char in key):
                raise ValueError("projection keys must be SHA-256 fingerprints")
        if rebuild_reason is not None and rebuild_reason not in REBUILD_REASONS:
            raise ValueError("invalid projection rebuild reason")
        if len(items) > 2048 or not all(isinstance(item, dict) for item in items):
            raise ProjectionCapacityError("projection item limit exceeded")
        payload = json.dumps(items, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        size = len(payload.encode("utf-8"))
        if size > self.view_bytes:
            raise ProjectionCapacityError("projection view budget exceeded")
        async with self.database.sessions() as session:
            # SQLite is the supported deployment store. Reserve its writer before
            # checking global limits and CAS, including across repository instances.
            await session.execute(text("BEGIN IMMEDIATE"))
            source = await session.get(CanonicalConversationModel, conversation_id)
            if source is None or (source.generation, source.starts_after_event_id) != (
                generation,
                starts_after_event_id,
            ):
                raise ProjectionConflict("projection source generation changed")
            if source.prompt_source_revision != expected_source_revision:
                raise ProjectionConflict("projection source revision changed")
            old = await session.get(PromptProjectionModel, view_key)
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
                    previous = json.loads(old.payload_json)
                    # Compare serialization, not dict equality: key order is wire data.
                    prefix = json.dumps(
                        items[: len(previous)],
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    if prefix != old.payload_json:
                        raise ProjectionConflict("committed projection prefix cannot be rewritten")
            used, count = (
                await session.execute(
                    select(
                        func.coalesce(func.sum(PromptProjectionModel.byte_size), 0),
                        func.count(),
                    ).where(PromptProjectionModel.view_key != view_key)
                )
            ).one()
            if self.reclaim:
                others = list(
                    (
                        await session.scalars(
                            select(PromptProjectionModel)
                            .where(PromptProjectionModel.view_key != view_key)
                            .order_by(
                                PromptProjectionModel.updated_at, PromptProjectionModel.view_key
                            )
                        )
                    ).all()
                )
                count = sum(row.invalidated_reason is None for row in others)
                for victim in others:
                    if used + size <= self.total_bytes and count < self.maximum_views:
                        break
                    if victim.invalidated_reason is not None:
                        if used + size > self.total_bytes:
                            used -= victim.byte_size
                            await session.delete(victim)
                        continue
                    used -= victim.byte_size - 2
                    count -= 1
                    victim.payload_json, victim.byte_size = "[]", 2
                    victim.invalidated_reason = "capacity"
                    victim.revision += 1
                    # Keep the eviction boundary until marker retention expires.
                    victim.updated_at = datetime.now(UTC)
                await session.flush()
                markers = list(
                    (
                        await session.scalars(
                            select(PromptProjectionModel)
                            .where(
                                PromptProjectionModel.view_key != view_key,
                                PromptProjectionModel.invalidated_reason.is_not(None),
                            )
                            .order_by(
                                PromptProjectionModel.updated_at.desc(),
                                PromptProjectionModel.view_key,
                            )
                        )
                    ).all()
                )
                for marker in markers[self.maximum_views :]:
                    used -= marker.byte_size
                    await session.delete(marker)
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
            row.updated_at = datetime.now(UTC)
            session.add(row)
            await session.commit()
            return _snapshot(row)

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
    )
