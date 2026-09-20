"""Explicit authority snapshots for user and superuser automations."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from pydantic import Field

from qq_ai_bot.automation.models import StrictModel, TurnOrigin
from qq_ai_bot.config import Settings

class PermissionLevel(StrEnum):
    USER = "user"
    SUPERUSER = "superuser"


class DelegatedAuthority(StrictModel):
    creator_user_id: str
    bot_user_id: str
    created_from_message_id: str
    created_at: str
    permission_level: PermissionLevel
    granted_capabilities: tuple[str, ...]
    capability_schema_versions: dict[str, int | str]
    capability_provenance: dict[str, dict[str, str]] = Field(default_factory=dict)
    authority_version: int = 1
    origin: TurnOrigin = TurnOrigin.SCHEDULED_AUTOMATION
    current_group_id: str | None = None


class AuthorityContext(StrictModel):
    origin: TurnOrigin
    actor_user_id: str
    actor_is_superuser: bool
    bot_user_id: str
    delegated_authority: DelegatedAuthority | None = None
    allowed_capabilities: frozenset[str] = Field(default_factory=frozenset)


def permission_for_accounts(settings: Settings, account_ids: Iterable[str]) -> PermissionLevel:
    """Current role from live account ids. Empty input is a regular user."""

    if any(item in settings.superusers for item in account_ids):
        return PermissionLevel.SUPERUSER
    return PermissionLevel.USER
