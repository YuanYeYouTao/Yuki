"""Episode index: what a conversation may recall of its episodes (layered memory P2/P2.1).

Current contract: docs/architecture/memory-v2.md "经历目录". Every Main turn may carry
two deterministic blocks for its conversation:

- the index, the last part of the system message, rendered from the audience's
  ``memory_episode_indexes`` row: long-term impressions (at most ten, any age), the
  recent episodes (the last fifteen of the row's recent list) and per-month counts of
  the rest;
- today's new episodes, inside the current message: visible episodes with an ID above
  the row's ``through_fact_id`` that the index does not list.

The row is written by the nightly Dream step (``memory.episode_selection``) and by the
owner CLI, so the index bytes change at most once a day (plus immediate removals: a
listed episode that is no longer visible disappears, a rewritten one shows its
successor). Without a row the index is empty and the tail starts above the newest
finished Dream run's snapshot. Visibility is P1's evidence rule (``memory.resident``):
private-origin episodes never leave their private chat; groups need every evidence row
to be a direct chat event in that group.

Rendering reads in one session with a constant number of statements (the row, then one
primary-key query ``id IN (listed) OR id > through`` with the eligibility conditions
as columns, then one evidence query), never calls a model and never writes.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from yuki.identity.db_models import CanonicalPersonModel
from yuki.identity.person_identity import person_root_id
from yuki.memory.dream.db_models import MemoryDreamRunModel
from yuki.memory.resident import (
    EvidenceOrigin,
    conversation_audience,
    evidence_origins,
    evidence_origins_where,
    private_evidence_visible,
    space_evidence_visible,
    successor_steps,
)
from yuki.persistence.database import Database
from yuki.persistence.models import (
    MemoryEpisodeIndexModel,
    MemoryEvidenceModel,
    MemoryFactModel,
)
from yuki.time.formatting import local_datetime, stored_utc

MAX_EPISODE_RECENT = 15  # the recent window, by count; no time threshold
MAX_EPISODE_MEMORABLE = 10  # long-term impressions per audience
MAX_EPISODE_RECENT_LINES = 20  # today's new episodes listed; the rest as one count
UNTITLED = "（未命名经历）"
RECENT_HEADER = "今天新记下的经历（还没进入目录）"
MEMORABLE_SECTION = "[长期印象]"
RECENT_SECTION = "[最近的经历]"
REST_SECTION = "[其余]"
FINISHED_RUN_STATUSES = ("completed", "partial_failed")  # watermark; P4 member window

MemorableSource = Literal["dream", "owner"]


@dataclass(frozen=True, slots=True)
class IndexOwner:
    """A conversation audience: a private chat's person root, or a space."""

    kind: Literal["private", "space"]
    owner_id: str

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.owner_id}"


@dataclass(frozen=True, slots=True)
class MemorableEntry:
    fact_id: int
    since: datetime  # when it entered the impressions; kept by successors
    source: MemorableSource

    def to_json(self) -> dict[str, Any]:
        return {"fact_id": self.fact_id, "since": self.since.isoformat(), "source": self.source}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> MemorableEntry:
        source: MemorableSource = "owner" if value["source"] == "owner" else "dream"
        return cls(int(value["fact_id"]), datetime.fromisoformat(str(value["since"])), source)


@dataclass(frozen=True, slots=True)
class IndexRow:
    id: int
    owner: IndexOwner
    recent: tuple[int, ...]  # ascending by sort key; the last 15 render, the rest wait
    memorable: tuple[MemorableEntry, ...]
    months: Mapping[str, int]
    through_fact_id: int
    locked: bool
    revision: int
    as_of: datetime | None
    current: bool = True  # False: a merged person's old row, never read again

    @property
    def window(self) -> tuple[int, ...]:
        return self.recent[-MAX_EPISODE_RECENT:]

    @property
    def pending(self) -> tuple[int, ...]:
        """Leavers not compared yet (input budget); counted by month, never listed."""
        return self.recent[: max(0, len(self.recent) - MAX_EPISODE_RECENT)]

    @property
    def memorable_ids(self) -> tuple[int, ...]:
        return tuple(item.fact_id for item in self.memorable)


