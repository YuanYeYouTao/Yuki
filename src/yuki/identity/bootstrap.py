"""Initialize explicitly configured Spaces; Person authority is persistent."""

from __future__ import annotations

from datetime import UTC, datetime

from yuki.config import Settings
from yuki.identity.canonical_repository import ensure_space
from yuki.persistence.database import Database


async def bootstrap_settings_identity(database: Database, settings: Settings) -> None:
    """Create canonical allow-list entries missing from the database."""

    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        for group_id in settings.enabled_groups:
            await ensure_space(session, group_id, enabled=True, now=now)
