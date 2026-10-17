"""Trusted actorless Memory provenance. A target Person never grants private access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import and_, literal, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.autonomy_db_models import InitiativeRunModel
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.identity.db_models import CanonicalSpaceModel, PresenceModel, SpaceBindingModel
from yuki.memory.partition import (
    MemoryPartitionResolutionError,
    format_canonical_memory_partition,
)
from yuki.persistence.unit_of_work import optional_session

if TYPE_CHECKING:
    from yuki.memory.models import MemoryFact
    from yuki.persistence.database import Database


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
                    *(
                        [SpaceBindingModel.id == conversation.space_binding_id]
                        if conversation.space_binding_id is not None
                        else [SpaceBindingModel.external_space_id == group_id]
                        if group_id
                        else []
                    ),
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


async def read_self_initiative_facts(
    database: Database,
    *,
    canonical_conversation_id: str,
    fact_ids: tuple[int, ...],
    session: AsyncSession | None = None,
) -> tuple[MemoryFact, ...]:
    """Read exact saved Memory sources under their original SELF visibility.

    This reader verifies existing run/admission dependencies. It never scans for
    new initiative candidates or grants private Person access from a target.
    """
    from yuki.memory.projections import project_memory_fact_rows
    from yuki.memory.repository import (
        readable_evidence_count_expression,
        readable_evidence_predicate,
    )
    from yuki.persistence.models import MemoryEvidenceModel as e
    from yuki.persistence.models import MemoryFactModel as f

    if not fact_ids:
        return ()
    now = datetime.now(UTC)
    async with optional_session(database, session, write=False) as session:
        conversation = await session.get(CanonicalConversationModel, canonical_conversation_id)
        if (
            conversation is None
            or conversation.kind != "space"
            or not conversation.space_id
            or conversation.person_id is not None
        ):
            return ()
        space_id = conversation.space_id
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
        readable = (
            select(literal(1))
            .select_from(e)
            .where(e.fact_id == f.id, readable_evidence_predicate())
            .correlate(f)
            .exists()
        )
        rows = (
            await session.execute(
                select(f, readable_evidence_count_expression())
                .where(
                    f.id.in_(fact_ids),
                    f.status == "active",
                    f.review_state == "verified",
                    or_(current_group, visible_self),
                    or_(f.valid_from.is_(None), f.valid_from <= now),
                    or_(f.valid_until.is_(None), f.valid_until > now),
                    readable,
                )
                .order_by(f.id)
            )
        ).all()
        return await project_memory_fact_rows(session, ((fact, count) for fact, count in rows))
