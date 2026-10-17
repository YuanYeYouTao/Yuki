"""SUPERUSERS / ENABLED_GROUPS bootstrap into Person/Space foundation."""

from __future__ import annotations

from datetime import UTC, datetime

from yuki.config import Settings
from yuki.identity.canonical_repository import ensure_person, ensure_space
from yuki.persistence.database import Database


async def bootstrap_settings_identity(database: Database, settings: Settings) -> None:
    """Create canonical allow-list entries missing from the database."""

    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        for user_id in settings.superusers:
            await ensure_person(session, user_id, now=now)
        for group_id in settings.enabled_groups:
            await ensure_space(session, group_id, enabled=True, now=now)
