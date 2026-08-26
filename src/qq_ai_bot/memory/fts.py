"""SQLite FTS5 derived index with subject-first SQL boundaries."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.memory.enums import MemoryKind
from qq_ai_bot.memory.errors import MemoryRetrievalError
from qq_ai_bot.memory.models import (
    MemoryEntityTarget,
    MemoryIndexHealth,
    MemoryLexicalCandidate,
)
from qq_ai_bot.memory.partition import (
    MemoryPartitionResolutionError,
    resolve_fact_canonical_owners,
)
from qq_ai_bot.memory.query import normalize_query_text
from qq_ai_bot.persistence.database import Database

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class SafeLexicalQuery:
    normalized_text: str
    terms: tuple[str, ...]
    fts_expression: str
    short_term: str | None


def build_safe_lexical_query(value: str, *, term_limit: int) -> SafeLexicalQuery:
    """Generate quoted FTS terms without accepting user-provided FTS syntax."""

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
            if len(terms) >= term_limit:
                break
        if len(terms) >= term_limit:
            break
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
    ) -> tuple[MemoryLexicalCandidate, ...]: ...

    async def rebuild(self) -> MemoryIndexHealth: ...

    async def health(self) -> MemoryIndexHealth: ...


class SQLiteMemoryFTSIndex:
    """A replaceable lexical index; memory_facts remains the truth source."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def search(
        self,
        target: MemoryEntityTarget,
        query: SafeLexicalQuery,
        *,
        candidate_limit: int,
        kinds: tuple[MemoryKind, ...] = (),
        short_query_fallback_enabled: bool = True,
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
