"""Resident memory: a small deterministic view of active facts in every Main turn.

Current contract: docs/architecture/memory-v2.md "常驻记忆段". Private chats render
the counterpart's explicit facts and SELF principles; group chats render the group
core set and SELF principles. Person core sets were replaced by person profiles
(layered memory P4, ``memory.profiles``). Private-origin facts never render outside
the private chat they came from.

Rendering reads in one session, never calls a model and never writes. Every
filter is re-applied at render time, so stored core sets never need syncing
when facts change.

The evidence classification below (``evidence_origins`` and the per-row predicates) is
shared with the episode index and with the evidence of ``search_memory(fact_id)``
(P4 task book §10).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel
from yuki.identity.person_identity import person_family_condition, person_root_id
from yuki.memory.dream.db_models import (
    MemoryDreamOperationModel,
    MemoryDreamOperationSourceModel,
)
from yuki.persistence.database import Database
from yuki.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemoryFactModel,
    MemoryResidentSetModel,
    MemoryToolReceiptModel,
)

if TYPE_CHECKING:
    from yuki.memory.profiles import RenderedProfile

MAX_RESIDENT_SET = 10  # per group core set and SELF principles; a prompt budget
MAX_RESIDENT_EXPLICIT = 30  # explicit facts rendered; the rest stay on demand

_HEADER = (
    "常驻记忆（少量稳定条目；其余记忆用 search_memory 查，可按 fact 编号读取。"
    "条目之间冲突时以编号大的为准，并说明存在冲突）"
)
_PERSON_SCOPES = ("person", "person_group")


@dataclass(frozen=True, slots=True)
class ResidentAudience:
    """Who the resident segment is rendered for, derived from the conversation row."""

    kind: Literal["private", "space"]
    person_id: str | None  # private: counterpart person (rendering resolves its root)
    space_id: str | None  # space: the group's space ID


@dataclass(frozen=True, slots=True)
class ResidentRender:
    text: str  # empty: this turn has no resident segment
    fact_ids: tuple[int, ...]  # fact IDs of the fact-per-line sections, in order
    omitted_explicit: int  # explicit facts not rendered because of the cap
    profiles: tuple[RenderedProfile, ...] = ()  # person profiles in the segment (P4)


@dataclass(frozen=True, slots=True)
class ResidentOwner:
    """A resident owner. Only space and SELF owners have core sets; a person owner
    only names whose explicit facts a private chat renders."""

    kind: Literal["person", "space", "self"]
    person_id: str | None = None
    space_id: str | None = None


@dataclass(frozen=True, slots=True)
class ResidentSet:
    owner: ResidentOwner
    fact_ids: tuple[int, ...]
    source: Literal["dream", "owner"]
    considered_through_fact_id: int
    revision: int
    selected_at: datetime | None


class ResidentValidationError(ValueError):
    """A core set write that does not satisfy the resident contract."""


@dataclass(frozen=True, slots=True)
class _Fact:
    id: int
    scope_type: str
    content: str
    explicit: bool
    active: bool
    eligible: bool  # common conditions: status, conflict, review, expiry, kind, authority
    subject_root: str | None
    subject_space_id: str | None
    visibility_type: str | None
    visibility_root: str | None
    visibility_space_id: str | None

    def owned_by(self, owner: ResidentOwner) -> bool:
        if owner.kind == "space":
            return self.scope_type == "group" and self.subject_space_id == owner.space_id
        return self.scope_type == "self"


@dataclass(frozen=True, slots=True)
class EvidenceOrigin:
    """Where one evidence row came from: its chat event, or a tool receipt's trigger event.

    ``event_id`` is set only for a direct chat event (``None`` for a tool receipt); the
    conversation fields describe that event, or the receipt's trigger event.
    """

    event_id: int | None
    conversation_kind: str | None
    person_root: str | None
    space_id: str | None
    excerpt: str

    @property
    def direct(self) -> bool:
        return self.event_id is not None


def group_direct_evidence(item: EvidenceOrigin) -> bool:
    """A chat event in any group; all groups count as one public space (P4 §6)."""
    return item.direct and item.conversation_kind not in (None, "private")


def own_private_evidence(item: EvidenceOrigin, person_root: str | None) -> bool:
    """A chat event, or a receipt's trigger event, in the private chat with ``person_root``."""
    return (
        person_root is not None
        and item.conversation_kind == "private"
        and item.person_root == person_root
    )


