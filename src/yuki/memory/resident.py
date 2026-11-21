"""Resident memory: a small deterministic view of active facts in every Main turn.

Current contract: docs/architecture/memory-v2.md "常驻记忆段". P1 scope per the
owner's decisions: private chats render the counterpart's explicit facts, the
counterpart's core set and SELF principles; group chats render the group core
set and SELF principles only (no speaker / mention sections). Private-origin
facts never render outside the private chat they came from.

Rendering reads in one session, never calls a model and never writes. Every
filter is re-applied at render time, so stored core sets never need syncing
when facts change.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

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

MAX_RESIDENT_SET = 10  # per owner core set and SELF principles; a prompt budget
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
    fact_ids: tuple[int, ...]  # rendered fact IDs in order of appearance
    omitted_explicit: int  # explicit facts not rendered because of the cap


@dataclass(frozen=True, slots=True)
class ResidentOwner:
    kind: Literal["person", "space", "self"]
    person_id: str | None = None
    space_id: str | None = None


@dataclass(frozen=True, slots=True)
class ResidentSet:
    owner: ResidentOwner  # person owners always carry the root person ID
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
        if owner.kind == "person":
            return self.scope_type in _PERSON_SCOPES and self.subject_root == owner.person_id
        if owner.kind == "space":
            return self.scope_type == "group" and self.subject_space_id == owner.space_id
        return self.scope_type == "self"


@dataclass(frozen=True, slots=True)
class _Origin:
    """Where one evidence row came from; ``direct`` means it names a chat event."""

    direct: bool
    conversation_kind: str | None
    person_root: str | None
    space_id: str | None


class ResidentMemory:
    """Render resident segments and read/write the per-owner core sets."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def render(self, audience: ResidentAudience) -> ResidentRender:
        async with self._database.read_session() as session:
            return await self._render(session, audience)

    async def render_conversation(self, conversation_id: str) -> ResidentRender:
        async with self._database.read_session() as session:
            row = (
                await session.execute(
                    select(
                        CanonicalConversationModel.kind,
                        CanonicalConversationModel.person_id,
                        CanonicalConversationModel.space_id,
                    ).where(CanonicalConversationModel.id == conversation_id)
                )
            ).one()
            audience = (
                ResidentAudience("private", row.person_id, None)
                if row.kind == "private"
                else ResidentAudience("space", None, row.space_id)
            )
            return await self._render(session, audience)

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
            facts = await _facts(session, _owner_filter(owner), MemoryFactModel.id > after_fact_id)
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
            facts = await _facts(session, MemoryFactModel.id.in_(wanted))
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
        lists: dict[str, tuple[int, ...]] = {}
        for item in (owner, self_owner):
            row = await session.scalar(_set_statement(item))
            lists[item.kind] = () if row is None else tuple(json.loads(row.fact_ids_json))
        explicit: dict[int, _Fact] = {}
        if owner.kind == "person":
            explicit = await _facts(
                session,
                _owner_filter(owner),
                or_(
                    MemoryFactModel.source_type == "explicit",
                    MemoryFactModel.authority == "explicit",
                ),
            )
        listed, current = await _resolve_listed(session, (*lists[owner.kind], *lists["self"]))
        facts = {**listed, **explicit}
        origins = await _origins(
            session, (fact.id for fact in facts.values() if fact.eligible and fact.active)
        )

        def visible(fact: _Fact) -> bool:
            evidence = origins.get(fact.id, ())
            if owner.kind == "person":
                # Never another person's private chat; facts without evidence are allowed.
                return not any(
                    item.conversation_kind == "private" and item.person_root != owner.person_id
                    for item in evidence
                )
            # Group: at least one evidence row, each a chat event in this space.
            return bool(evidence) and all(
                item.direct and item.space_id == owner.space_id for item in evidence
            )

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
            sections.append(("[关于对方]", pick(lists["person"], owner), MAX_RESIDENT_SET, ""))
        else:
            sections.append(("[本群]", pick(lists["space"], owner), MAX_RESIDENT_SET, ""))
        sections.append(("[我自己的原则]", pick(lists["self"], self_owner), MAX_RESIDENT_SET, ""))

        seen: set[int] = set()
        lines: list[str] = []
        rendered: list[int] = []
        for title, candidates, cap, note in sections:
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
        return ResidentRender("\n".join((_HEADER, *lines)), tuple(rendered), omitted)


def _line(fact: _Fact) -> str:
    return f"- {' '.join(fact.content.split())}（fact:{fact.id}）"


async def _resolve_owner(session: AsyncSession, owner: ResidentOwner) -> ResidentOwner:
    if owner.kind == "self":
        return ResidentOwner("self")
    if owner.kind == "space":
        if await session.get(CanonicalSpaceModel, owner.space_id) is None:
            raise ResidentValidationError(f"unknown space {owner.space_id}")
        return ResidentOwner("space", space_id=owner.space_id)
    root = await session.scalar(select(person_root_id(owner.person_id)))
    if root is None:
        raise ResidentValidationError(f"unknown person {owner.person_id}")
    return ResidentOwner("person", person_id=root)


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


async def _facts(session: AsyncSession, *conditions: Any) -> dict[int, _Fact]:
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


async def _resolve_listed(
    session: AsyncSession, listed: tuple[int, ...]
) -> tuple[dict[int, _Fact], dict[int, int]]:
    """Map each listed ID to its current active fact, walking successors set by set.

    A non-active ID follows the newest fact whose ``supersedes_id`` names it, else
    the newest committed Dream operation output that consumed it. IDs without an
    active end of chain are absent from the returned mapping.
    """
    facts = await _facts(session, MemoryFactModel.id.in_(set(listed)))
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
        following = await _successors(session, set(frontier.values()))
        unseen = set(following.values()) - facts.keys()
        if unseen:
            facts.update(await _facts(session, MemoryFactModel.id.in_(unseen)))
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


async def _successors(session: AsyncSession, fact_ids: set[int]) -> dict[int, int]:
    fact = MemoryFactModel
    found: dict[int, int] = {
        row[0]: row[1]
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
            found[fact_id] = output_id  # ascending operation ID: the newest wins
    return found


async def _origins(session: AsyncSession, fact_ids: Iterable[int]) -> dict[int, list[_Origin]]:
    """Every evidence row's conversation for the given facts, in one query."""
    wanted = set(fact_ids)
    if not wanted:
        return {}
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
        )
        .select_from(evidence)
        .outerjoin(receipt, receipt.id == evidence.tool_receipt_id)
        .outerjoin(event, event.id == func.coalesce(evidence.event_id, receipt.trigger_event_id))
        .outerjoin(conversation, conversation.id == event.canonical_conversation_id)
        .outerjoin(person, person.id == conversation.person_id)
        .where(evidence.fact_id.in_(wanted))
    )
    origins: dict[int, list[_Origin]] = {}
    for fact_id, event_id, kind, person_root, space_id in rows:
        origins.setdefault(fact_id, []).append(
            _Origin(event_id is not None, kind, person_root, space_id)
        )
    return origins
