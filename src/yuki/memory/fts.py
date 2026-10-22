"""SQLite FTS5 derived index with subject-first SQL boundaries."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import column, func, literal, literal_column, or_, select, table, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.memory.authorized_scope import AuthorizedMemoryScope, authorized_fact_condition
from yuki.memory.enums import MemoryKind
from yuki.memory.errors import MemoryRetrievalError
from yuki.memory.models import (
    MemoryEntityTarget,
    MemoryIndexHealth,
    MemoryLexicalCandidate,
    MemoryTemporalIntent,
)
from yuki.memory.partition import (
    MemoryPartitionResolutionError,
    resolve_fact_canonical_owners,
)
from yuki.memory.query import normalize_query_text
from yuki.memory.temporal_filter import strict_time_sql
from yuki.persistence.database import Database
from yuki.persistence.models import MemoryFactModel

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class SafeLexicalQuery:
    normalized_text: str
    terms: tuple[str, ...]
    fts_expression: str
    short_term: str | None
    short_terms: tuple[str, ...] = ()

    @property
    def like_terms(self) -> tuple[str, ...]:
        return self.short_terms or ((self.short_term,) if self.short_term else ())


@dataclass(frozen=True, slots=True)
class AuthorizedLexicalCandidate:
    fact_id: int
    fts_rank: float
    exact_match: bool
    matched_terms: tuple[str, ...]


def build_safe_lexical_query(value: str, *, term_limit: int) -> SafeLexicalQuery:
    """Generate quoted FTS terms without accepting user-provided FTS syntax."""

    normalized = normalize_query_text(value)
    terms: list[str] = []
    short_terms: list[str] = []
    for raw in _WORD.findall(unicodedata.normalize("NFKC", normalized)):
        token = raw.casefold()
        if len(token) < 3:
            if token not in short_terms:
                short_terms.append(token)
            if len(terms) + len(short_terms) >= term_limit:
                break
            continue
        has_cjk = any("\u3400" <= character <= "\u9fff" for character in token)
        generated = (
            tuple(token[index : index + 3] for index in range(len(token) - 2))
            if has_cjk and len(token) > 3
            else (token,)
        )
        for term in generated:
            if term not in terms:
                terms.append(term)
            if len(terms) + len(short_terms) >= term_limit:
                break
        if len(terms) + len(short_terms) >= term_limit:
            break
    expression = " OR ".join(f'"{term}"' for term in terms)
    short = normalized if 0 < len(normalized) < 3 else None
    return SafeLexicalQuery(
        normalized_text=normalized,
        terms=tuple((*terms, *short_terms)),
        fts_expression=expression,
        short_term=short,
        short_terms=tuple(short_terms),
    )


class MemoryLexicalIndex(Protocol):
    async def search(
        self,
        target: MemoryEntityTarget,
        query: SafeLexicalQuery,
        *,
        candidate_limit: int,
        kinds: tuple[MemoryKind, ...] = (),
        short_query_fallback_enabled: bool = True,
        temporal: MemoryTemporalIntent | None = None,
    ) -> tuple[MemoryLexicalCandidate, ...]: ...

    async def rebuild(self) -> MemoryIndexHealth: ...

    async def health(self) -> MemoryIndexHealth: ...


class SQLiteMemoryFTSIndex:
    """A replaceable lexical index; memory_facts remains the truth source."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def search_authorized(
        self,
        scope: AuthorizedMemoryScope,
        query: SafeLexicalQuery,
        *,
        candidate_limit: int,
        kinds: tuple[MemoryKind, ...] = (),
        temporal: MemoryTemporalIntent | None = None,
    ) -> tuple[tuple[AuthorizedLexicalCandidate, ...], bool]:
        """One global FTS order over all SQL-authorized canonical owners."""
        if not query.fts_expression and not query.like_terms:
            return (), False
        mf = MemoryFactModel
        conditions = [
            authorized_fact_condition(scope),
            mf.status == "active",
            mf.review_state != "quarantined",
            (mf.valid_until.is_(None) | (mf.valid_until > datetime.now(UTC))),
        ]
        if kinds:
            conditions.append(mf.kind.in_(kind.value for kind in kinds))
        from yuki.memory.temporal_filter import strict_time_conditions

        conditions.extend(strict_time_conditions(temporal))
        fields = (mf.id, mf.memory_key, mf.category, mf.normalized_content)
        fts = table("memory_facts_fts", column("rowid"))
        try:
            async with self._database.read_session() as session:
                rows: list[Any] = []
                if query.fts_expression:
                    statement = (
                        select(
                            *fields,
                            func.bm25(literal_column("memory_facts_fts"), 1.0, 4.0, 2.0).label(
                                "fts_rank"
                            ),
                        )
                        .select_from(fts.join(mf, mf.id == fts.c.rowid))
                        .where(*conditions, text("memory_facts_fts MATCH :fts_query"))
                        .order_by(text("fts_rank ASC"), mf.id.asc())
                        .limit(candidate_limit + 1)
                    )
                    rows.extend(
                        (await session.execute(statement, {"fts_query": query.fts_expression}))
                        .mappings()
                        .all()
                    )
                if query.like_terms:
                    patterns = tuple(f"%{self._escape_like(term)}%" for term in query.like_terms)
                    statement = (
                        select(*fields, literal(1000.0).label("fts_rank"))
                        .where(
                            *conditions,
                            or_(
                                *(
                                    mf.normalized_content.like(pattern, escape="\\")
                                    | mf.memory_key.like(pattern, escape="\\")
                                    | mf.category.like(pattern, escape="\\")
                                    for pattern in patterns
                                )
                            ),
                        )
                        .order_by(mf.id.asc())
                        .limit(candidate_limit + 1)
                    )
                    rows.extend((await session.execute(statement)).mappings().all())
        except DatabaseError as exc:
            raise MemoryRetrievalError("memory_index_unavailable") from exc
        rows_by_id: dict[int, Any] = {}
        for row in rows:
            rows_by_id.setdefault(int(row["id"]), row)
        rows = list(rows_by_id.values())
        truncated = len(rows) > candidate_limit
        candidates = []
        for row in rows[:candidate_limit]:
            key = normalize_query_text(str(row["memory_key"]))
            category = normalize_query_text(str(row["category"]))
            content = normalize_query_text(str(row["normalized_content"]))
            haystack = " ".join((key, category, content))
            candidates.append(
                AuthorizedLexicalCandidate(
                    fact_id=int(row["id"]),
                    fts_rank=float(row["fts_rank"]),
                    exact_match=query.normalized_text in {key, category, content},
                    matched_terms=tuple(term for term in query.terms if term in haystack),
                )
            )
        return tuple(candidates), truncated

    async def search(
        self,
        target: MemoryEntityTarget,
        query: SafeLexicalQuery,
        *,
        candidate_limit: int,
        kinds: tuple[MemoryKind, ...] = (),
        short_query_fallback_enabled: bool = True,
        temporal: MemoryTemporalIntent | None = None,
    ) -> tuple[MemoryLexicalCandidate, ...]:
        if not query.fts_expression and not (query.like_terms and short_query_fallback_enabled):
            return ()
        try:
            async with self._database.read_session() as session:
                try:
                    scope_sql, params = await self._scope_filter(session, target)
                except MemoryPartitionResolutionError:
                    return ()
                params.update(
                    {
                        "now": datetime.now(UTC),
                        "limit": max(1, candidate_limit),
                    }
                )
                time_sql, time_params = strict_time_sql(temporal)
                params.update(time_params)
                kind_sql = ""
                if kinds:
                    placeholders = []
                    for index, kind in enumerate(kinds):
                        name = f"kind_{index}"
                        placeholders.append(f":{name}")
                        params[name] = kind.value
                    kind_sql = f" AND mf.kind IN ({', '.join(placeholders)})"
                rows: list[Any] = []
                if query.fts_expression:
                    fts_params = {**params, "fts_query": query.fts_expression}
                    rows.extend(
                        (
                            await session.execute(
                                text(
                                    """
                                    SELECT mf.id, mf.memory_key, mf.category,
                                           mf.normalized_content,
                                           bm25(memory_facts_fts, 1.0, 4.0, 2.0) AS fts_rank
                                    FROM memory_facts_fts
                                    JOIN memory_facts AS mf
                                      ON mf.id = memory_facts_fts.rowid
                                    WHERE memory_facts_fts MATCH :fts_query
                                      AND mf.status = 'active'
                                      AND (mf.valid_until IS NULL OR mf.valid_until > :now)
                                    """
                                    + scope_sql
                                    + time_sql
                                    + kind_sql
                                    + " ORDER BY fts_rank ASC, mf.id ASC LIMIT :limit"
                                ),
                                fts_params,
                            )
                        ).mappings()
                    )
                if query.like_terms and short_query_fallback_enabled:
                    like_params = dict(params)
                    short_clauses: list[str] = []
                    for index, term in enumerate(query.like_terms):
                        parameter = f"short_term_{index}"
                        like_params[parameter] = f"%{self._escape_like(term)}%"
                        short_clauses.extend(
                            f"mf.{field} LIKE :{parameter} ESCAPE '\\'"
                            for field in ("normalized_content", "memory_key", "category")
                        )
                    rows.extend(
                        (
                            await session.execute(
                                text(
                                    """
                                    SELECT mf.id, mf.memory_key, mf.category,
                                           mf.normalized_content, 1000.0 AS fts_rank
                                    FROM memory_facts AS mf
                                    WHERE mf.status = 'active'
                                      AND (mf.valid_until IS NULL OR mf.valid_until > :now)
                                    """
                                    + " AND ("
                                    + " OR ".join(short_clauses)
                                    + ")"
                                    + scope_sql
                                    + time_sql
                                    + kind_sql
                                    + " ORDER BY mf.id ASC LIMIT :limit"
                                ),
                                like_params,
                            )
                        ).mappings()
                    )
        except DatabaseError as exc:
            raise MemoryRetrievalError("memory_index_unavailable") from exc

        candidates: dict[int, MemoryLexicalCandidate] = {}
        for row in rows:
            fact_id = int(row["id"])
            if fact_id in candidates:
                continue
            key = normalize_query_text(str(row["memory_key"]))
            category = normalize_query_text(str(row["category"]))
            content = normalize_query_text(str(row["normalized_content"]))
            haystack = " ".join((key, category, content))
            matched = tuple(term for term in query.terms if term in haystack)
            exact = query.normalized_text in {key, category, content}
            candidates[fact_id] = MemoryLexicalCandidate(
                fact_id=fact_id,
                target=target,
                fts_rank=float(row["fts_rank"]),
                exact_match=exact,
                matched_terms=matched,
            )
        return tuple(candidates.values())[:candidate_limit]

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
        try:
            async with self._database.immediate_session() as session:
                await session.execute(
                    text("INSERT INTO memory_facts_fts(memory_facts_fts) VALUES ('rebuild')")
                )
        except DatabaseError as exc:
            raise MemoryRetrievalError("memory_index_unavailable") from exc
        health = await self.health()
        if not health.healthy:
            raise MemoryRetrievalError("memory_index_inconsistent")
        return health

    @staticmethod
    async def _scope_filter(
        session: AsyncSession,
        target: MemoryEntityTarget,
    ) -> tuple[str, dict[str, Any]]:
        owners = await resolve_fact_canonical_owners(session, target)
        params: dict[str, Any] = {"scope_type": target.scope_type.value}
        clauses = [" AND mf.scope_type = :scope_type"]
        if owners.subject_person_id is None:
            clauses.append(" AND mf.canonical_subject_person_id IS NULL")
        else:
            clauses.append(" AND mf.canonical_subject_person_id = :subject_person_id")
            params["subject_person_id"] = owners.subject_person_id
        if owners.subject_space_id is None:
            clauses.append(" AND mf.canonical_subject_space_id IS NULL")
        else:
            clauses.append(" AND mf.canonical_subject_space_id = :subject_space_id")
            params["subject_space_id"] = owners.subject_space_id
        if target.scope_type.value == "self":
            clauses.append(
                " AND ((mf.visibility_type = 'global' "
                "AND mf.canonical_visibility_person_id IS NULL "
                "AND mf.canonical_visibility_space_id IS NULL) "
                "OR (mf.visibility_type = :visibility_type"
            )
            params["visibility_type"] = (
                target.visibility_type.value if target.visibility_type else ""
            )
            if owners.visibility_person_id is None:
                clauses.append(" AND mf.canonical_visibility_person_id IS NULL")
            else:
                clauses.append(" AND mf.canonical_visibility_person_id = :visibility_person_id")
                params["visibility_person_id"] = owners.visibility_person_id
            if owners.visibility_space_id is None:
                clauses.append(" AND mf.canonical_visibility_space_id IS NULL))")
            else:
                clauses.append(" AND mf.canonical_visibility_space_id = :visibility_space_id))")
                params["visibility_space_id"] = owners.visibility_space_id
        else:
            clauses.extend(
                (
                    " AND mf.visibility_type IS NULL",
                    " AND mf.canonical_visibility_person_id IS NULL",
                    " AND mf.canonical_visibility_space_id IS NULL",
                )
            )
        return "".join(clauses), params

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
