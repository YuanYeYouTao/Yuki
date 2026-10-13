"""Unified group access and autonomous-participation administration."""

from __future__ import annotations

import time

from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.models import ControlAuditRef
from qq_ai_bot.control_plane.principal import ControlPrincipal, PrincipalSource
from qq_ai_bot.control_plane.targets import SpaceControlTarget
from qq_ai_bot.domain.control import DecisionContext
from qq_ai_bot.persistence.repositories import GroupSetting, GroupSettingsRepository
from qq_ai_bot.services.admin.control_auth import require_capability

type SpaceAdminContext = DecisionContext[ControlPrincipal, PrincipalSource, SpaceControlTarget]


class GroupAdminService:
    """Manage group admission."""

    def __init__(
        self,
        *,
        groups: GroupSettingsRepository,
        audit: AdminAuditService,
    ) -> None:
        self._groups = groups
        self._audit = audit

    async def enable_current_group(
        self,
        context: SpaceAdminContext,
        *,
        audit: ControlAuditRef,
    ) -> GroupSetting:
        return await self._set_enabled(context, True, audit=audit)

    async def disable_current_group(
        self,
        context: SpaceAdminContext,
        *,
        audit: ControlAuditRef,
    ) -> GroupSetting:
        return await self._set_enabled(context, False, audit=audit)

    async def _set_enabled(
        self,
        context: SpaceAdminContext,
        enabled: bool,
        *,
        audit: ControlAuditRef,
    ) -> GroupSetting:
        require_capability(context, "control.group.mutate")
        group_id = _space_storage_id(context)
        started = time.perf_counter()
        async with self._audit.transaction() as session:
            before = await self._groups.get(group_id, session=session)
            after = await self._groups.set_enabled(
                group_id,
                enabled,
                session=session,
            )
            await self._audit.record(
                actor=audit,
                capability="group",
                operation="enable" if enabled else "disable",
                target_type="group",
                target_id=group_id,
                before={"enabled": before.enabled if before else None},
                after={"enabled": enabled},
                success=True,
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
        return after


def _space_storage_id(context: object) -> str:
    target = getattr(context, "canonical_target", None)
    if type(target) is not SpaceControlTarget:
        raise TypeError("canonical_target must be SpaceControlTarget")
    return target.storage_group_id
