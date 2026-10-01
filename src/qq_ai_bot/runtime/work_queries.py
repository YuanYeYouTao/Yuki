"""Safe Work directory and local recent facts, without execution authority."""

from __future__ import annotations

import math
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, case, func, literal_column, or_, select, text, tuple_

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.canonical_repository import IDENTITY_PLATFORM
from qq_ai_bot.identity.db_models import IdentityBindingModel
from qq_ai_bot.runtime.subagent_schema import children
from qq_ai_bot.runtime.work_query_schema import (
    SOURCE_SCOPE_FIELDS,
    source_scope_column,
    work_statuses,
)
from qq_ai_bot.runtime.work_repository import WorkLease, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import work


class WorkQueries:
    def __init__(self, repository: WorkRepository) -> None:
        self.repository = repository

    @staticmethod
    def _query(lease: WorkLease, source: dict[str, Any], *, local: bool = False) -> Select[Any]:
        creator = source_scope_column("actor_user_id")
        person = func.json_extract(work.c.source_json, literal_column("'$.actor_person_id'"))
        principal = source_scope_column("principal_kind")
        query = (
            select(
                work.c.id.label("work_id"),
                work.c.goal,
                work.c.state,
                work.c.revision,
                work.c.output_kind,
                func.substr(work.c.reason, 1, 128).label("reason"),
                work.c.created.label("created_at"),
                work.c.updated.label("updated_at"),
                work.c.model_requests,
                work.c.tool_calls,
                work.c.sent_messages,
                work.c.conversation_id,
                work.c.generation,
                creator.label("creator_user_id"),
                person.label("creator_person_id"),
                case(
                    (principal == "self", "Yuki / SELF"),
                    else_=IdentityBindingModel.display_name,
                ).label("creator_display_name"),
                case(
                    (principal == "self", "self"),
                    (or_(person != "", creator != ""), "person"),
                    else_="unknown",
                ).label("creator_kind"),
                source_scope_column("origin").label("origin"),
            )
            .select_from(work)
            .outerjoin(
                IdentityBindingModel,
                and_(
                    IdentityBindingModel.person_id == person,
                    IdentityBindingModel.platform == IDENTITY_PLATFORM,
                    IdentityBindingModel.external_account_id == creator,
                ),
            )
        )
        if local or lease.work_id:
            query = query.where(
                work.c.conversation_id == lease.conversation_id,
                work.c.generation == lease.generation,
                select(CanonicalConversationModel.id)
                .where(
                    CanonicalConversationModel.id == lease.conversation_id,
                    CanonicalConversationModel.generation == lease.generation,
                )
                .exists(),
                *(source_scope_column(key).is_(source.get(key)) for key in SOURCE_SCOPE_FIELDS),
            )
        if lease.work_id:
            query = query.where(
                work.c.id == lease.work_id,
                select(children.c.work_id)
                .where(children.c.work_id == work.c.id, children.c.archived_at.is_(None))
                .exists(),
            )
        else:
            query = query.where(
                ~select(children.c.work_id).where(children.c.work_id == work.c.id).exists()
            )
        return query

    async def get(
        self, lease: WorkLease, source: dict[str, Any], work_id: str
    ) -> dict[str, Any] | None:
        async with self.repository.database.sessions() as session:
            row = (
                (await session.execute(self._query(lease, source).where(work.c.id == work_id)))
                .mappings()
                .first()
            )
            return dict(row) if row else None

    async def recent(self, lease: WorkLease, source: dict[str, Any]) -> dict[str, Any] | None:
        """Only this actor/source and current conversation generation enter prompts."""
        async with self.repository.database.sessions() as session:
            row = (
                (
                    await session.execute(
                        self._query(lease, source, local=True)
                        .order_by(work.c.updated.desc(), work.c.id.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    @staticmethod
    def _cursor(cursor: str) -> tuple[float, str]:
        try:
            if type(cursor) is not str or len(cursor) > 256:
                raise ValueError
            updated, identity = cursor.split(":", 1)
            stamp = float(updated)
            if not math.isfinite(stamp) or stamp < 0 or str(UUID(identity)) != identity:
                raise ValueError
            return stamp, identity
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("invalid_work_query_cursor") from exc

    async def list(
        self,
        lease: WorkLease,
        source: dict[str, Any],
        *,
        limit: int = 8,
        status: str = "active",
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("invalid_work_query_limit")
        if status not in {"active", "terminal", "all"}:
            raise ValueError("invalid_work_query_status")
        query = self._query(lease, source)
        if cursor is not None:
            updated, identity = self._cursor(cursor)
            query = query.where(tuple_(work.c.updated, work.c.id) < (updated, identity))
        async with self.repository.database.sessions() as session:
            # Different status pages share one explicit read snapshot. Taking
            # at most N+1 from each disjoint state preserves the exact global
            # top N without sorting every historical row matching an IN list.
            await session.execute(text("BEGIN"))
            candidates = (None,) if status == "all" else work_statuses(status)
            rows: list[dict[str, Any]] = []
            for state in candidates:
                page = query if state is None else query.where(work.c.state == state)
                result = await session.execute(
                    page.order_by(work.c.updated.desc(), work.c.id.desc()).limit(limit + 1)
                )
                rows.extend(dict(row) for row in result.mappings().all())
            rows.sort(key=lambda row: (row["updated_at"], row["work_id"]), reverse=True)
            items = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = f"{last['updated_at']}:{last['work_id']}"
        return {"works": items, "next_cursor": next_cursor}
