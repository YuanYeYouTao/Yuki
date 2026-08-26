"""Canonical Person erasure keeps shared assets while removing attribution."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from qq_ai_bot.emoji.db_models import EmojiAssetModel, EmojiUsageEventModel
from qq_ai_bot.identity.canonical_repository import ensure_person
from qq_ai_bot.identity.db_models import CanonicalPersonModel
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.people_repository import PeopleRepository


@pytest.mark.asyncio
async def test_forget_anonymizes_shared_emoji_and_deletes_usage(database: Database) -> None:
    now = datetime(2026, 8, 26, tzinfo=UTC)
    asset_id = str(uuid4())
    async with database.sessions() as session, session.begin():
        person_id = await ensure_person(session, "1001", display_name="owner", now=now)
        session.add(
            EmojiAssetModel(
                id=asset_id,
                sha256="a" * 64,
                perceptual_hash=None,
                relative_path="emoji/shared.webp",
                preview_relative_path=None,
                image_format="webp",
                mime_type="image/webp",
                byte_size=1,
                width=1,
                height=1,
                frame_count=1,
                animated=False,
                status="adopted",
                description="shared",
                emotion_tags_json="[]",
                usage_scenarios_json="[]",
                ocr_text="",
                intensity=0.5,
                confidence=1.0,
                analysis_version="test",
                pinned=False,
                source_event_id=None,
                first_seen_user_id="1001",
                first_seen_group_id=None,
                source_sub_type="test",
                source_emoji_id="",
                source_package_id="",
                seen_count=1,
                use_count=1,
                first_seen_at=now,
                last_seen_at=now,
                last_used_at=now,
                missing_since=None,
                created_at=now,
                updated_at=now,
                canonical_first_seen_person_id=person_id,
                canonical_first_seen_space_id=None,
            )
        )
        session.add(
            EmojiUsageEventModel(
                emoji_id=asset_id,
                actor_user_id="1001",
                group_id=None,
                trigger_message_id="fixture-message",
                source="test",
                created_at=now,
                canonical_actor_person_id=person_id,
                canonical_space_id=None,
            )
        )

    assert await PeopleRepository(database).delete_person("1001") is True

    async with database.sessions() as session:
        assert int(await session.scalar(text("PRAGMA foreign_keys")) or 0) == 1
        assert await session.get(CanonicalPersonModel, person_id) is None
        asset = await session.get(EmojiAssetModel, asset_id)
        assert asset is not None
        assert asset.first_seen_user_id is None
        assert asset.canonical_first_seen_person_id is None
        assert await session.scalar(select(func.count()).select_from(EmojiUsageEventModel)) == 0
        assert tuple((await session.execute(text("PRAGMA foreign_key_check"))).all()) == ()