@dataclass(frozen=True, slots=True)
class Episode:
    """One fact row with what the index needs; conditions evaluated in SQL as columns."""

    id: int
    title: str | None
    content: str
    day: date  # local date of coalesce(valid_from, created_at)
    active: bool
    eligible: bool  # kind, status, conflict, review, expiry, authority, owner enabled
    supersedes_id: int | None
    scope_type: str
    subject_root: str | None
    subject_space_id: str | None
    visibility_type: str | None
    visibility_root: str | None
    visibility_space_id: str | None

    @property
    def sort_key(self) -> tuple[date, int]:
        return (self.day, self.id)

    @property
    def month(self) -> str:
        return self.day.strftime("%Y-%m")

    def owners(self) -> tuple[IndexOwner, ...]:
        """Audiences whose index may list this episode (ownership only, P2 §5.3).

        Person and person_group episodes belong to the subject's private chat (by
        root); group and person_group episodes to their group; SELF episodes to the
        private chat or group they are visible in; global SELF episodes to none.
        """
        owners: list[IndexOwner] = []
        if self.scope_type in ("person", "person_group") and self.subject_root:
            owners.append(IndexOwner("private", self.subject_root))
        if self.scope_type in ("group", "person_group") and self.subject_space_id:
            owners.append(IndexOwner("space", self.subject_space_id))
        if self.scope_type == "self":
            if self.visibility_type == "private" and self.visibility_root:
                owners.append(IndexOwner("private", self.visibility_root))
            if self.visibility_type == "group" and self.visibility_space_id:
                owners.append(IndexOwner("space", self.visibility_space_id))
        return tuple(owners)


def evidence_visible(owner: IndexOwner, evidence: Sequence[EvidenceOrigin]) -> bool:
    """P1's evidence rule for the audience (the only copy is in ``memory.resident``)."""
    if owner.kind == "private":
        return private_evidence_visible(evidence, owner.owner_id)
    return space_evidence_visible(evidence, owner.owner_id)


def displayable(
    episode: Episode | None,
    owner: IndexOwner,
    origins: Mapping[int, Sequence[EvidenceOrigin]],
) -> bool:
    return (
        episode is not None
        and episode.active
        and episode.eligible
        and owner in episode.owners()
        and evidence_visible(owner, origins.get(episode.id, ()))
    )


@dataclass(frozen=True, slots=True)
class Current:
    """Where a listed ID points now: itself, or the active end of its successor chain."""

    fact_id: int
    same_root: bool  # every step was a rewrite of the same episode (supersedes_id)


@dataclass(frozen=True, slots=True)
class EpisodeIndexRender:
    index_text: str  # end of the system message; empty: no index
    recent_text: str  # inside the current message; empty: none
    index_fact_ids: tuple[int, ...]  # impressions then recent, as listed
    recent_fact_ids: tuple[int, ...]  # today's new episodes listed
    through_fact_id: int  # the row's through, or the global watermark without a row
    memorable_fact_ids: tuple[int, ...] = ()
    window_fact_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class EpisodeWatermark:
    through_fact_id: int
    run_public_id: str | None
    status: str | None
    completed_at: datetime | None


@dataclass(slots=True)
class VisibleEpisodes:
    """Every active episode and, per audience, the visible ones in sort-key order."""

    facts: dict[int, Episode]
    origins: dict[int, list[EvidenceOrigin]]
    by_owner: dict[IndexOwner, list[int]] = field(default_factory=dict)


