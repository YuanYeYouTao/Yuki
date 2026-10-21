"""Initialize explicitly configured Spaces; Person authority is persistent."""

from __future__ import annotations

from datetime import UTC, datetime

from yuki.config import Settings
from yuki.identity.canonical_repository import create_space_binding, find_space_binding
from yuki.persistence.database import Database


async def bootstrap_settings_identity(database: Database, settings: Settings) -> None:
    """Create canonical allow-list entries missing from the database."""

    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        for group_id in settings.enabled_groups:
            if await find_space_binding(session, group_id, platform="qq") is None:
                await create_space_binding(
                    session,
                    external_space_id=group_id,
                    platform="qq",
                    name="",
                    enabled=True,
                    autonomous_enabled=True,
                    require_mention=True,
                    now=now,
                )