def foreign_private_evidence(item: EvidenceOrigin, person_root: str | None) -> bool:
    """Someone else's private chat; a row whose conversation cannot be traced counts too."""
    return item.conversation_kind is None or (
        item.conversation_kind == "private" and item.person_root != person_root
    )


def evidence_shown_in(item: EvidenceOrigin, private_root: str | None) -> bool:
    """Whether one evidence row may be quoted in a conversation (P4 §10.2).

    ``private_root`` is the counterpart's root person in a private chat, ``None``
    elsewhere. A direct chat event of any group is shown everywhere; a direct event of
    the private chat with ``private_root`` is shown there too. Tool receipts are never
    shown, only counted.
    """
    return group_direct_evidence(item) or (item.direct and own_private_evidence(item, private_root))


def private_evidence_visible(evidence: Sequence[EvidenceOrigin], person_root: str | None) -> bool:
    """Evidence rule for the private chat with ``person_root`` (shared with the episode index).

    Never another person's private chat. Facts without evidence rows are allowed.
    """
    return not any(foreign_private_evidence(item, person_root) for item in evidence)


def space_evidence_visible(evidence: Sequence[EvidenceOrigin], space_id: str | None) -> bool:
    """Evidence rule for a group: at least one row, each a direct chat event in this space."""
    return bool(evidence) and all(item.direct and item.space_id == space_id for item in evidence)


def public_profile_evidence(evidence: Sequence[EvidenceOrigin], person_root: str) -> bool:
    """Profile rule (P4 §6): a fact ``person_root``'s public profile may cite.

    At least one direct chat event in any group and no row from someone else's
    private chat. Rows from the person's own private chat and group tool receipts do
    not exclude (P4-Q2). Facts without evidence rows never qualify.
    """
    return any(group_direct_evidence(item) for item in evidence) and private_evidence_visible(
        evidence, person_root
    )


def private_profile_evidence(evidence: Sequence[EvidenceOrigin], person_root: str) -> bool:
    """Profile rule (P4 §6): a fact ``person_root``'s private profile may cite.

    Every row is the person's own private chat (direct or a receipt triggered there);
    facts without evidence rows qualify as well.
    """
    return all(own_private_evidence(item, person_root) for item in evidence)