class EpisodeIndex:
    """Render the episode index and today's new episodes of one conversation."""

    def __init__(self, database: Database, *, timezone: str) -> None:
        self._database = database
        self._timezone = timezone

    async def watermark(self) -> EpisodeWatermark:
        async with self._database.read_session() as session:
            return await _watermark(session)

    async def render_conversation(self, conversation_id: str) -> EpisodeIndexRender:
        async with self._database.read_session() as session:
            owner = await conversation_owner(session, conversation_id)
            row = await load_index_row(session, owner)
            through = (
                row.through_fact_id
                if row is not None
                else (await _watermark(session)).through_fact_id
            )
            memorable = row.memorable_ids if row is not None else ()
            window = row.window if row is not None else ()
            listed = tuple(dict.fromkeys((*memorable, *window)))
            fact = MemoryFactModel
            condition = fact.id > through
            if listed:
                # Only primary-key conditions: with the eligibility conditions in the
                # WHERE clause the planner switches to the status index (P2.1 §11.1).
                condition = or_(fact.id.in_(listed), condition)
            facts = await load_episodes(session, self._timezone, condition)
            current = await resolve_current(
                session, listed, facts, _loader(session, self._timezone, facts)
            )
            wanted = {item.fact_id for item in current.values()}
            wanted.update(
                episode.id
                for episode in facts.values()
                if episode.id > through
                and episode.active
                and episode.eligible
                and owner in episode.owners()
            )
            origins = await evidence_origins(session, wanted)

        def shown(fact_id: int) -> bool:
            return displayable(facts.get(fact_id), owner, origins)

        memorable_ids: list[int] = []
        for listed_id in memorable:
            item = current.get(listed_id)
            if item is not None and shown(item.fact_id) and item.fact_id not in memorable_ids:
                memorable_ids.append(item.fact_id)
        window_ids: list[int] = []
        for listed_id in window:
            item = current.get(listed_id)
            if (
                item is not None
                and item.same_root
                and shown(item.fact_id)
                and item.fact_id not in memorable_ids
                and item.fact_id not in window_ids
            ):
                window_ids.append(item.fact_id)
        memorable_items = sorted((facts[i] for i in memorable_ids), key=lambda e: e.sort_key)
        window_items = sorted((facts[i] for i in window_ids), key=lambda e: e.sort_key)
        indexed = {*memorable_ids, *window_ids}
        tail = sorted(
            (
                episode
                for episode in facts.values()
                if episode.id > through and episode.id not in indexed and shown(episode.id)
            ),
            key=lambda e: e.sort_key,
        )
        as_of = (
            None if row is None or row.as_of is None else local_datetime(row.as_of, self._timezone)
        )
        index_text = _index_text(
            memorable_items, window_items, row.months if row is not None else {}, as_of
        )
        recent_text, recent_ids = _recent_text(tail)
        return EpisodeIndexRender(
            index_text=index_text,
            recent_text=recent_text,
            index_fact_ids=tuple(e.id for e in (*memorable_items, *window_items)),
            recent_fact_ids=recent_ids,
            through_fact_id=through,
            memorable_fact_ids=tuple(e.id for e in memorable_items),
            window_fact_ids=tuple(e.id for e in window_items),
        )


def _index_text(
    memorable: Sequence[Episode],
    window: Sequence[Episode],
    months: Mapping[str, int],
    as_of: datetime | None,
) -> str:
    counted = sorted((month, count) for month, count in months.items() if count > 0)
    if not memorable and not window and not counted:
        return ""
    year = as_of.year if as_of is not None else None
    asof = "" if as_of is None else f"截至 {as_of:%m-%d} 的每日整理。"
    lines = [
        f"经历目录（{asof}每行一件可以想起的事，用 search_memory(fact_id=编号) 读全文；"
        "其余经历只给每月条数，可用 search_memory 的 since/until 按时间查）"
    ]
    if memorable:
        lines.append(MEMORABLE_SECTION)
        lines.extend(_line(item, year) for item in memorable)
    if window:
        lines.append(RECENT_SECTION)
        lines.extend(_line(item, year) for item in window)
    if counted:
        lines.append(REST_SECTION)
        lines.extend(f"- {month}：共 {count} 条" for month, count in counted)
    return "\n".join(lines)


