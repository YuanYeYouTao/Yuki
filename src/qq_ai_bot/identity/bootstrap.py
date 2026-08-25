"""SUPERUSERS / ENABLED_GROUPS bootstrap into Person/Space foundation."""

from __future__ import annotations

from datetime import UTC, datetime

from qq_ai_bot.config import Settings
from qq_ai_bot.identity.dual_write import (
    ensure_canonical_person_preconfig,
    ensure_canonical_space_preconfig,
    sync_account,
    sync_space,
)
from qq_ai_bot.identity.runtime import load_identity_runtime
from qq_ai_bot.persistence.database import Database


async def bootstrap_settings_identity(database: Database, settings: Settings) -> None:
    """Create missing Person/Binding and Space/Binding for configured allowlists.

    v1 Settings that have not landed in legacy people/groups must only create
    canonical Person/IdentityBinding and Space/SpaceBinding. They must not
    insert people, groups, or conversation_scopes.
    """

    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        runtime = await load_identity_runtime(session)
        if runtime.state != "v1" and not runtime.complete_v2:
            return
        if runtime.state == "v1":
            for user_id in settings.superusers:
                await sync_account(session, user_id, role="human", now=now)
            for group_id in settings.enabled_groups:
                await sync_space(session, group_id, enabled=True, now=now)
            return
        for user_id in settings.superusers:
            await ensure_canonical_person_preconfig(session, user_id, now=now)
        for group_id in settings.enabled_groups:
            await ensure_canonical_space_preconfig(session, group_id, now=now)
