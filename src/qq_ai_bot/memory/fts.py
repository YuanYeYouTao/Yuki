"""SQLite FTS5 derived index with subject-first SQL boundaries."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import column, func, literal_column, select, table, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.memory.authorized_scope import AuthorizedMemoryScope, authorized_fact_condition
from qq_ai_bot.memory.enums import MemoryKind
from qq_ai_bot.memory.errors import MemoryRetrievalError
from qq_ai_bot.memory.models import (
    MemoryEntityTarget,
    MemoryIndexHealth,
    MemoryLexicalCandidate,
    MemoryTemporalIntent,
)
from qq_ai_bot.memory.partition import (
    MemoryPartitionResolutionError,
    resolve_fact_canonical_owners,
)
from qq_ai_bot.memory.query import normalize_query_text
from qq_ai_bot.memory.temporal_filter import strict_time_sql
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import MemoryFactModel

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class SafeLexicalQuery:
    normalized_text: str
    terms: tuple[str, ...]
    fts_expression: str
    short_term: str | None


@dataclass(frozen=True, slots=True)
class AuthorizedLexicalCandidate:
    fact_id: int
    fts_rank: float
    exact_match: bool
    matched_terms: tuple[str, ...]


def build_safe_lexical_query(value: str, *, term_limit: int) -> SafeLexicalQuery:
    """Generate quoted FTS terms without accepting user-provided FTS syntax."""

    if term_limit <= 0:
        raise ValueError("term_limit must be positive")
    normalized = normalize_query_text(value)
    terms: list[str] = []
    for raw in _WORD.findall(unicodedata.normalize("NFKC", normalized)):
        token = raw.casefold()
        if len(token) < 3:
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
    # Preserve coverage across the whole request. Taking the first N trigrams
    # can drop the actual question after a long person/scene preamble.
    if len(terms) > term_limit:
        if term_limit == 1:
            terms = [terms[len(terms) // 2]]
        else:
            last = len(terms) - 1
            terms = [terms[round(index * last / (term_limit - 1))] for index in range(term_limit)]
    expression = " OR ".join(f'"{term}"' for term in terms)
    short = normalized if 0 < len(normalized) < 3 else None
    return SafeLexicalQuery(
        normalized_text=normalized,
        terms=tuple(terms),
        fts_expression=expression,
        short_term=short,
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
        if not query.fts_expression and not query.short_term:
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
        from qq_ai_bot.memory.temporal_filter import strict_time_conditions

        conditions.extend(strict_time_conditions(temporal))
        fields = (mf.id, mf.memory_key, mf.category, mf.normalized_content)
        fts = table("memory_facts_fts", column("rowid"))
        try:
            async with self._database.sessions() as session:
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
                    rows = (
                        (await session.execute(statement, {"fts_query": query.fts_expression}))
                        .mappings()
                        .all()
                    )
                else:
                    assert query.short_term is not None
                    pattern = f"%{self._escape_like(query.short_term)}%"
                    statement = (
                        select(*fields)
                        .where(
                            *conditions,
                            (mf.normalized_content.like(pattern, escape="\\"))
                            | (mf.memory_key.like(pattern, escape="\\"))
                            | (mf.category.like(pattern, escape="\\")),
                        )
                        .order_by(mf.id.asc())
                        .limit(candidate_limit + 1)
                    )
                    rows = (await session.execute(statement)).mappings().all()
        except DatabaseError as exc:
            raise MemoryRetrievalError("memory_index_unavailable") from exc
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
                    fts_rank=float(row["fts_rank"]) if query.fts_expression else 1000.0,
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
        if not query.fts_expression and not (query.short_term and short_query_fallback_enabled):
            return ()
        try:
            async with self._database.sessions() as session:
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
                if query.short_term and short_query_fallback_enabled:
                    like_params = {
                        **params,
                        "pattern": f"%{self._escape_like(query.short_term)}%",
                    }
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
                                      AND (
                                        mf.normalized_content LIKE :pattern ESCAPE '\\'
                                        OR mf.memory_key LIKE :pattern ESCAPE '\\'
                                        OR mf.category LIKE :pattern ESCAPE '\\'
                                      )
                                    """
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
            async with self._database.sessions() as session:
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
            async with self._database.sessions() as session, session.begin():
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
