"""Repository for bounded web-search source provenance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult

from qq_ai_bot.identity.shadows import conversation_id_for_event
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    WebSearchRunModel,
    WebSearchSourceModel,
)
from qq_ai_bot.web.base import WebSearchError, normalize_public_url
from qq_ai_bot.web.models import WebSearchResponse, WebSearchSource


class WebSearchSourceRepository:
    """Persist real source metadata with strict ConversationIdentity isolation."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def save_response(
        self,
        *,
        conversation_key: str,
        trigger_message_id: str,
        provider: str,
        response: WebSearchResponse,
        max_runs: int,
    ) -> int:
        """Persist one successful tool run and prune older runs in this conversation."""

        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            run = WebSearchRunModel(
                conversation_key=conversation_key[:255],
                trigger_message_id=trigger_message_id[:128],
                query=response.query[:400],
                provider=provider[:32],
                created_at=now,
                partial_failure=response.partial_failure,
            )
            session.add(run)
            await session.flush()
            trigger = await session.scalar(
                select(ChatEventModel.id).where(
                    ChatEventModel.platform_message_id == trigger_message_id[:128]
                )
            )
            proven = await conversation_id_for_event(session, trigger)
            if proven is not None and run.canonical_conversation_id is None:
                run.canonical_conversation_id = proven
            seen: set[str] = set()
            ordinal = 0
            for source in response.sources:
                try:
                    normalized = normalize_public_url(source.url)
                except WebSearchError:
                    continue
                if normalized in seen:
                    continue
                seen.add(normalized)
                ordinal += 1
                session.add(
                    WebSearchSourceModel(
                        run_id=run.id,
                        ordinal=ordinal,
                        title=" ".join(source.title.split())[:512],
                        url=normalized,
                        domain=source.domain[:255],
                        snippet=source.snippet[:1000],
                        published_at=source.published_at,
                        provider_score=source.provider_score,
                        created_at=now,
                    )
                )
            await session.flush()
            old_run_ids = (
                await session.scalars(
                    select(WebSearchRunModel.id)
                    .where(WebSearchRunModel.conversation_key == conversation_key[:255])
                    .order_by(WebSearchRunModel.created_at.desc(), WebSearchRunModel.id.desc())
                    .offset(max_runs)
                )
            ).all()
            if old_run_ids:
                await session.execute(
                    delete(WebSearchRunModel).where(WebSearchRunModel.id.in_(old_run_ids))
                )
            return run.id

    async def for_trigger(
        self,
        *,
        conversation_key: str,
        trigger_message_id: str,
    ) -> tuple[WebSearchSource, ...]:
        """Return only sources used by this trigger in this exact conversation."""

        async with self._database.sessions() as session:
            rows = (
                await session.execute(
                    select(WebSearchSourceModel)
                    .join(
                        WebSearchRunModel,
                        WebSearchSourceModel.run_id == WebSearchRunModel.id,
                    )
                    .where(
                        WebSearchRunModel.conversation_key == conversation_key[:255],
                        WebSearchRunModel.trigger_message_id == trigger_message_id[:128],
                    )
                    .order_by(
                        WebSearchRunModel.created_at.asc(),
                        WebSearchRunModel.id.asc(),
                        WebSearchSourceModel.ordinal.asc(),
                    )
                )
            ).scalars()
            return tuple(self._source_record(row) for row in rows)

    async def latest(self, conversation_key: str) -> tuple[WebSearchSource, ...]:
        """Return sources from the latest successful run in one conversation."""

        async with self._database.sessions() as session:
            run_id = await session.scalar(
                select(WebSearchRunModel.id)
                .join(
                    WebSearchSourceModel,
                    WebSearchSourceModel.run_id == WebSearchRunModel.id,
                )
                .where(WebSearchRunModel.conversation_key == conversation_key[:255])
                .group_by(WebSearchRunModel.id)
                .order_by(WebSearchRunModel.created_at.desc(), WebSearchRunModel.id.desc())
                .limit(1)
            )
            if run_id is None:
                return ()
            rows = (
                await session.scalars(
                    select(WebSearchSourceModel)
                    .where(WebSearchSourceModel.run_id == run_id)
                    .order_by(WebSearchSourceModel.ordinal.asc())
                )
            ).all()
            return tuple(self._source_record(row) for row in rows)

    async def used_url_for_trigger(
        self,
        *,
        conversation_key: str,
        trigger_message_id: str,
        url: str,
    ) -> bool:
        """Return whether a prior web search in this turn produced this URL."""

        normalized = normalize_public_url(url)
        async with self._database.sessions() as session:
            count = await session.scalar(
                select(func.count(WebSearchSourceModel.id))
                .join(
                    WebSearchRunModel,
                    WebSearchSourceModel.run_id == WebSearchRunModel.id,
                )
                .where(
                    WebSearchRunModel.conversation_key == conversation_key[:255],
                    WebSearchRunModel.trigger_message_id == trigger_message_id[:128],
                    WebSearchSourceModel.url == normalized,
                )
            )
            return bool(count)

    async def cleanup_expired(
        self,
        *,
        retention_days: int,
        now: datetime | None = None,
    ) -> int:
        """Delete expired runs; source rows cascade through their foreign key."""

        cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                delete(WebSearchRunModel).where(WebSearchRunModel.created_at < cutoff)
            )
            return int(cast(CursorResult[Any], result).rowcount or 0)

    @staticmethod
    def _source_record(row: WebSearchSourceModel) -> WebSearchSource:
        return WebSearchSource(
            source_id=f"stored-{row.id}",
            title=row.title,
            url=row.url,
            domain=row.domain,
            snippet=row.snippet,
            relevant_content="",
            published_at=row.published_at,
            provider_score=row.provider_score,
        )
