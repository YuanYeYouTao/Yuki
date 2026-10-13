"""Trusted actorless Memory provenance. A target Person never grants private access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, and_, case, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.autonomy_db_models import InitiativeRunModel
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.db_models import CanonicalSpaceModel, PresenceModel, SpaceBindingModel
from qq_ai_bot.memory.partition import (
    MemoryPartitionResolutionError,
    format_canonical_memory_partition,
)
from qq_ai_bot.persistence.unit_of_work import optional_session

if TYPE_CHECKING:
    from qq_ai_bot.memory.models import MemoryFact
    from qq_ai_bot.persistence.database import Database
    from qq_ai_bot.persistence.models import (
        MemoryFactModel,
    )


@dataclass(frozen=True, slots=True)
class SelfMemoryOrigin:
    initiative_run_id: str | None
    canonical_conversation_id: str
    space_id: str
    presence_id: str
    group_id: str
    bot_user_id: str
    partition: str
    occurred_at: datetime
    generation: int


def self_execution_origin_sql(execution_id: str) -> str:
    """Resolve an exact saved execution or Work, using its original task version.

    The expression is a bound parameter or a caller-owned static SQL column.
    No transport identity or newer automation definition reconstructs this source.
    """
    # SDK boundaries retain the original actor.source_key between the plugin
    # prefix and their final SHA-256 digest. Decode that saved internal anchor;
    # the receipt's execution/call identity remains the SDK boundary itself.
    source_execution = (
        f"(CASE WHEN substr({execution_id},1,7)='plugin:' "
        f"AND instr({execution_id},':execution:')>0 THEN "
        f"substr({execution_id},instr({execution_id},':execution:')+length(':execution:'),"
        f"length({execution_id})-instr({execution_id},':execution:')-length(':execution:')-64) "
        f"ELSE {execution_id} END)"
    )
    step_id = (
        "COALESCE(json_extract(w.source_json,'$.execution_source.step_id'),"
        "json_extract(w.source_json,'$.step_id'))"
    )
    return (
        "SELECT ic.id AS conversation_id, av.canonical_target_space_id AS space_id, "
        "av.canonical_presence_id AS presence_id, ip.external_account_id AS bot_user_id, "
        "json_extract(av.authority_snapshot_json,'$.current_group_id') AS group_id, "
        "ar.created_at AS occurred_at, COALESCE(w.generation,ic.generation) AS generation "
        "FROM automation_runs ar JOIN automation_versions av ON av.id=ar.version_id "
        "JOIN automations a ON a.id=ar.automation_id AND a.id=av.automation_id "
        "JOIN canonical_conversations ic ON "
        "ic.id=json_extract(av.authority_snapshot_json,'$.canonical_conversation_id') "
        "JOIN presences ip ON ip.id=av.canonical_presence_id "
        "JOIN spaces isp ON isp.id=av.canonical_target_space_id "
        f"LEFT JOIN runtime_work w ON w.id={source_execution} "
        "AND w.conversation_id=ic.id "
        "AND COALESCE(json_extract(w.source_json,'$.execution_source.parent_execution_id'),"
        "json_extract(w.source_json,'$.parent_execution_id'))="
        f"CASE WHEN {step_id} IS NULL "
        "THEN printf('automation:%d:%s',ar.id,av.script_hash) "
        "ELSE printf('automation:%d:%s:%s',ar.id,"
        f"{step_id},av.script_hash) END "
        "WHERE a.creator_kind='self' "
        "AND ic.kind='space' AND ic.space_id=av.canonical_target_space_id "
        f"AND ({source_execution}=printf('automation:%d:%s',ar.id,av.script_hash) "
        "OR w.id IS NOT NULL)"
    )


async def resolve_self_origin(
    session: AsyncSession,
    *,
    initiative_run_id: str | None = None,
    execution_id: str | None = None,
    canonical_conversation_id: str | None = None,
    group_id: str | None = None,
    require_live: bool = True,
    require_group_projection: bool = True,
) -> SelfMemoryOrigin:
    if initiative_run_id is not None:
        run = await session.get(InitiativeRunModel, initiative_run_id)
        if run is None:
            raise MemoryPartitionResolutionError("initiative_source_missing")
        conversation_id, space_id, presence_id = run.conversation_id, run.space_id, run.presence_id
        generation, at = run.generation, run.created_at
    else:
        saved = (
            (
                await session.execute(
                    text(self_execution_origin_sql(":execution_id")), {"execution_id": execution_id}
                )
            )
            .mappings()
            .one_or_none()
        )
        if saved is None:
            raise MemoryPartitionResolutionError("self_execution_source_missing")
        conversation_id, space_id, presence_id = (
            saved["conversation_id"],
            saved["space_id"],
            saved["presence_id"],
        )
        generation = saved["generation"]
        at = datetime.fromisoformat(saved["occurred_at"])
        group_id = group_id or saved["group_id"]
    if canonical_conversation_id and conversation_id != canonical_conversation_id:
        raise MemoryPartitionResolutionError("initiative_source_mismatch")
    conversation = await session.get(CanonicalConversationModel, conversation_id)
    presence = await session.get(PresenceModel, presence_id)
    space = await session.get(CanonicalSpaceModel, space_id)
    if (
        conversation is None
        or conversation.kind != "space"
        or conversation.space_id != space_id
        or conversation.person_id is not None
        or presence is None
        or space is None
    ):
        raise MemoryPartitionResolutionError("initiative_source_mismatch")
    if require_live and (
        conversation.generation != generation or not presence.enabled or not space.enabled
    ):
        raise MemoryPartitionResolutionError("initiative_source_inactive")
    bindings = (
        list(
            await session.scalars(
                select(SpaceBindingModel).where(
                    SpaceBindingModel.space_id == space_id,
                    SpaceBindingModel.platform == presence.platform,
                    SpaceBindingModel.status == "active",
                    *([SpaceBindingModel.external_space_id == group_id] if group_id else []),
                )
            )
        )
        if require_group_projection
        else []
    )
    if require_group_projection and len(bindings) != 1:
        raise MemoryPartitionResolutionError("initiative_group_binding_ambiguous")
    return SelfMemoryOrigin(
        initiative_run_id,
        conversation_id,
        space_id,
        presence_id,
        bindings[0].external_space_id if bindings else "",
        presence.external_account_id,
        format_canonical_memory_partition(space_id=space_id),
        at.replace(tzinfo=UTC) if at.tzinfo is None else at,
        generation,
    )


def sql_self_receipt_evidence_predicate(
    *, fact: str = "f", evidence: str = "e", receipt: str = "t"
) -> str:
    """Canonical SELF receipt readability, using caller-owned SQL aliases.

    The aliases must be static query identifiers, never user input. The correlated
    EXISTS needs no event join: a silent SELF run has no human event to borrow.
    """
    f, e, t = fact, evidence, receipt
    # SQLite's default trim removes only ASCII spaces; match the Python
    # source excerpt's str.strip() whitespace set (runtime Python 3.12).
    whitespace = "char(9,10,11,12,13,28,29,30,31,32,133,160,5760," + (
        "8192,8193,8194,8195,8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288)"
    )
    return (
        f"({t}.trigger_event_id IS NULL "
        f"AND {e}.event_id IS NULL AND {f}.scope_type='self' "
        f"AND {f}.canonical_subject_person_id IS NULL "
        f"AND {f}.canonical_subject_space_id IS NULL "
        f"AND {f}.canonical_visibility_person_id IS NULL "
        f"AND {e}.relation='agent_reflection' AND {e}.authority='agent_reflection' "
        f"AND {e}.source_speaker_user_id={t}.bot_user_id "
        f"AND trim({e}.excerpt,{whitespace})!='' AND instr({t}.result_excerpt,{e}.excerpt)>0 "
        "AND (EXISTS (SELECT 1 FROM autonomy_initiative_runs ir "
        "JOIN canonical_conversations ic ON ic.id=ir.conversation_id "
        "JOIN presences ip ON ip.id=ir.presence_id "
        f"WHERE ir.id={t}.initiative_run_id "
        "AND ic.kind='space' AND ic.space_id=ir.space_id AND ic.person_id IS NULL "
        f"AND ip.external_account_id={t}.bot_user_id "
        f"AND {t}.canonical_space_id=ir.space_id AND {t}.canonical_person_id IS NULL "
        f"AND (({f}.visibility_type='global' AND {f}.canonical_visibility_space_id IS NULL) "
        f"OR ({f}.visibility_type='group' AND {f}.canonical_visibility_space_id=ir.space_id))) "
        f"OR ({t}.initiative_run_id IS NULL AND EXISTS ("
        + self_execution_origin_sql(f"{t}.execution_id")
        + f" AND {t}.canonical_space_id=ic.space_id AND {t}.canonical_person_id IS NULL "
        f"AND {t}.bot_user_id=ip.external_account_id "
        f"AND (({f}.visibility_type='global' AND {f}.canonical_visibility_space_id IS NULL) "
        f"OR ({f}.visibility_type='group' AND {f}.canonical_visibility_space_id=ic.space_id))))))"
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


def _seed_query(
    space_id: str, *, now: datetime | None = None
) -> Select[tuple[MemoryFactModel, Any]]:
    from qq_ai_bot.memory.repository import readable_evidence_count_expression
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
    return select(f, readable_evidence_count_expression()).where(
        f.status == "active",
        f.review_state == "verified",
        or_(current_group, visible_self),
        or_(f.valid_from.is_(None), f.valid_from <= now),
        or_(f.valid_until.is_(None), f.valid_until > now),
    )


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
    """Forward-only change stream over authorized facts, with SQL lineage checks.

    The Host persists ``next_cursor`` even for empty pages. The requested scan limit
    bounds each page; SQL checks all source evidence. The UTC (change_time, id) cursor
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
    bound = max(1, limit)
    scan_bound = max(1, scan_limit)
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
        rows = (await session.execute(query)).all()
        selected: list[tuple[MemoryFactModel, Any]] = []
        scanned = 0
        for row, evidence_count in rows:
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
            if evidence_count:
                selected.append((row, evidence_count))
            if len(selected) >= bound:
                break
        from qq_ai_bot.memory.projections import project_memory_fact_rows

        return SelfSeedPage(await project_memory_fact_rows(session, selected), cursor, scanned)


