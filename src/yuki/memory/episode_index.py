"""Episode index: titles of the episodes a conversation may recall (layered memory P2).

Current contract: docs/architecture/memory-v2.md "经历目录". Every Main turn may carry
two deterministic blocks for its conversation:

- the index, the last part of the system message: one line per visible episode up
  to the watermark (date, title, ``fact:ID``), older ones as per-month counts;
- today's new episodes, inside the current message: visible episodes written after
  the watermark.

The watermark is the ``snapshot_max_fact_id`` of the newest finished Dream run, so
the index changes at most once per Dream batch (plus immediate removals). The
visibility rules are P1's evidence rules (``memory.resident``): private-origin
episodes never leave their private chat, groups need every evidence row to be a
direct chat event in that group.

Rendering reads in one session, never calls a model and never writes.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from yuki.identity.db_models import CanonicalPersonModel
from yuki.identity.person_identity import person_family_condition, person_root_id
from yuki.memory.dream.db_models import MemoryDreamRunModel
from yuki.memory.resident import (
    conversation_audience,
    evidence_origins,
    private_evidence_visible,
    space_evidence_visible,
)
from yuki.persistence.database import Database
from yuki.persistence.models import MemoryFactModel
from yuki.time.formatting import local_datetime, stored_utc

MAX_EPISODE_INDEX_LINES = 120  # listed one per line; older ones become month counts
MAX_EPISODE_RECENT_LINES = 20  # today's new episodes listed; the rest as one count
UNTITLED = "（未命名经历）"
RECENT_HEADER = "今天新记下的经历（还没进入目录）"
_FINISHED = ("completed", "partial_failed")


@dataclass(frozen=True, slots=True)
class EpisodeIndexRender:
    index_text: str  # end of the system message; empty: no index
    recent_text: str  # inside the current message; empty: none
    index_fact_ids: tuple[int, ...]
    recent_fact_ids: tuple[int, ...]
    through_fact_id: int  # watermark used; 0 without a finished Dream run


@dataclass(frozen=True, slots=True)
class EpisodeWatermark:
    through_fact_id: int
    run_public_id: str | None
    status: str | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class _Episode:
    id: int
    title: str | None
    day: datetime  # local coalesce(valid_from, created_at)


class EpisodeIndex:
    """Render the episode index and today's new episodes of one conversation."""

    def __init__(self, database: Database, *, timezone: str) -> None:
        self._database = database
        self._timezone = timezone

    async def watermark(self) -> EpisodeWatermark:
        async with self._database.read_session() as session:
            return await _watermark(session, None)

    async def render_conversation(
        self, conversation_id: str, *, through_fact_id: int | None = None
    ) -> EpisodeIndexRender:
        """``through_fact_id`` replaces the watermark for offline checks (CLI only)."""
        async with self._database.read_session() as session:
            mark = await _watermark(session, through_fact_id)
            through = mark.through_fact_id if through_fact_id is None else through_fact_id
            episodes = await self._visible(session, conversation_id)
        listed = [item for item in episodes if item.id <= through]
        recent = [item for item in episodes if item.id > through]
        index_text, index_ids = self._index(listed, mark.completed_at)
        recent_text, recent_ids = self._recent(recent)
        return EpisodeIndexRender(index_text, recent_text, index_ids, recent_ids, through)

    async def _visible(self, session: AsyncSession, conversation_id: str) -> list[_Episode]:
        audience = await conversation_audience(session, conversation_id)
        fact = MemoryFactModel
        root: str | None = None
        if audience.kind == "private":
            root = await session.scalar(select(person_root_id(audience.person_id)))
            if root is None:
                raise LookupError(f"unknown person {audience.person_id}")
            owner: Any = or_(
                and_(
                    fact.scope_type.in_(("person", "person_group")),
                    person_family_condition(fact.canonical_subject_person_id, root),
                ),
                and_(
                    fact.scope_type == "self",
                    fact.visibility_type == "private",
                    person_family_condition(fact.canonical_visibility_person_id, root),
                ),
            )
        else:
            space = audience.space_id
            owner = or_(
                and_(
                    fact.scope_type.in_(("group", "person_group")),
                    fact.canonical_subject_space_id == space,
                ),
                and_(
                    fact.scope_type == "self",
                    fact.visibility_type == "group",
                    fact.canonical_visibility_space_id == space,
                ),
            )
        subject = aliased(CanonicalPersonModel)
        subject_root = aliased(CanonicalPersonModel)
        rows = (
            await session.execute(
                select(fact.id, fact.title, fact.valid_from, fact.created_at)
                .select_from(fact)
                .outerjoin(subject, subject.id == fact.canonical_subject_person_id)
                .outerjoin(
                    subject_root,
                    subject_root.id == func.coalesce(subject.merged_into_person_id, subject.id),
                )
                .where(
                    owner,
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
            )
        ).all()
        origins = await evidence_origins(session, (row.id for row in rows))

        def visible(fact_id: int) -> bool:
            evidence = origins.get(fact_id, ())
            if audience.kind == "private":
                return private_evidence_visible(evidence, root)
            return space_evidence_visible(evidence, audience.space_id)

        episodes = [
            _Episode(
                row.id,
                row.title,
                local_datetime(row.valid_from or row.created_at, self._timezone),
            )
            for row in rows
            if visible(row.id)
        ]
        return sorted(episodes, key=lambda item: (item.day.date(), item.id))

    def _index(
        self, episodes: list[_Episode], completed_at: datetime | None
    ) -> tuple[str, tuple[int, ...]]:
        if not episodes:
            return "", ()
        shown = episodes[-MAX_EPISODE_INDEX_LINES:]
        counted = Counter(
            item.day.strftime("%Y-%m") for item in episodes[: len(episodes) - len(shown)]
        )
        asof = (
            ""
            if completed_at is None
            else f"截至 {local_datetime(completed_at, self._timezone):%m-%d} 的每日整理。"
        )
        lines = [
            f"经历目录（{asof}每行一件可以想起的事，用 search_memory(fact_id=编号) 读全文；"
            "更早的只给每月条数，可用 search_memory 的 since/until 按时间查）",
            *(f"- {month}：共 {count} 条" for month, count in sorted(counted.items())),
            *(_line(item) for item in shown),
        ]
        return "\n".join(lines), tuple(item.id for item in shown)

    def _recent(self, episodes: list[_Episode]) -> tuple[str, tuple[int, ...]]:
        if not episodes:
            return "", ()
        shown = episodes[-MAX_EPISODE_RECENT_LINES:]
        hidden = len(episodes) - len(shown)
        lines = [RECENT_HEADER]
        if hidden:
            lines.append(f"（另有 {hidden} 条，可用 search_memory 查）")
        lines.extend(_line(item) for item in shown)
        return "\n".join(lines), tuple(item.id for item in shown)


def _line(episode: _Episode) -> str:
    title = " ".join((episode.title or "").split()) or UNTITLED
    return f"- {episode.day:%m-%d} {title}（fact:{episode.id}）"


async def _watermark(session: AsyncSession, through_fact_id: int | None) -> EpisodeWatermark:
    """The newest finished Dream run (at or below ``through_fact_id`` when given)."""
    run = MemoryDreamRunModel
    statement = select(run).where(run.completed_at.is_not(None), run.status.in_(_FINISHED))
    if through_fact_id is not None:
        statement = statement.where(run.snapshot_max_fact_id <= through_fact_id)
    row = await session.scalar(statement.order_by(run.completed_at.desc(), run.id.desc()).limit(1))
    if row is None:
        return EpisodeWatermark(0, None, None, None)
    assert row.completed_at is not None
    return EpisodeWatermark(
        row.snapshot_max_fact_id, row.public_id, row.status, stored_utc(row.completed_at)
    )