def _recent_text(episodes: Sequence[Episode]) -> tuple[str, tuple[int, ...]]:
    if not episodes:
        return "", ()
    shown = episodes[-MAX_EPISODE_RECENT_LINES:]
    hidden = len(episodes) - len(shown)
    lines = [RECENT_HEADER]
    if hidden:
        lines.append(f"（另有 {hidden} 条，可用 search_memory 查）")
    lines.extend(_line(item, None) for item in shown)
    return "\n".join(lines), tuple(item.id for item in shown)


def _line(episode: Episode, year: int | None) -> str:
    title = " ".join((episode.title or "").split()) or UNTITLED
    day = (
        episode.day.strftime("%m-%d")
        if year is None or episode.day.year == year
        else episode.day.isoformat()
    )
    return f"- {day} {title}（fact:{episode.id}）"


# ------------------------------------------------------------------- reading


async def conversation_owner(session: AsyncSession, conversation_id: str) -> IndexOwner:
    """The audience of a conversation (a private chat resolves its person root)."""
    audience = await conversation_audience(session, conversation_id)
    if audience.kind == "private":
        root = await session.scalar(select(person_root_id(audience.person_id)))
        if root is None:
            raise LookupError(f"unknown person {audience.person_id}")
        return IndexOwner("private", str(root))
    assert audience.space_id is not None
    return IndexOwner("space", audience.space_id)


def _row_statement(owner: IndexOwner) -> Any:
    model = MemoryEpisodeIndexModel
    if owner.kind == "private":
        return select(model).where(
            model.owner_kind == "private", model.owner_person_id == owner.owner_id
        )
    return select(model).where(model.owner_kind == "space", model.owner_space_id == owner.owner_id)


def _row(model: MemoryEpisodeIndexModel, *, current: bool = True) -> IndexRow:
    owner = IndexOwner(
        "private" if model.owner_kind == "private" else "space",
        str(model.owner_person_id if model.owner_kind == "private" else model.owner_space_id),
    )
    return IndexRow(
        id=model.id,
        owner=owner,
        recent=tuple(int(item) for item in json.loads(model.recent_fact_ids_json)),
        memorable=tuple(
            MemorableEntry.from_json(item) for item in json.loads(model.memorable_json)
        ),
        months={str(key): int(value) for key, value in json.loads(model.month_counts_json).items()},
        through_fact_id=model.through_fact_id,
        locked=bool(model.locked),
        revision=model.revision,
        as_of=None if model.as_of is None else stored_utc(model.as_of),
        current=current,
    )


async def load_index_row(session: AsyncSession, owner: IndexOwner) -> IndexRow | None:
    model = await session.scalar(_row_statement(owner))
    return None if model is None else _row(model)


async def load_index_rows(session: AsyncSession) -> list[IndexRow]:
    """Every row, ordered by ID; a merged person's old row is marked not current."""
    model = MemoryEpisodeIndexModel
    person = aliased(CanonicalPersonModel)
    rows = await session.execute(
        select(model, func.coalesce(person.merged_into_person_id, person.id))
        .outerjoin(person, person.id == model.owner_person_id)
        .order_by(model.id)
    )
    return [
        _row(item, current=item.owner_kind != "private" or root == item.owner_person_id)
        for item, root in rows
    ]