async def read_self_seed_candidates(
    database: Database,
    *,
    canonical_conversation_id: str,
    limit: int = 8,
    fact_ids: tuple[int, ...] | None = None,
    session: AsyncSession | None = None,
) -> tuple[MemoryFact, ...]:
    """Bounded priority/exact-ID lookup for source verification, not polling fairness.

    Pollers use ``read_self_seed_page`` and persist its cursor. Both paths share
    the exact scope, lifecycle and readable-lineage checks; neither grants Person
    access from a proposed contact target.
    """
    from qq_ai_bot.persistence.models import MemoryFactModel as f

    bound = max(1, limit)
    async with optional_session(database, session, write=False) as session:
        space_id = await _seed_space_id(session, canonical_conversation_id)
        if space_id is None:
            return ()
        query = _seed_query(space_id)
        if fact_ids is not None:
            if not fact_ids:
                return ()
            query = query.where(f.id.in_(fact_ids))
        from sqlalchemy import literal

        from qq_ai_bot.memory.projections import project_memory_fact_rows
        from qq_ai_bot.memory.repository import readable_evidence_predicate
        from qq_ai_bot.persistence.models import MemoryEvidenceModel as e

        readable = (
            select(literal(1))
            .select_from(e)
            .where(e.fact_id == f.id, readable_evidence_predicate())
            .correlate(f)
            .exists()
        )
        rows = (
            await session.execute(
                query.where(readable)
                .order_by(f.importance.desc(), f.updated_at.desc(), f.id.desc())
                .limit(bound)
            )
        ).all()
        return await project_memory_fact_rows(session, rows)
