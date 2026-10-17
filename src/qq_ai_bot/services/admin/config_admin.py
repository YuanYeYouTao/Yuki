"""Thin shared entrypoint around the single RuntimeConfigService implementation."""

from __future__ import annotations

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import (
    AdminActor,
    AdminOperationEvent,
    ConfigChangeResult,
    ConfigSpec,
    EffectiveConfigValue,
    RuntimeConfigSnapshot,
)


class ConfigAdminService:
    """Expose registered runtime configuration to both administrator frontends."""

    def __init__(self, runtime_config: RuntimeConfigService) -> None:
        self._runtime_config = runtime_config

    def list_capabilities(self, category: str | None = None) -> tuple[ConfigSpec, ...]:
        return self._runtime_config.registry.list(category)

    async def snapshot(
        self,
        *,
        user_id: str | None = None,
        group_id: str | None = None,
    ) -> RuntimeConfigSnapshot:
        return await self._runtime_config.snapshot(user_id=user_id, group_id=group_id)

    async def get(
        self,
        key: str,
        *,
        user_id: str | None = None,
        group_id: str | None = None,
    ) -> EffectiveConfigValue:
        return await self._runtime_config.get_effective(
            key,
            user_id=user_id,
            group_id=group_id,
        )

    async def set(
        self,
        actor: AdminActor,
        *,
        key: str,
        value: object,
        scope_type: str,
        scope_id: str,
    ) -> ConfigChangeResult:
        assert actor.person_id is not None
        return await self._runtime_config.set_override(
            key,
            value,
            scope_type=scope_type,
            scope_id=scope_id,
            actor_user_id=actor.person_id,
            trigger_message_id=actor.trigger_message_id,
            conversation_key=actor.conversation_key,
        )

    async def unset(
        self,
        actor: AdminActor,
        *,
        key: str,
        scope_type: str,
        scope_id: str,
    ) -> ConfigChangeResult:
        assert actor.person_id is not None
        return await self._runtime_config.delete_override(
            key,
            scope_type=scope_type,
            scope_id=scope_id,
            actor_user_id=actor.person_id,
            trigger_message_id=actor.trigger_message_id,
            conversation_key=actor.conversation_key,
        )

    async def history(
        self,
        *,
        key: str | None = None,
        actor_user_id: str | None = None,
        limit: int = 20,
    ) -> tuple[AdminOperationEvent, ...]:
        return await self._runtime_config.history(
            key=key,
            actor_user_id=actor_user_id,
            limit=limit,
        )

    async def rollback(
        self,
        actor: AdminActor,
        change_id: int,
    ) -> ConfigChangeResult:
        assert actor.person_id is not None
        return await self._runtime_config.rollback(
            change_id,
            actor_user_id=actor.person_id,
            trigger_message_id=actor.trigger_message_id,
            conversation_key=actor.conversation_key,
        )