async def load_episodes(
    session: AsyncSession, timezone: str, *conditions: Any
) -> dict[int, Episode]:
    """Fact rows with person roots; every index condition comes back as a column."""
    fact = MemoryFactModel
    subject = aliased(CanonicalPersonModel)
    subject_root = aliased(CanonicalPersonModel)
    visible = aliased(CanonicalPersonModel)
    subject_root_id = func.coalesce(subject.merged_into_person_id, subject.id)
    eligible = and_(
        fact.kind == "episode",
        fact.status == "active",
        fact.conflict_state == "clear",
        fact.review_state != "quarantined",
        or_(fact.valid_until.is_(None), fact.valid_until > datetime.now(UTC)),
        fact.authority != "third_party",
        or_(
            fact.scope_type.in_(("group", "self")),
            and_(subject.enabled.is_(True), subject_root.enabled.is_(True)),
        ),
    )
    rows = await session.execute(
        select(
            fact.id,
            fact.title,
            fact.content,
            fact.valid_from,
            fact.created_at,
            (fact.status == "active").label("active"),
            case((eligible, True), else_=False).label("eligible"),
            fact.supersedes_id,
            fact.scope_type,
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
        row.id: Episode(
            id=row.id,
            title=row.title,
            content=row.content,
            day=local_datetime(row.valid_from or row.created_at, timezone).date(),
            active=bool(row.active),
            eligible=bool(row.eligible),
            supersedes_id=row.supersedes_id,
            scope_type=row.scope_type,
            subject_root=row.subject_root,
            subject_space_id=row.canonical_subject_space_id,
            visibility_type=row.visibility_type,
            visibility_root=row.visibility_root,
            visibility_space_id=row.canonical_visibility_space_id,
        )
        for row in rows
    }


def _loader(
    session: AsyncSession, timezone: str, facts: dict[int, Episode]
) -> Callable[[set[int]], Awaitable[None]]:
    async def load(fact_ids: set[int]) -> None:
        missing = fact_ids - facts.keys()
        if missing:
            facts.update(await load_episodes(session, timezone, MemoryFactModel.id.in_(missing)))

    return load


async def resolve_current(
    session: AsyncSession,
    listed: Iterable[int],
    facts: Mapping[int, Episode],
    load: Callable[[set[int]], Awaitable[None]] | None = None,
) -> dict[int, Current]:
    """Map listed IDs to their current active fact (P2.1 §5.2, Q6).

    An active ID maps to itself. Otherwise the chain is walked set by set
    (``successor_steps``: rewrites by ``supersedes_id``, else committed Dream outputs);
    ``same_root`` stays true only while every step is a rewrite of the same episode.
    ``load`` fetches facts the walk reaches; without it ``facts`` must already hold
    every active fact (the nightly step reads all active episodes). IDs without an
    active end of chain are absent. Usually no listed ID needs a walk.
    """
    resolved: dict[int, Current] = {}
    frontier: dict[int, tuple[int, bool]] = {}
    walked: dict[int, set[int]] = {}
    for fact_id in set(listed):
        episode = facts.get(fact_id)
        if episode is not None and episode.active:
            resolved[fact_id] = Current(fact_id, True)
        else:
            frontier[fact_id] = (fact_id, True)
            walked[fact_id] = {fact_id}
    while frontier:
        steps = await successor_steps(session, {at for at, _ in frontier.values()})
        if load is not None:
            await load({step[0] for step in steps.values()})
        advanced: dict[int, tuple[int, bool]] = {}
        for origin, (at, same_root) in frontier.items():
            step = steps.get(at)
            if step is None or step[0] in walked[origin]:
                continue
            following, rewrite = step
            walked[origin].add(following)
            episode = facts.get(following)
            if episode is not None and episode.active:
                resolved[origin] = Current(following, same_root and rewrite)
            else:
                advanced[origin] = (following, same_root and rewrite)
        frontier = advanced
    return resolved


async def visible_episodes(session: AsyncSession, timezone: str) -> VisibleEpisodes:
    """Every active episode and its evidence in two statements; per-audience lists.

    The month recount (P2.1 §6.3, D4) and the nightly step read through here once a
    day: ``status = 'active'`` uses the status index, evidence comes by a subquery.
    """
    fact = MemoryFactModel
    active = and_(fact.kind == "episode", fact.status == "active")
    facts = await load_episodes(session, timezone, active)
    origins = await evidence_origins_where(
        session, MemoryEvidenceModel.fact_id.in_(select(fact.id).where(active))
    )
    result = VisibleEpisodes(facts=facts, origins=origins)
    for episode in sorted(facts.values(), key=lambda item: item.sort_key):
        if not episode.eligible:
            continue
        for owner in episode.owners():
            if evidence_visible(owner, origins.get(episode.id, ())):
                result.by_owner.setdefault(owner, []).append(episode.id)
    return result


def month_counts(
    visible: VisibleEpisodes, owner: IndexOwner, listed: Collection[int]
) -> dict[str, int]:
    """Visible episodes of ``owner`` not rendered in its two lists, by month."""
    return dict(
        sorted(
            Counter(
                visible.facts[fact_id].month
                for fact_id in visible.by_owner.get(owner, ())
                if fact_id not in listed
            ).items()
        )
    )


async def max_fact_id(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.max(MemoryFactModel.id))) or 0)


