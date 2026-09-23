"""Explicit authority snapshots for user and superuser automations."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from pydantic import Field, model_validator

from qq_ai_bot.automation.models import StrictModel, TurnOrigin
from qq_ai_bot.config import Settings


class PermissionLevel(StrEnum):
    USER = "user"
    SUPERUSER = "superuser"
    SELF = "self"


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
    principal_kind: str = "person"
    canonical_conversation_id: str | None = None
    conversation_generation: int | None = None
    canonical_presence_id: str | None = None
    canonical_space_id: str | None = None

    @model_validator(mode="after")
    def _valid_principal(self) -> DelegatedAuthority:
        if self.principal_kind == "self":
            if (
                self.permission_level is not PermissionLevel.SELF
                or self.creator_user_id
                or self.created_from_message_id
                or not self.current_group_id
                or not self.canonical_conversation_id
                or self.conversation_generation is None
                or not self.canonical_presence_id
                or not self.canonical_space_id
            ):
                raise ValueError("invalid_self_automation_authority")
        elif self.principal_kind != "person" or self.permission_level is PermissionLevel.SELF:
            raise ValueError("invalid_person_automation_authority")
        return self


class AuthorityContext(StrictModel):
    origin: TurnOrigin
    actor_user_id: str
    actor_is_superuser: bool
    bot_user_id: str
    principal_kind: str = "person"
    delegated_authority: DelegatedAuthority | None = None
    allowed_capabilities: frozenset[str] = Field(default_factory=frozenset)


def permission_for_accounts(settings: Settings, account_ids: Iterable[str]) -> PermissionLevel:
    """Current role from live account ids. Empty input is a regular user."""

    if any(item in settings.superusers for item in account_ids):
        return PermissionLevel.SUPERUSER
    return PermissionLevel.USER
