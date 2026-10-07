"""Empty discovery, bounded pages and changed eligibility use actual SQLite writers."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from qq_ai_bot.persistence.media_repository import MediaAnalysisRepository
from qq_ai_bot.persistence.models import MediaAnalysisModel, WebSearchRunModel
from qq_ai_bot.persistence.web_repository import WebSearchSourceRepository


@pytest.mark.parametrize("kind", ["media", "web"])
async def test_cleanup_has_bounded_page_and_rechecks_after_discovery(database, monkeypatch, kind):
    now = datetime(2026, 10, 1, tzinfo=UTC)
    old = now - timedelta(days=45)
    if kind == "media":
        model = MediaAnalysisModel
        repository = MediaAnalysisRepository(database)
        rows = [
            model(
                content_hash=f"{i:064x}",
                analysis_mode="general",
                provider="test",
                model="test",
                prompt_version="test",
                observation_json="{}",
                created_at=old,
                expires_at=old,
            )
            for i in range(140)
        ]
        primary_key = model.id
        changed_values = {"expires_at": now + timedelta(days=1)}

        async def cleanup():
            return await repository.cleanup_expired(now=now)

    elif kind == "web":
        model = WebSearchRunModel
        repository = WebSearchSourceRepository(database)
        rows = [
            model(
                conversation_key="test",
                trigger_message_id="",
                execution_id="test",
                query="test",
                provider="test",
                created_at=old,
            )
            for _ in range(140)
        ]
        primary_key = model.id
        changed_values = {"created_at": now}

        async def cleanup():
            return await repository.cleanup_expired(retention_days=30, now=now)

    # An empty cleanup must not queue behind another SQLite writer.
    async with database.immediate_session():
        assert await asyncio.wait_for(cleanup(), 1) == 0
    async with database.immediate_session() as session:
        session.add_all(rows)
        await session.flush()
        protected_key = rows[0].id
    original = database.immediate_session
    changed = False

    @asynccontextmanager
    async def changed_before_writer():
        nonlocal changed
        if not changed:
            changed = True
            async with original() as session:
                await session.execute(
                    update(model).where(primary_key == protected_key).values(**changed_values)
                )
        async with original() as session:
            yield session

    monkeypatch.setattr(database, "immediate_session", changed_before_writer)
    assert await cleanup() == 127
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(model)) == 13
    assert await cleanup() == 12
    assert await cleanup() == 0
