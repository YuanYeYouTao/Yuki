"""Trusted actorless Memory provenance. A target Person never grants private access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Select, and_, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.autonomy_db_models import InitiativeRunModel
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.db_models import CanonicalSpaceModel, PresenceModel, SpaceBindingModel
from qq_ai_bot.memory.partition import (
    MemoryPartitionResolutionError,
    format_canonical_memory_partition,
)

if TYPE_CHECKING:
    from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFact
    from qq_ai_bot.persistence.database import Database
    from qq_ai_bot.persistence.models import (
        MemoryEvidenceModel,
        MemoryFactModel,
        MemoryToolReceiptModel,
    )


@dataclass(frozen=True, slots=True)
class SelfMemoryOrigin:
    initiative_run_id: str
    canonical_conversation_id: str
    space_id: str
    presence_id: str
    group_id: str
    bot_user_id: str
    partition: str
    occurred_at: datetime


async def resolve_self_origin(
    session: AsyncSession,
    *,
    initiative_run_id: str,
    canonical_conversation_id: str | None = None,
    require_live: bool = True,
    require_group_projection: bool = True,
) -> SelfMemoryOrigin:
    run = await session.get(InitiativeRunModel, initiative_run_id)
    if run is None or (
        canonical_conversation_id and run.conversation_id != canonical_conversation_id
    ):
        raise MemoryPartitionResolutionError("initiative_source_missing")
    conversation = await session.get(CanonicalConversationModel, run.conversation_id)
    presence = await session.get(PresenceModel, run.presence_id)
    space = await session.get(CanonicalSpaceModel, run.space_id)
    if (
        conversation is None
        or conversation.kind != "space"
        or conversation.space_id != run.space_id
        or conversation.person_id is not None
        or presence is None
        or presence.platform != "qq"
        or space is None
    ):
        raise MemoryPartitionResolutionError("initiative_source_mismatch")
    if require_live and (
        conversation.generation != run.generation
        or not presence.enabled
        or not space.enabled
        or run.state not in {"accepted", "running"}
    ):
        raise MemoryPartitionResolutionError("initiative_source_inactive")
    bindings = (
        list(
            await session.scalars(
                select(SpaceBindingModel).where(
                    SpaceBindingModel.space_id == run.space_id,
                    SpaceBindingModel.platform == presence.platform,
                    SpaceBindingModel.status == "active",
                )
            )
        )
        if require_group_projection
        else []
    )
    if require_group_projection and len(bindings) != 1:
        raise MemoryPartitionResolutionError("initiative_group_binding_ambiguous")
    at = run.created_at
    return SelfMemoryOrigin(
        run.id,
        run.conversation_id,
        run.space_id,
        run.presence_id,
        bindings[0].external_space_id if bindings else "",
        presence.external_account_id,
        format_canonical_memory_partition(space_id=run.space_id),
        at.replace(tzinfo=UTC) if at.tzinfo is None else at,
    )


async def receipt_evidence_readable(
    session: AsyncSession,
    *,
    fact: MemoryFactModel,
    evidence: MemoryEvidenceModel | MemoryEvidenceCreate,
    receipt: MemoryToolReceiptModel,
) -> bool:
    """Historical run evidence remains valid without resurrecting its execution authority."""
    if receipt.initiative_run_id is None or receipt.trigger_event_id is not None:
        return False
    run = await session.get(InitiativeRunModel, receipt.initiative_run_id)
    if run is None:
        return False
    conversation = await session.get(CanonicalConversationModel, run.conversation_id)
    presence = await session.get(PresenceModel, run.presence_id)
    return bool(
        conversation is not None
        and conversation.kind == "space"
        and conversation.space_id == run.space_id
        and conversation.person_id is None
        and presence is not None
        and presence.platform == "qq"
        and presence.external_account_id == receipt.bot_user_id
        and receipt.canonical_space_id == run.space_id
        and receipt.canonical_person_id is None
        and fact.scope_type == "self"
        and fact.canonical_subject_person_id is None
        and fact.canonical_subject_space_id is None
        and fact.canonical_visibility_person_id is None
        and (
            (fact.visibility_type == "global" and fact.canonical_visibility_space_id is None)
            or (
                fact.visibility_type == "group"
                and fact.canonical_visibility_space_id == run.space_id
            )
        )
        and evidence.event_id is None
        and evidence.relation == "agent_reflection"
        and evidence.authority == "agent_reflection"
        and evidence.source_speaker_user_id == receipt.bot_user_id
        and bool(evidence.excerpt and evidence.excerpt.strip())
        and evidence.excerpt in receipt.result_excerpt
    )


def sql_self_receipt_evidence_predicate(
    *, fact: str = "f", evidence: str = "e", receipt: str = "t"
) -> str:
    """SQL equivalent of receipt_evidence_readable, using caller-owned SQL aliases.

    The aliases must be static query identifiers, never user input. The correlated
    EXISTS needs no event join: a silent SELF run has no human event to borrow.
    """
    f, e, t = fact, evidence, receipt
    return (
        f"({t}.initiative_run_id IS NOT NULL AND {t}.trigger_event_id IS NULL "
        f"AND {e}.event_id IS NULL AND {f}.scope_type='self' "
        f"AND {f}.canonical_subject_person_id IS NULL "
        f"AND {f}.canonical_subject_space_id IS NULL "
        f"AND {f}.canonical_visibility_person_id IS NULL "
        f"AND {e}.relation='agent_reflection' AND {e}.authority='agent_reflection' "
        f"AND {e}.source_speaker_user_id={t}.bot_user_id "
        f"AND trim({e}.excerpt)!='' AND instr({t}.result_excerpt,{e}.excerpt)>0 "
        "AND EXISTS (SELECT 1 FROM autonomy_initiative_runs ir "
        "JOIN canonical_conversations ic ON ic.id=ir.conversation_id "
        "JOIN presences ip ON ip.id=ir.presence_id "
        f"WHERE ir.id={t}.initiative_run_id "
        "AND ic.kind='space' AND ic.space_id=ir.space_id AND ic.person_id IS NULL "
        f"AND ip.platform='qq' AND ip.external_account_id={t}.bot_user_id "
        f"AND {t}.canonical_space_id=ir.space_id AND {t}.canonical_person_id IS NULL "
        f"AND (({f}.visibility_type='global' AND {f}.canonical_visibility_space_id IS NULL) "
        f"OR ({f}.visibility_type='group' AND {f}.canonical_visibility_space_id=ir.space_id))))"
    )


async def _seed_space_id(session: AsyncSession, conversation_id: str) -> str | None:
    conversation = await session.get(CanonicalConversationModel, conversation_id)
    if (
        conversation is None
        or conversation.kind != "space"
        or not conversation.space_id
        or conversation.person_id is not None
    ):
        return None
    return conversation.space_id


def _seed_query(space_id: str, *, now: datetime | None = None) -> Select[tuple[MemoryFactModel]]:
    from qq_ai_bot.persistence.models import MemoryFactModel as f

    now = now or datetime.now(UTC)
    current_group = and_(
        f.scope_type == "group",
        f.canonical_subject_space_id == space_id,
        f.canonical_subject_person_id.is_(None),
    )
    visible_self = and_(
        f.scope_type == "self",
        f.canonical_subject_person_id.is_(None),
        f.canonical_subject_space_id.is_(None),
        f.canonical_visibility_person_id.is_(None),
        or_(
            and_(f.visibility_type == "global", f.canonical_visibility_space_id.is_(None)),
            and_(f.visibility_type == "group", f.canonical_visibility_space_id == space_id),
        ),
    )
    return select(f).where(
        f.status == "active",
        f.review_state == "verified",
        or_(current_group, visible_self),
        or_(f.valid_from.is_(None), f.valid_from <= now),
        or_(f.valid_until.is_(None), f.valid_until > now),
    )


async def _readable_seed(
    database: Database,
    session: AsyncSession,
    row: MemoryFactModel,
) -> MemoryFact | None:
    from qq_ai_bot.identity.memory_guard import v2_evidence_row_readable
    from qq_ai_bot.memory.repository import MemoryFactRepository
    from qq_ai_bot.persistence.models import MemoryEvidenceModel

    evidence = await session.scalars(
        select(MemoryEvidenceModel)
        .where(MemoryEvidenceModel.fact_id == row.id)
        .order_by(MemoryEvidenceModel.created_at.desc(), MemoryEvidenceModel.id.desc())
        .limit(32)
    )
    for item in evidence:
        if await v2_evidence_row_readable(session, row, item):
            return await MemoryFactRepository(database).get_fact(row.id, session=session)
    return None


@dataclass(frozen=True, slots=True)
class SelfSeedPage:
    facts: tuple[MemoryFact, ...]
    next_cursor: tuple[str, int]
    scanned: int


async def read_self_seed_page(
    database: Database,
    *,
    canonical_conversation_id: str,
    cursor: tuple[str, int] | None = None,
    limit: int = 4,
    scan_limit: int = 32,
) -> SelfSeedPage:
    """Forward-only change stream over authorized facts, with bounded lineage inspection.

    The Host persists ``next_cursor`` even for empty pages. At most 128 facts and
    32 evidence rows per inspected fact are read. The UTC (change_time, id) cursor
    never wraps, where change_time=max(updated_at, valid_from). Thus previously
    future-dated facts are discovered when they become valid, without replaying
    unchanged old facts. This call's fixed now is the scan ceiling; concurrent
    writes are seen later. New evidence updates fact.updated_at atomically.
    """
    from qq_ai_bot.persistence.models import MemoryFactModel as f

    cursor = cursor or (datetime(1970, 1, 1, tzinfo=UTC).isoformat(), 0)
    cursor_at = datetime.fromisoformat(cursor[0])
    if cursor_at.tzinfo is None or cursor[1] < 0:
        raise ValueError("seed_cursor_requires_utc_time_and_nonnegative_id")
    cursor_at = cursor_at.astimezone(UTC)
    cursor = (cursor_at.isoformat(), cursor[1])
    ceiling = datetime.now(UTC)
    bound = max(1, min(32, limit))
    scan_bound = max(1, min(128, scan_limit))
    change_time = case((f.valid_from > f.updated_at, f.valid_from), else_=f.updated_at)
    async with database.sessions() as session:
        space_id = await _seed_space_id(session, canonical_conversation_id)
        if space_id is None:
            return SelfSeedPage((), cursor, 0)
        query = (
            _seed_query(space_id, now=ceiling)
            .where(
                or_(change_time > cursor_at, and_(change_time == cursor_at, f.id > cursor[1])),
                change_time <= ceiling,
            )
            .order_by(change_time, f.id)
            .limit(scan_bound)
        )
        rows = list(await session.scalars(query))
        selected: list[MemoryFact] = []
        scanned = 0
        for row in rows:
            updated_at = row.updated_at
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            if row.valid_from is not None:
                valid_from = row.valid_from
                if valid_from.tzinfo is None:
                    valid_from = valid_from.replace(tzinfo=UTC)
                updated_at = max(updated_at, valid_from)
            cursor = (updated_at.astimezone(UTC).isoformat(), row.id)
            scanned += 1
            fact = await _readable_seed(database, session, row)
            if fact is not None:
                selected.append(fact)
            if len(selected) >= bound:
                break
        return SelfSeedPage(tuple(selected), cursor, scanned)


async def read_self_seed_candidates(
    database: Database,
    *,
    canonical_conversation_id: str,
    limit: int = 8,
    fact_ids: tuple[int, ...] | None = None,
) -> tuple[MemoryFact, ...]:
    """Bounded priority/exact-ID lookup for source verification, not polling fairness.

    Pollers use ``read_self_seed_page`` and persist its cursor. Both paths share
    the exact scope, lifecycle and readable-lineage checks; neither grants Person
    access from a proposed contact target.
    """
    from qq_ai_bot.persistence.models import MemoryFactModel as f

    bound = max(1, min(32, limit))
    async with database.sessions() as session:
        space_id = await _seed_space_id(session, canonical_conversation_id)
        if space_id is None:
            return ()
        query = _seed_query(space_id)
        if fact_ids is not None:
            if not fact_ids or len(fact_ids) > 32:
                return ()
            query = query.where(f.id.in_(fact_ids))
        rows = await session.scalars(
            query.order_by(f.importance.desc(), f.updated_at.desc(), f.id.desc()).limit(bound * 4)
        )
        selected: list[MemoryFact] = []
        for row in rows:
            fact = await _readable_seed(database, session, row)
            if fact is not None:
                selected.append(fact)
            if len(selected) == bound:
                break
        return tuple(selected)
