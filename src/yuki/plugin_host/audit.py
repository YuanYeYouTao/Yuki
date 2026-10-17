"""Small redacted audit facade shared by Host adapters."""

from __future__ import annotations

from collections.abc import Mapping

from yuki.plugin_host.repository import PluginAuditRepository


class PluginAuditService:
    def __init__(self, repository: PluginAuditRepository) -> None:
        self._repository = repository

    async def record(
        self,
        *,
        plugin_id: str,
        actor_user_id: str | None,
        operation: str,
        permission: str | None,
        success: bool,
        error_category: str | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        await self._repository.record(
            plugin_id=plugin_id,
            actor_user_id=actor_user_id,
            operation=operation,
            permission=permission,
            success=success,
            error_category=error_category,
            detail=dict(detail or {}),
        )


__all__ = ["PluginAuditService"]