class ResidentMemory:
    """Render resident segments and read/write the per-owner core sets."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def render(self, audience: ResidentAudience) -> ResidentRender:
        async with self._database.read_session() as session:
            return await self._render(session, audience)

    async def render_conversation(self, conversation_id: str) -> ResidentRender:
        async with self._database.read_session() as session:
            return await self._render(
                session, await conversation_audience(session, conversation_id)
            )

    async def load_set(self, owner: ResidentOwner) -> ResidentSet | None:
        async with self._database.read_session() as session:
            owner = await _resolve_owner(session, owner)
            row = await session.scalar(_set_statement(owner))
        return None if row is None else _resident_set(owner, row)

    async def candidate_fact_ids(
        self, owner: ResidentOwner, *, after_fact_id: int = 0
    ) -> tuple[int, ...]:
        """Facts the selection step may choose for ``owner``, ascending by ID."""
        async with self._database.read_session() as session:
            owner = await _resolve_owner(session, owner)
            facts = await load_facts(
                session, _owner_filter(owner), MemoryFactModel.id > after_fact_id
            )
        return tuple(
            fact.id
            for fact in sorted(facts.values(), key=lambda item: item.id)
            if fact.eligible and fact.owned_by(owner) and not fact.explicit
        )

    async def write_set(
        self,
        owner: ResidentOwner,
        fact_ids: Sequence[int],
        *,
        source: Literal["dream", "owner"],
        considered_through_fact_id: int | None = None,
    ) -> ResidentSet:
        wanted = tuple(fact_ids)
        if owner.kind == "self" and source != "owner":
            raise ResidentValidationError("SELF principles are written by the owner only")
        if len(wanted) > MAX_RESIDENT_SET:
            raise ResidentValidationError(f"at most {MAX_RESIDENT_SET} facts per owner")
        if len(set(wanted)) != len(wanted):
            raise ResidentValidationError("duplicate fact IDs")
        async with self._database.read_session() as session:
            owner = await _resolve_owner(session, owner)
            facts = await load_facts(session, MemoryFactModel.id.in_(wanted))
        invalid = [
            fact_id
            for fact_id in wanted
            if fact_id not in facts
            or not facts[fact_id].eligible
            or not facts[fact_id].owned_by(owner)
        ]
        if invalid:
            raise ResidentValidationError(
                "facts not eligible for this owner: " + ",".join(map(str, invalid))
            )
        now = datetime.now(UTC)
        async with self._database.immediate_session() as session:
            row = await session.scalar(_set_statement(owner))
            if row is None:
                row = MemoryResidentSetModel(
                    owner_kind=owner.kind,
                    owner_person_id=owner.person_id,
                    owner_space_id=owner.space_id,
                    fact_ids_json="[]",
                    considered_through_fact_id=0,
                    revision=0,
                    created_at=now,
                )
                session.add(row)
            elif source == "dream" and row.source == "owner":
                # The owner locked this set while the selection's model call ran.
                raise ResidentValidationError("resident_set_locked_by_owner")
            if tuple(json.loads(row.fact_ids_json)) != wanted:
                row.fact_ids_json = json.dumps(list(wanted))
                row.revision += 1
            if considered_through_fact_id is not None:
                row.considered_through_fact_id = considered_through_fact_id
            row.source = source
            row.selected_at = now
            row.updated_at = now
            await session.flush()
            return _resident_set(owner, row)

    async def _render(self, session: AsyncSession, audience: ResidentAudience) -> ResidentRender:
        if audience.kind == "private":
            root = await session.scalar(select(person_root_id(audience.person_id)))
            if root is None:
                raise LookupError(f"unknown person {audience.person_id}")
            owner = ResidentOwner("person", person_id=root)
        else:
            owner = ResidentOwner("space", space_id=audience.space_id)
        self_owner = ResidentOwner("self")
        lists: dict[str, tuple[int, ...]] = {"person": ()}
        for item in (owner, self_owner) if owner.kind == "space" else (self_owner,):
            row = await session.scalar(_set_statement(item))
            lists[item.kind] = () if row is None else tuple(json.loads(row.fact_ids_json))
        explicit: dict[int, _Fact] = {}
        if owner.kind == "person":
            explicit = await load_facts(
                session,
                _owner_filter(owner),
                or_(
                    MemoryFactModel.source_type == "explicit",
                    MemoryFactModel.authority == "explicit",
                ),
            )
        listed, current = await resolve_listed(session, (*lists[owner.kind], *lists["self"]))
        facts = {**listed, **explicit}
        origins = await evidence_origins(
            session, (fact.id for fact in facts.values() if fact.eligible and fact.active)
        )

        def visible(fact: _Fact) -> bool:
            evidence = origins.get(fact.id, ())
            if owner.kind == "person":
                return private_evidence_visible(evidence, owner.person_id)
            return space_evidence_visible(evidence, owner.space_id)

        def self_visible(fact: _Fact) -> bool:
            if fact.visibility_type == "global":
                return True
            if owner.kind == "person":
                return fact.visibility_type == "private" and fact.visibility_root == owner.person_id
            return fact.visibility_type == "group" and fact.visibility_space_id == owner.space_id

        def pick(ids: Iterable[int], section_owner: ResidentOwner) -> list[_Fact]:
            chosen: list[_Fact] = []
            for listed_id in ids:
                fact_id = current.get(listed_id)
                if fact_id is None:
                    continue
                fact = facts[fact_id]
                if (
                    fact.eligible
                    and fact.owned_by(section_owner)
                    and visible(fact)
                    and (section_owner.kind != "self" or self_visible(fact))
                ):
                    chosen.append(fact)
            return chosen

        # (title, candidates in order, cap after dedupe, trailing note)
        sections: list[tuple[str, list[_Fact], int, str]] = []
        omitted = 0
        if owner.kind == "person":
            eligible_explicit = [
                fact
                for fact in sorted(explicit.values(), key=lambda item: item.id)
                if fact.eligible and visible(fact)
            ]
            omitted = max(0, len(eligible_explicit) - MAX_RESIDENT_EXPLICIT)
            note = f"（另有 {omitted} 条明确记忆未常驻，可用 search_memory 查）" if omitted else ""
            sections.append(
                ("[对方明确要求记住的]", eligible_explicit, MAX_RESIDENT_EXPLICIT, note)
            )
        else:
            sections.append(("[本群]", pick(lists["space"], owner), MAX_RESIDENT_SET, ""))
        sections.append(("[我自己的原则]", pick(lists["self"], self_owner), MAX_RESIDENT_SET, ""))

        # Person profiles (layered memory P4 §7) sit before the SELF principles. The
        # import is local: the profile module builds on this module's fact helpers.
        from yuki.memory.profiles import render_private_profiles, render_space_profiles

        if owner.kind == "person":
            assert owner.person_id is not None
            profile_lines, profiles = await render_private_profiles(session, owner.person_id)
        else:
            assert owner.space_id is not None
            profile_lines, profiles = await render_space_profiles(session, owner.space_id)
        seen: set[int] = set()
        lines: list[str] = []
        rendered: list[int] = []
        for title, candidates, cap, note in sections:
            if title == "[我自己的原则]":
                lines.extend(profile_lines)
            entries: list[_Fact] = []
            for fact in candidates:
                if fact.id not in seen and fact.id not in {item.id for item in entries}:
                    entries.append(fact)
            entries = entries[:cap]
            seen.update(fact.id for fact in entries)
            if not entries:
                continue
            lines.append(title)
            lines.extend(_line(fact) for fact in entries)
            rendered.extend(fact.id for fact in entries)
            if note:
                lines.append(note)
        if not lines:
            return ResidentRender("", (), omitted)
        return ResidentRender("\n".join((_HEADER, *lines)), tuple(rendered), omitted, profiles)


def _line(fact: _Fact) -> str:
    return f"- {' '.join(fact.content.split())}（fact:{fact.id}）"


async def _resolve_owner(session: AsyncSession, owner: ResidentOwner) -> ResidentOwner:
    if owner.kind == "self":
        return ResidentOwner("self")
    if owner.kind == "person":
        raise ResidentValidationError("persons have profiles, not core sets")
    if await session.get(CanonicalSpaceModel, owner.space_id) is None:
        raise ResidentValidationError(f"unknown space {owner.space_id}")
    return ResidentOwner("space", space_id=owner.space_id)


def _set_statement(owner: ResidentOwner) -> Any:
    model = MemoryResidentSetModel
    return select(model).where(
        model.owner_kind == owner.kind,
        model.owner_person_id.is_(None)
        if owner.person_id is None
        else model.owner_person_id == owner.person_id,
        model.owner_space_id.is_(None)
        if owner.space_id is None
        else model.owner_space_id == owner.space_id,
    )


def _resident_set(owner: ResidentOwner, row: MemoryResidentSetModel) -> ResidentSet:
    source: Literal["dream", "owner"] = "owner" if row.source == "owner" else "dream"
    return ResidentSet(
        owner=owner,
        fact_ids=tuple(json.loads(row.fact_ids_json)),
        source=source,
        considered_through_fact_id=row.considered_through_fact_id,
        revision=row.revision,
        selected_at=row.selected_at,
    )


def _owner_filter(owner: ResidentOwner) -> Any:
    fact = MemoryFactModel
    if owner.kind == "person":
        return and_(
            fact.scope_type.in_(_PERSON_SCOPES),
            person_family_condition(fact.canonical_subject_person_id, owner.person_id),
        )
    if owner.kind == "space":
        return and_(fact.scope_type == "group", fact.canonical_subject_space_id == owner.space_id)
    return fact.scope_type == "self"


async def load_facts(session: AsyncSession, *conditions: Any) -> dict[int, _Fact]:
    """Fetch facts with person roots and the common resident conditions evaluated in SQL."""
    fact = MemoryFactModel
    subject = aliased(CanonicalPersonModel)
    subject_root = aliased(CanonicalPersonModel)
    visible = aliased(CanonicalPersonModel)
    subject_root_id = func.coalesce(subject.merged_into_person_id, subject.id)
    eligible = and_(
        fact.status == "active",
        fact.conflict_state == "clear",
        fact.review_state != "quarantined",
        or_(fact.valid_until.is_(None), fact.valid_until > datetime.now(UTC)),
        fact.kind.in_(("fact", "preference")),
        or_(
            fact.scope_type.in_(("group", "self")),
            and_(
                fact.authority.in_(("explicit", "self_report")),
                subject.enabled.is_(True),
                subject_root.enabled.is_(True),
            ),
        ),
    )
    rows = await session.execute(
        select(
            fact.id,
            fact.scope_type,
            fact.content,
            or_(fact.source_type == "explicit", fact.authority == "explicit").label("explicit"),
            (fact.status == "active").label("active"),
            case((eligible, True), else_=False).label("eligible"),
            subject_root_id.label("subject_root"),
            fact.canonical_subject_space_id,
            fact.visibility_type,
            func.coalesce(visible.merged_into_person_id, visible.id).label("visibility_root"),
            fact.canonical_visibility_space_id,
        )
        .select_from(fact)
        .outerjoin(subject, subject.id == fact.canonical_subject_person_id)
        .outerjoin(subject_root, subject_root.id == subject_root_id)
        .outerjoin(visible, visible.id == fact.canonical_visibility_person_id)
        .where(*conditions)
    )
    return {
        row.id: _Fact(
            id=row.id,
            scope_type=row.scope_type,
            content=row.content,
            explicit=bool(row.explicit),
            active=bool(row.active),
            eligible=bool(row.eligible),
            subject_root=row.subject_root,
            subject_space_id=row.canonical_subject_space_id,
            visibility_type=row.visibility_type,
            visibility_root=row.visibility_root,
            visibility_space_id=row.canonical_visibility_space_id,
        )
        for row in rows
    }


async def resolve_listed(
    session: AsyncSession, listed: tuple[int, ...]
) -> tuple[dict[int, _Fact], dict[int, int]]:
    """Map each listed ID to its current active fact, walking successors set by set.

    A non-active ID follows the newest fact whose ``supersedes_id`` names it, else
    the newest committed Dream operation output that consumed it. IDs without an
    active end of chain are absent from the returned mapping.
    """
    facts = await load_facts(session, MemoryFactModel.id.in_(set(listed)))
    resolved: dict[int, int] = {}
    frontier: dict[int, int] = {}
    walked: dict[int, set[int]] = {}
    for fact_id in set(listed) & facts.keys():
        if facts[fact_id].active:
            resolved[fact_id] = fact_id
        else:
            frontier[fact_id] = fact_id
            walked[fact_id] = {fact_id}
    while frontier:
        following = {
            fact_id: step[0]
            for fact_id, step in (await successor_steps(session, set(frontier.values()))).items()
        }
        unseen = set(following.values()) - facts.keys()
        if unseen:
            facts.update(await load_facts(session, MemoryFactModel.id.in_(unseen)))
        advanced: dict[int, int] = {}
        for origin, at in frontier.items():
            nxt = following.get(at)
            if nxt is None or nxt in walked[origin]:
                continue
            walked[origin].add(nxt)
            if facts[nxt].active:
                resolved[origin] = nxt
            else:
                advanced[origin] = nxt
        frontier = advanced
    return facts, resolved


async def successor_steps(session: AsyncSession, fact_ids: set[int]) -> dict[int, tuple[int, bool]]:
    """One step along each chain: ``fact_id -> (next ID, found through supersedes_id)``.

    The newest fact whose ``supersedes_id`` names it (a rewrite of the same episode or
    fact, served by ``ix_memory_facts_supersedes_id``), else the newest committed Dream
    operation output that consumed it (merged into or combined with another fact).
    """
    fact = MemoryFactModel
    found: dict[int, tuple[int, bool]] = {
        row[0]: (row[1], True)
        for row in await session.execute(
            select(fact.supersedes_id, func.max(fact.id))
            .where(fact.supersedes_id.in_(fact_ids))
            .group_by(fact.supersedes_id)
        )
    }
    remaining = fact_ids - found.keys()
    if remaining:
        source, operation = MemoryDreamOperationSourceModel, MemoryDreamOperationModel
        rows = await session.execute(
            select(source.fact_id, operation.output_fact_id)
            .join(operation, operation.id == source.operation_id)
            .where(
                source.fact_id.in_(remaining),
                operation.status == "committed",
                operation.output_fact_id.is_not(None),
                operation.output_fact_id != source.fact_id,
            )
            .order_by(operation.id)
        )
        for fact_id, output_id in rows:
            found[fact_id] = (output_id, False)  # ascending operation ID: the newest wins
    return found


async def conversation_audience(session: AsyncSession, conversation_id: str) -> ResidentAudience:
    """Who a conversation's segments are for, from ``canonical_conversations``."""
    row = (
        await session.execute(
            select(
                CanonicalConversationModel.kind,
                CanonicalConversationModel.person_id,
                CanonicalConversationModel.space_id,
            ).where(CanonicalConversationModel.id == conversation_id)
        )
    ).one()
    if row.kind == "private":
        return ResidentAudience("private", row.person_id, None)
    return ResidentAudience("space", None, row.space_id)


