"""Bounded receipt finalization using staging metadata, not proposal bodies."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import case, exists, func, select, text, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.canonical_repository import IDENTITY_PLATFORM
from qq_ai_bot.identity.db_models import IdentityBindingModel, SpaceBindingModel
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.memory.eligibility import MemoryEventEligibilityPolicy
from qq_ai_bot.memory.enums import (
    MemoryProcessingSource,
    MemoryRebuildCommitStatus,
    MemoryRebuildItemStatus,
    MemoryRebuildJobOutcome,
    MemoryRebuildReviewStatus,
    MemoryRebuildRunStatus,
    MemoryRebuildThirdPartyMode,
)
from qq_ai_bot.memory.extraction import source_event_fingerprint
from qq_ai_bot.memory.rebuild.models import MemoryRebuildSelection
from qq_ai_bot.memory.rebuild.trusted_sources import (
    changed_trusted_sources,
    prepare_trusted_sources,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryJobModel,
    MemoryRebuildItemModel,
    MemoryRebuildProposalModel,
    MemoryRebuildRunModel,
)
from qq_ai_bot.persistence.repository_helpers import _event_record

READY_STATUSES = (MemoryRebuildItemStatus.STAGED.value, MemoryRebuildItemStatus.NO_CLAIMS.value)


def _unfinished_proposal() -> Any:
    proposal = MemoryRebuildProposalModel
    return (proposal.review_status == MemoryRebuildReviewStatus.PENDING.value) | (
        (proposal.review_status == MemoryRebuildReviewStatus.APPROVED.value)
        & proposal.commit_status.in_(
            (MemoryRebuildCommitStatus.PENDING.value, MemoryRebuildCommitStatus.FAILED.value)
        )
    )


@dataclass(frozen=True, slots=True)
class _Item:
    id: int
    event_id: int
    status: str
    updated_at: datetime
    source_hash: str
    source_token: tuple[Any, ...]
    person_id: str | None
    space_id: str | None
    total: int
    rejected: int
    unfinished: int

    @property
    def outcome(self) -> str:
        if not self.total:
            return MemoryRebuildJobOutcome.NO_CLAIMS.value
        if self.rejected == self.total:
            return MemoryRebuildJobOutcome.ALL_REJECTED.value
        return MemoryRebuildJobOutcome.CLAIMS_APPLIED.value


async def _read_batch(
    session: AsyncSession, run_id: int, item_ids: tuple[int, ...]
) -> tuple[dict[int, _Item], dict[int, Any]]:
    """Three indexed reads, independent of the number of items in the page."""
    proposal = MemoryRebuildProposalModel
    counts = {
        int(row.item_id): row
        for row in await session.execute(
            select(
                proposal.item_id,
                func.count().label("total"),
                func.sum(
                    case(
                        (proposal.review_status == MemoryRebuildReviewStatus.REJECTED.value, 1),
                        else_=0,
                    )
                ).label("rejected"),
                func.sum(case((_unfinished_proposal(), 1), else_=0)).label("unfinished"),
            )
            .where(proposal.item_id.in_(item_ids))
            .group_by(proposal.item_id)
        )
    }
    event = ChatEventModel
    conversation = CanonicalConversationModel
    person_binding = exists(
        select(IdentityBindingModel.id).where(
            IdentityBindingModel.platform == IDENTITY_PLATFORM,
            IdentityBindingModel.external_account_id
            == func.coalesce(
                event.private_peer_user_id,
                case((event.direction == "inbound", event.sender_user_id), else_=None),
            ),
            IdentityBindingModel.person_id == conversation.person_id,
            IdentityBindingModel.status == "active",
        )
    )
    space_binding = exists(
        select(SpaceBindingModel.id).where(
            SpaceBindingModel.platform == IDENTITY_PLATFORM,
            SpaceBindingModel.external_space_id == event.group_id,
            SpaceBindingModel.space_id == conversation.space_id,
            SpaceBindingModel.status == "active",
        )
    )
    rows = await session.execute(
        select(
            MemoryRebuildItemModel.id,
            MemoryRebuildItemModel.event_id,
            MemoryRebuildItemModel.status,
            MemoryRebuildItemModel.updated_at,
            MemoryRebuildItemModel.source_event_hash,
            event.canonical_conversation_id,
            conversation.kind,
            conversation.person_id,
            conversation.space_id,
            conversation.generation,
            conversation.prompt_source_revision,
            person_binding.label("person_bound"),
            space_binding.label("space_bound"),
            event.group_id,
            event.scope_type,
            event.canonical_event_id,
            event.suppression_status,
            event.author_kind,
            event.author_person_id,
            event.author_presence_id,
        )
        .join(event, event.id == MemoryRebuildItemModel.event_id)
        .outerjoin(conversation, conversation.id == event.canonical_conversation_id)
        .where(MemoryRebuildItemModel.run_id == run_id, MemoryRebuildItemModel.id.in_(item_ids))
    )
    items: dict[int, _Item] = {}
    for row in rows:
        aggregate = counts.get(int(row.id))
        person_id = (
            row.person_id
            if row.kind == "private" and not row.group_id and row.person_bound
            else None
        )
        space_id = (
            row.space_id if row.kind == "space" and row.group_id and row.space_bound else None
        )
        items[int(row.id)] = _Item(
            id=int(row.id),
            event_id=int(row.event_id),
            status=str(row.status),
            updated_at=row.updated_at,
            source_hash=str(row.source_event_hash),
            source_token=tuple(row[5:]),
            person_id=person_id,
            space_id=space_id,
            total=int(aggregate.total) if aggregate else 0,
            rejected=int(aggregate.rejected) if aggregate else 0,
            unfinished=int(aggregate.unfinished) if aggregate else 0,
        )
    receipts = {
        int(row.event_id): row
        for row in await session.execute(
            select(
                MemoryJobModel.event_id,
                MemoryJobModel.status,
                MemoryJobModel.processing_source,
                MemoryJobModel.rebuild_run_id,
            ).where(MemoryJobModel.event_id.in_(tuple(item.event_id for item in items.values())))
        )
    }
    return items, receipts


async def complete_receipts(
    database: Database,
    public_id: str,
    *,
    include_failed_live_jobs: bool,
    item_ids: tuple[int, ...] | None,
    limit: int,
) -> int:
    limit = max(1, min(limit, 256))
    if item_ids == ():
        return 0
    async with database.sessions() as reader:
        await reader.execute(text("BEGIN"))
        run = (
            await reader.execute(
                select(
                    MemoryRebuildRunModel.id,
                    MemoryRebuildRunModel.selection_hash,
                    MemoryRebuildRunModel.selection_json,
                ).where(
                    MemoryRebuildRunModel.public_id == public_id,
                    MemoryRebuildRunModel.status == MemoryRebuildRunStatus.COMMITTING.value,
                )
            )
        ).one_or_none()
        if run is None:
            return 0
        discovery = select(MemoryRebuildItemModel.id).where(
            MemoryRebuildItemModel.run_id == run.id,
            MemoryRebuildItemModel.status.in_(READY_STATUSES),
            ~exists(
                select(MemoryRebuildProposalModel.id).where(
                    MemoryRebuildProposalModel.item_id == MemoryRebuildItemModel.id,
                    _unfinished_proposal(),
                )
            ),
        )
        if item_ids is not None:
            discovery = discovery.where(MemoryRebuildItemModel.id.in_(item_ids))
        ids = tuple(
            await reader.scalars(discovery.order_by(MemoryRebuildItemModel.id).limit(limit))
        )
        if not ids:
            return 0
        prepared, _ = await _read_batch(reader, run.id, ids)
        selection = MemoryRebuildSelection.model_validate_json(run.selection_json)
        mode = selection.third_party_mode
        include_failed_live_jobs = include_failed_live_jobs and selection.include_failed_live_jobs
        sources = tuple(
            await reader.scalars(
                select(ChatEventModel).where(
                    ChatEventModel.id.in_(tuple(item.event_id for item in prepared.values()))
                )
            )
        )
        events = tuple(_event_record(source) for source in sources)
        trusted_fences = (
            await prepare_trusted_sources(reader, events)
            if mode is MemoryRebuildThirdPartyMode.TRUSTED_METADATA
            else {}
        )
        # Fingerprints and optional subject hydration stay outside the writer.
        ledger = EventLedgerRepository(database)
        invalid_sources: dict[int, str] = {}
        eligibility = MemoryEventEligibilityPolicy()
        expected_hashes = {item.event_id: item.source_hash for item in prepared.values()}
        for event in events:
            try:
                event = (
                    replace(event, mentioned_user_ids=(), reply_sender_user_id=None)
                    if mode is MemoryRebuildThirdPartyMode.DISABLED
                    else await ledger.hydrate_rebuild_subjects(event)
                )
            except CanonicalIdentityError:
                # A disabled/reclassified trusted reference invalidates this
                # item, not every otherwise-valid source in the page.
                invalid_sources[event.id] = "source_event_changed"
                continue
            if source_event_fingerprint(event) != expected_hashes[event.id]:
                invalid_sources[event.id] = "source_event_changed"
            elif not eligibility.is_eligible(event):
                invalid_sources[event.id] = "event_ineligible_at_commit"
            elif event.canonical_event_id is None or event.canonical_conversation_id is None:
                invalid_sources[event.id] = "source_owner_unavailable"

    now = datetime.now(UTC)
    completed = 0
    async with database.immediate_session() as writer:
        if not await writer.scalar(
            select(MemoryRebuildRunModel.id).where(
                MemoryRebuildRunModel.id == run.id,
                MemoryRebuildRunModel.status == MemoryRebuildRunStatus.COMMITTING.value,
                MemoryRebuildRunModel.selection_hash == run.selection_hash,
            )
        ):
            return 0
        current, receipts = await _read_batch(writer, run.id, ids)
        changed_references = await changed_trusted_sources(writer, trusted_fences)
        for item_id, item in current.items():
            # Recheck the bounded page before its first DML. Concurrent source,
            # staging, review or privacy changes require a fresh preparation.
            if (
                item != prepared.get(item_id)
                or item.unfinished
                or item.status not in READY_STATUSES
                or item.event_id in changed_references
            ):
                continue
            receipt = receipts.get(item.event_id)
            reason: str | None = invalid_sources.get(item.event_id)
            own_done = bool(
                receipt is not None
                and receipt.status == "done"
                and receipt.processing_source == MemoryProcessingSource.REBUILD.value
                and receipt.rebuild_run_id == run.id
            )
            if reason is None and receipt is not None and not own_done:
                if receipt.status == "done":
                    reason = "already_processed"
                elif receipt.status in {"pending", "processing"}:
                    reason = "live_job_active"
                elif receipt.status == "failed" and not include_failed_live_jobs:
                    reason = "failed_live_job_not_selected"
            if reason is None and not own_done:
                if item.person_id is None and item.space_id is None:
                    reason = "source_owner_unavailable"
                else:
                    values = dict(
                        event_id=item.event_id,
                        conversation_key=f"rebuild:{public_id}",
                        canonical_person_id=item.person_id,
                        canonical_space_id=item.space_id,
                        status="done",
                        attempts=0,
                        next_attempt_at=now,
                        created_at=now,
                        updated_at=now,
                        error_category=None,
                        processing_source=MemoryProcessingSource.REBUILD.value,
                        rebuild_run_id=run.id,
                        outcome=item.outcome,
                        completed_at=now,
                    )
                    statement = insert(MemoryJobModel).values(**values)
                    statement = (
                        statement.on_conflict_do_update(
                            index_elements=[MemoryJobModel.event_id],
                            where=MemoryJobModel.status == "failed",
                            set_={
                                key: value
                                for key, value in values.items()
                                if key
                                not in {
                                    "event_id",
                                    "conversation_key",
                                    "attempts",
                                    "next_attempt_at",
                                    "created_at",
                                }
                            },
                        )
                        if include_failed_live_jobs
                        else statement.on_conflict_do_nothing(
                            index_elements=[MemoryJobModel.event_id]
                        )
                    )
                    result = await writer.execute(statement)
                    if cast(CursorResult[Any], result).rowcount != 1:
                        continue
            result = await writer.execute(
                update(MemoryRebuildItemModel)
                .where(
                    MemoryRebuildItemModel.id == item.id,
                    MemoryRebuildItemModel.run_id == run.id,
                    MemoryRebuildItemModel.status == item.status,
                    MemoryRebuildItemModel.updated_at == item.updated_at,
                )
                .values(
                    status="skipped" if reason else "committed",
                    error_category=reason,
                    updated_at=now,
                )
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                raise RuntimeError("rebuild receipt item changed during finalization")
            completed += int(reason is None)
    return completed
