"""SQLite lexical candidates for ``search_memory``: FTS5 trigrams plus one weighted scan.

Contract: docs/architecture/memory-v2-retrieval.md "词法打分". Terms of three or more
characters go through the trigram index (ordered by bm25); shorter terms through one
scan over the authorized active facts, ordered by the summed length of the terms each
fact holds. A term found in a person's display name or alias (the whole merged family)
or in a space name adds that subject's facts to the scan window. Both statements keep
``memory_facts`` as the truth source and take at most ``candidate_limit`` IDs each.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import case, column, func, literal, or_, select, table, text, union
from sqlalchemy.exc import DatabaseError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from yuki.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel
from yuki.memory.authorized_scope import AuthorizedMemoryScope, authorized_fact_condition
from yuki.memory.enums import MemoryKind
from yuki.memory.errors import MemoryRetrievalError
from yuki.memory.lexical import LexicalTerms, entity_terms, normalized_names
from yuki.memory.models import (
    MemoryEntityTarget,
    MemoryFact,
    MemoryIndexHealth,
    MemoryTemporalIntent,
)
from yuki.memory.partition import (
    MemoryPartitionResolutionError,
    resolve_fact_canonical_owners,
)
from yuki.memory.temporal_filter import strict_time_conditions
from yuki.persistence.database import Database
from yuki.persistence.models import MemoryFactModel, PersonAliasModel

_FTS = table("memory_facts_fts", column("rowid"))
_ENTITY_WEIGHT = 2  # a name hit weighs like one two-character term in the scan order


class MemoryLexicalIndex(Protocol):
    async def search(
        self,
        target: MemoryEntityTarget,
        terms: LexicalTerms,
        *,
        candidate_limit: int,
        kinds: tuple[MemoryKind, ...] = (),
        short_query_fallback_enabled: bool = True,
        temporal: MemoryTemporalIntent | None = None,
    ) -> tuple[tuple[int, ...], bool]: ...

    async def rebuild(self) -> MemoryIndexHealth: ...

    async def health(self) -> MemoryIndexHealth: ...


class SQLiteMemoryFTSIndex:
    """A replaceable lexical index; memory_facts remains the truth source."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def search_authorized(
        self,
        scope: AuthorizedMemoryScope,
        terms: LexicalTerms,
        *,
        candidate_limit: int,
        kinds: tuple[MemoryKind, ...] = (),
        temporal: MemoryTemporalIntent | None = None,
    ) -> tuple[tuple[int, ...], bool]:
        """Candidate IDs over all SQL-authorized canonical owners, and whether either
        statement reached ``candidate_limit``."""
        try:
            async with self._database.read_session() as session:
                return await _candidates(
                    session,
                    terms,
                    [authorized_fact_condition(scope), *_live(kinds, temporal)],
                    candidate_limit,
                )
        except DatabaseError as exc:
            raise MemoryRetrievalError("memory_index_unavailable") from exc

    async def search(
        self,
        target: MemoryEntityTarget,
        terms: LexicalTerms,
        *,
        candidate_limit: int,
        kinds: tuple[MemoryKind, ...] = (),
        short_query_fallback_enabled: bool = True,
        temporal: MemoryTemporalIntent | None = None,
    ) -> tuple[tuple[int, ...], bool]:
        """The same candidates inside one resolved target."""
        try:
            async with self._database.read_session() as session:
                try:
                    scope = await _target_conditions(session, target)
                except MemoryPartitionResolutionError:
                    return (), False
                return await _candidates(
                    session,
                    terms,
                    [*scope, *_live(kinds, temporal)],
                    candidate_limit,
                    scan=short_query_fallback_enabled,
                )
        except DatabaseError as exc:
            raise MemoryRetrievalError("memory_index_unavailable") from exc

    async def health(self) -> MemoryIndexHealth:
        try:
            async with self._database.read_session() as session:
                row = (
                    (
                        await session.execute(
                            text(
                                """
                            SELECT
                              (SELECT COUNT(*) FROM memory_facts WHERE status = 'active')
                                AS fact_count,
                              (SELECT COUNT(*) FROM memory_facts_fts_docsize)
                                AS indexed_row_count,
                              (SELECT COUNT(*) FROM memory_facts AS mf
                               WHERE mf.status = 'active'
                                 AND NOT EXISTS (
                                   SELECT 1 FROM memory_facts_fts_docsize AS idx
                                   WHERE idx.id = mf.id
                                 )) AS missing_row_count,
                              (SELECT COUNT(*) FROM memory_facts_fts_docsize AS idx
                               WHERE NOT EXISTS (
                                 SELECT 1 FROM memory_facts AS mf WHERE mf.id = idx.id
                               )) AS orphan_row_count
                            """
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
        except DatabaseError as exc:
            raise MemoryRetrievalError("memory_index_unavailable") from exc
        return MemoryIndexHealth(**{key: int(row[key]) for key in row})

    async def rebuild(self) -> MemoryIndexHealth:
        async with self._database.immediate_session() as session:
            await session.execute(
                text("INSERT INTO memory_facts_fts(memory_facts_fts) VALUES ('rebuild')")
            )
        health = await self.health()
        if not health.healthy:
            raise MemoryRetrievalError("memory_index_inconsistent")
        return health


async def subject_names(session: AsyncSession, facts: tuple[MemoryFact, ...]) -> dict[int, str]:
    """Each fact's entity field: its subject person's family names and aliases, and the
    subject space's name. At most two statements whatever the number of facts."""
    persons = {fact.canonical_subject_person_id for fact in facts} - {None}
    spaces = {fact.canonical_subject_space_id for fact in facts} - {None}
    person_names: dict[str, list[str]] = {}
    if persons:
        subject, member = aliased(CanonicalPersonModel), aliased(CanonicalPersonModel)
        root = func.coalesce(subject.merged_into_person_id, subject.id)
        rows = await session.execute(
            select(subject.id, member.display_name, PersonAliasModel.alias)
            .join(member, or_(member.id == root, member.merged_into_person_id == root))
            .outerjoin(PersonAliasModel, PersonAliasModel.canonical_person_id == member.id)
            .where(subject.id.in_(persons))
        )
        for subject_id, name, alias in rows:
            person_names.setdefault(str(subject_id), []).extend((str(name or ""), alias or ""))
    space_names: dict[str, str] = {}
    if spaces:
        space_names = {
            str(space_id): str(name or "")
            for space_id, name in await session.execute(
                select(CanonicalSpaceModel.id, CanonicalSpaceModel.name).where(
                    CanonicalSpaceModel.id.in_(spaces)
                )
            )
        }
    return {
        fact.id: normalized_names(
            [
                *person_names.get(fact.canonical_subject_person_id or "", ()),
                space_names.get(fact.canonical_subject_space_id or "", ""),
            ]
        )
        for fact in facts
    }


def _live(kinds: tuple[MemoryKind, ...], temporal: MemoryTemporalIntent | None) -> list[Any]:
    fact = MemoryFactModel
    conditions: list[Any] = [
        fact.status == "active",
        fact.review_state != "quarantined",
        or_(fact.valid_until.is_(None), fact.valid_until > datetime.now(UTC)),
        *strict_time_conditions(temporal),
    ]
    if kinds:
        conditions.append(fact.kind.in_(kind.value for kind in kinds))
    return conditions


async def _candidates(
    session: AsyncSession,
    terms: LexicalTerms,
    conditions: list[Any],
    limit: int,
    *,
    scan: bool = True,
) -> tuple[tuple[int, ...], bool]:
    fact = MemoryFactModel
    found: list[int] = []
    truncated = False
    if terms.index_terms:
        rows = list(
            await session.scalars(
                select(fact.id)
                .select_from(_FTS.join(fact, fact.id == _FTS.c.rowid))
                .where(*conditions, text("memory_facts_fts MATCH :fts_query"))
                .order_by(text("bm25(memory_facts_fts, 1.0, 4.0, 2.0)"), fact.id)
                .limit(limit + 1),
                {"fts_query": terms.fts_expression},
            )
        )
        truncated = len(rows) > limit
        found.extend(rows[:limit])
    names = entity_terms(terms)
    if scan and (terms.scan_terms or names):
        fields = (fact.normalized_content, fact.memory_key, fact.category, fact.title)
        score: Any = literal(0)
        for term in terms.scan_terms:
            pattern = _pattern(term)
            score = score + case(
                (or_(*(field.like(pattern, escape="\\") for field in fields)), len(term)),
                else_=0,
            )
        if names:
            score = (
                score
                + case(
                    (fact.canonical_subject_person_id.in_(_named_people(names)), _ENTITY_WEIGHT),
                    else_=0,
                )
                + case(
                    (fact.canonical_subject_space_id.in_(_named_spaces(names)), _ENTITY_WEIGHT),
                    else_=0,
                )
            )
        window = (
            select(fact.id.label("id"), fact.importance.label("importance"), score.label("score"))
            .where(*conditions)
            .subquery()
        )
        rows = list(
            await session.scalars(
                select(window.c.id)
                .where(window.c.score > 0)
                .order_by(window.c.score.desc(), window.c.importance.desc(), window.c.id.desc())
                .limit(limit + 1)
            )
        )
        truncated = truncated or len(rows) > limit
        found.extend(rows[:limit])
    return tuple(dict.fromkeys(found)), truncated


def _named_people(names: tuple[str, ...]) -> Any:
    """Every person whose family has a display name or alias containing one of ``names``."""
    person, member = aliased(CanonicalPersonModel), aliased(CanonicalPersonModel)
    root = func.coalesce(member.merged_into_person_id, member.id)
    roots = union(
        select(root).where(_contains(member.display_name, names)),
        select(root)
        .join(PersonAliasModel, PersonAliasModel.canonical_person_id == member.id)
        .where(_contains(PersonAliasModel.alias, names)),
    )
    return select(person.id).where(
        func.coalesce(person.merged_into_person_id, person.id).in_(roots)
    )


def _named_spaces(names: tuple[str, ...]) -> Any:
    return select(CanonicalSpaceModel.id).where(_contains(CanonicalSpaceModel.name, names))


def _contains(field: Any, names: tuple[str, ...]) -> Any:
    return or_(*(field.like(_pattern(name), escape="\\") for name in names))


def _pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def _target_conditions(session: AsyncSession, target: MemoryEntityTarget) -> list[Any]:
    fact = MemoryFactModel
    owners = await resolve_fact_canonical_owners(session, target)
    conditions: list[Any] = [
        fact.scope_type == target.scope_type.value,
        fact.canonical_subject_person_id.is_(None)
        if owners.subject_person_id is None
        else fact.canonical_subject_person_id == owners.subject_person_id,
        fact.canonical_subject_space_id.is_(None)
        if owners.subject_space_id is None
        else fact.canonical_subject_space_id == owners.subject_space_id,
    ]
    if target.scope_type.value != "self":
        conditions.extend(
            (
                fact.visibility_type.is_(None),
                fact.canonical_visibility_person_id.is_(None),
                fact.canonical_visibility_space_id.is_(None),
            )
        )
        return conditions
    visibility_type = target.visibility_type.value if target.visibility_type else ""
    conditions.append(
        or_(
            (fact.visibility_type == "global")
            & fact.canonical_visibility_person_id.is_(None)
            & fact.canonical_visibility_space_id.is_(None),
            (fact.visibility_type == visibility_type)
            & (
                fact.canonical_visibility_person_id.is_(None)
                if owners.visibility_person_id is None
                else fact.canonical_visibility_person_id == owners.visibility_person_id
            )
            & (
                fact.canonical_visibility_space_id.is_(None)
                if owners.visibility_space_id is None
                else fact.canonical_visibility_space_id == owners.visibility_space_id
            ),
        )
    )
    return conditions