async def evidence_origins(
    session: AsyncSession, fact_ids: Iterable[int]
) -> dict[int, list[EvidenceOrigin]]:
    """Every evidence row's conversation for the given facts, in one query."""
    wanted = set(fact_ids)
    if not wanted:
        return {}
    return await evidence_origins_where(session, MemoryEvidenceModel.fact_id.in_(wanted))


async def evidence_origins_where(
    session: AsyncSession, condition: Any
) -> dict[int, list[EvidenceOrigin]]:
    """Evidence origins of the rows matching ``condition`` on ``memory_evidence``.

    ``condition`` may be an ID list or a subquery of fact IDs (``fact_id IN (SELECT
    ...)``), which keeps a batch over many facts to one statement without a bound
    parameter per ID.
    """
    evidence, receipt = MemoryEvidenceModel, MemoryToolReceiptModel
    event, conversation = ChatEventModel, CanonicalConversationModel
    person = aliased(CanonicalPersonModel)
    rows = await session.execute(
        select(
            evidence.fact_id,
            evidence.event_id,
            conversation.kind,
            func.coalesce(person.merged_into_person_id, person.id),
            conversation.space_id,
            evidence.excerpt,
        )
        .select_from(evidence)
        .outerjoin(receipt, receipt.id == evidence.tool_receipt_id)
        .outerjoin(event, event.id == func.coalesce(evidence.event_id, receipt.trigger_event_id))
        .outerjoin(conversation, conversation.id == event.canonical_conversation_id)
        .outerjoin(person, person.id == conversation.person_id)
        .where(condition)
    )
    origins: dict[int, list[EvidenceOrigin]] = {}
    for fact_id, event_id, kind, person_root, space_id, excerpt in rows:
        origins.setdefault(fact_id, []).append(
            EvidenceOrigin(event_id, kind, person_root, space_id, excerpt)
        )
    return origins


async def conversation_evidence(
    session: AsyncSession, fact_id: int, conversation_id: str | None
) -> tuple[list[EvidenceOrigin], int]:
    """Evidence of ``fact_id`` shown in ``conversation_id`` and the count of hidden rows.

    Shown rows ascend by event ID. Outside a private chat only group events are shown.
    """
    conversation = CanonicalConversationModel
    private_root = await session.scalar(
        select(person_root_id(conversation.person_id)).where(
            conversation.id == conversation_id, conversation.kind == "private"
        )
    )
    evidence = (await evidence_origins(session, (fact_id,))).get(fact_id, [])
    shown = sorted(
        (item for item in evidence if evidence_shown_in(item, private_root)),
        key=lambda item: item.event_id or 0,
    )
    return shown, len(evidence) - len(shown)
