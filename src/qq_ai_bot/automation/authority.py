"""Explicit authority snapshots for user and superuser automations."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import Field

from qq_ai_bot.automation.models import StrictModel, TurnOrigin
from qq_ai_bot.config import Settings

if TYPE_CHECKING:
    from qq_ai_bot.automation.registry import AutomationCapabilityRegistry


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


def permission_for(settings: Settings, user_id: str) -> PermissionLevel:
    return PermissionLevel.SUPERUSER if user_id in settings.superusers else PermissionLevel.USER


def permission_for_accounts(settings: Settings, account_ids: Iterable[str]) -> PermissionLevel:
    """Current role from live account ids. Empty input is a regular user."""

    if any(item in settings.superusers for item in account_ids):
        return PermissionLevel.SUPERUSER
    return PermissionLevel.USER


def effective_delegated_capabilities(
    authority: DelegatedAuthority,
    *,
    settings: Settings,
    registry: AutomationCapabilityRegistry,
    current_permission: PermissionLevel | None = None,
) -> frozenset[str]:
    """Intersect the immutable grant with current registry and creator permission.

    v1 omits ``current_permission`` and keeps the raw snapshot QQ baseline.
    complete-v2 must pass the Person principal's live PermissionLevel.
    """

    resolved = (
        current_permission
        if current_permission is not None
        else permission_for(settings, authority.creator_user_id)
    )
    if authority.permission_level is PermissionLevel.SUPERUSER and (
        resolved is not PermissionLevel.SUPERUSER
    ):
        return frozenset()
    allowed: set[str] = set()
    for name in authority.granted_capabilities:
        definition = registry.get(name)
        if definition is None:
            continue
        if authority.capability_schema_versions.get(name) != definition.schema_version:
            continue
        if definition.provider_plugin_id is not None:
            expected = authority.capability_provenance.get(name, {})
            if expected != {
                "plugin_id": definition.provider_plugin_id,
                "plugin_version": definition.provider_version or "",
                "manifest_hash": definition.provider_manifest_hash or "",
            }:
                continue
        if not definition.permits(resolved):
            continue
        if TurnOrigin.SCHEDULED_AUTOMATION not in definition.allowed_origins:
            continue
        allowed.add(name)
    return frozenset(allowed)