async def _watermark(session: AsyncSession) -> EpisodeWatermark:
    """The newest finished Dream run: the tail's start for audiences without a row."""
    run = MemoryDreamRunModel
    statement = select(run).where(
        run.completed_at.is_not(None), run.status.in_(FINISHED_RUN_STATUSES)
    )
    row = await session.scalar(statement.order_by(run.completed_at.desc(), run.id.desc()).limit(1))
    if row is None:
        return EpisodeWatermark(0, None, None, None)
    assert row.completed_at is not None
    return EpisodeWatermark(
        row.snapshot_max_fact_id, row.public_id, row.status, stored_utc(row.completed_at)
    )


# ------------------------------------------------------------------- writing


@dataclass(frozen=True, slots=True)
class IndexContent:
    recent: tuple[int, ...]
    memorable: tuple[MemorableEntry, ...]
    months: Mapping[str, int]
    through_fact_id: int
    locked: bool


class IndexConflict(RuntimeError):
    """The row changed (revision or lock) after it was read; nothing was written."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def write_index_row(
    database: Database,
    owner: IndexOwner,
    content: IndexContent,
    *,
    expected: IndexRow | None,
) -> int:
    """One short write transaction: compare, then write the row; returns the revision.

    ``expected`` is the row as read before any model call (``None``: no row then). A
    different revision or lock state, or a row that appeared meanwhile, raises
    ``IndexConflict`` and writes nothing. No model call, evidence or history read here.
    """
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        model = await session.scalar(_row_statement(owner))
        if expected is None:
            if model is not None:
                raise IndexConflict("revision_changed")
            model = MemoryEpisodeIndexModel(
                owner_kind=owner.kind,
                owner_person_id=owner.owner_id if owner.kind == "private" else None,
                owner_space_id=owner.owner_id if owner.kind == "space" else None,
                revision=0,
                created_at=now,
            )
            session.add(model)
        elif model is None or model.revision != expected.revision:
            raise IndexConflict("revision_changed")
        elif bool(model.locked) != expected.locked:
            raise IndexConflict("lock_changed")
        model.recent_fact_ids_json = json.dumps(list(content.recent))
        model.memorable_json = json.dumps([item.to_json() for item in content.memorable])
        model.month_counts_json = json.dumps(dict(content.months), sort_keys=True)
        model.through_fact_id = content.through_fact_id
        model.locked = content.locked
        model.revision = (model.revision or 0) + 1
        model.as_of = now
        model.updated_at = now
        await session.flush()
        return int(model.revision)


async def advance_through(database: Database, rows: Sequence[IndexRow], through: int) -> None:
    """Move unchanged rows' ``through`` forward: one primary-key UPDATE each, no revision."""
    if not rows:
        return
    model = MemoryEpisodeIndexModel
    async with database.immediate_session() as session:
        for row in rows:
            await session.execute(
                update(model)
                .where(
                    model.id == row.id,
                    model.revision == row.revision,
                    model.through_fact_id < through,
                )
                .values(through_fact_id=through)
            )


async def set_locked(database: Database, owner: IndexOwner, locked: bool) -> IndexRow:
    async with database.immediate_session() as session:
        row = await session.scalar(_row_statement(owner))
        if row is None:
            raise LookupError(f"no episode index row for {owner.label}")
        row.locked = locked
        row.updated_at = datetime.now(UTC)
        await session.flush()
        return _row(row)
