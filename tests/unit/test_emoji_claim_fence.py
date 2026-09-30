"""A superseded analysis attempt cannot complete or reset the current claim."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from tests.unit.test_emoji_system import _image_bytes, _runtime

from qq_ai_bot.emoji.db_models import EmojiJobModel
from qq_ai_bot.emoji.lifecycle import EmojiLifecycleService
from qq_ai_bot.emoji.models import EmojiAnalysis, EmojiLifecycleStatus
from qq_ai_bot.emoji.repository import EmojiClaimLostError, EmojiRepository
from qq_ai_bot.emoji.storage import EmojiStorage


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["complete", "fail"])
async def test_late_attempt_cannot_overwrite_reclaimed_job(database, tmp_path, finish):
    repository = EmojiRepository(database)
    media = EmojiStorage(tmp_path / "emoji").inspect(_image_bytes(), near_duplicate_enabled=False)
    asset, _ = await repository.record_candidate(
        media, source_event_id=None, user_id=None, group_id=None
    )
    assert await repository.enqueue(asset.id)
    (old,) = await repository.claim_jobs(worker_id="current", limit=1, lease_seconds=1)
    async with database.immediate_session() as session:
        await session.execute(
            update(EmojiJobModel)
            .where(EmojiJobModel.id == old.id)
            .values(claimed_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    (current,) = await repository.claim_jobs(worker_id="current", limit=1, lease_seconds=30)
    assert old.id == current.id
    with pytest.raises(EmojiClaimLostError):
        await repository.save_analysis(
            asset.id,
            EmojiAnalysis(
                is_emoji=True,
                description="obsolete",
                confidence=0.9,
                animated=False,
                analysis_version="old",
            ),
            status=EmojiLifecycleStatus.RECOGNIZED,
            job=old,
        )
    if finish == "complete":
        assert not await repository.complete_job(old)
    else:
        assert not await repository.fail_job(
            old, error_category="late", max_attempts=3, retry_delay_seconds=0
        )
    async with database.sessions() as session:
        row = await session.scalar(select(EmojiJobModel).where(EmojiJobModel.id == old.id))
        assert row is not None and row.status == "processing" and row.claimed_by == "current"
        assert row.attempts == 0
    assert (await repository.get(asset.id)).description != "obsolete"
    assert await repository.complete_job(current)


@pytest.mark.asyncio
async def test_reclaimed_analysis_cannot_replace_scope_after_external_choice(database, tmp_path):
    repository = EmojiRepository(database)
    storage = EmojiStorage(tmp_path / "emoji")
    assets = []
    for content in (_image_bytes(), _image_bytes("GIF", animated=True)):
        media = storage.inspect(content, near_duplicate_enabled=False)
        asset, _ = await repository.record_candidate(
            media, source_event_id=None, user_id=None, group_id=None
        )
        assets.append(asset)
    retained, candidate = assets
    await repository.adopt_scope(retained.id, scope_type="global")
    assert await repository.enqueue(candidate.id)
    (old,) = await repository.claim_jobs(worker_id="same-worker", limit=1, lease_seconds=1)
    analysis = EmojiAnalysis(
        is_emoji=True,
        description="classified",
        confidence=0.95,
        animated=False,
        analysis_version="current",
    )
    current = None

    async def reclaim_during_choice(candidates, *, mode):
        nonlocal current
        async with database.immediate_session() as session:
            await session.execute(
                update(EmojiJobModel)
                .where(EmojiJobModel.id == old.id)
                .values(claimed_until=datetime.now(UTC) - timedelta(seconds=1))
            )
        (current,) = await repository.claim_jobs(worker_id="same-worker", limit=1, lease_seconds=30)
        # Selection is external work. This newer owner already published its
        # analysis before the old chooser resumes with a valid eviction target.
        await repository.save_analysis(
            candidate.id, analysis, status=EmojiLifecycleStatus.RECOGNIZED, job=current
        )
        return candidates[0]

    lifecycle = EmojiLifecycleService(
        repository, replacement=SimpleNamespace(choose=reclaim_during_choice)
    )
    runtime = _runtime(pool_capacity=1, replacement_mode="llm")
    with pytest.raises(EmojiClaimLostError):
        await lifecycle.apply_analysis(candidate, analysis, runtime=runtime, job=old)
    assert await repository.has_enabled_scope(retained.id, scope_type="global", scope_id="")
    assert not await repository.has_enabled_scope(candidate.id, scope_type="global", scope_id="")
    assert await repository.adopted_count() == 1
    assert not await repository.complete_job(old)
    assert current is not None
    lifecycle = EmojiLifecycleService(
        repository, replacement=SimpleNamespace(choose=AsyncMock(return_value=retained))
    )
    result = await lifecycle.apply_analysis(candidate, analysis, runtime=runtime, job=current)
    assert result.status is EmojiLifecycleStatus.ADOPTED
    assert not await repository.has_enabled_scope(retained.id, scope_type="global", scope_id="")
    assert await repository.adopted_count() == 1
    assert await repository.complete_job(current)
