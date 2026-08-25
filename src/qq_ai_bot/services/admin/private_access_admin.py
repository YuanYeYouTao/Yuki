"""Unified private-chat access administration."""

from __future__ import annotations

import time

from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import ControlAuditRef
from qq_ai_bot.control_plane.principal import ControlPrincipal, PrincipalSource
from qq_ai_bot.control_plane.targets import PersonControlTarget
from qq_ai_bot.domain.control import DecisionContext
from qq_ai_bot.identity.errors import IdentityDualWriteError
from qq_ai_bot.persistence.repositories import (
    PrivateUserSetting,
    PrivateUserSettingsRepository,
)
from qq_ai_bot.services.admin.control_auth import person_storage_id, require_capability

_CANONICAL_OWNER_DISABLED = "canonical_owner_disabled"

type PersonAdminContext = DecisionContext[ControlPrincipal, PrincipalSource, PersonControlTarget]


class PrivateAccessAdminService:
    """Block or restore private access without allowing superuser lockout."""

    def __init__(
        self,
        *,
        private_users: PrivateUserSettingsRepository,
        audit: AdminAuditService,
        runtime_config: RuntimeConfigService | None = None,
    ) -> None:
        self._private_users = private_users
        self._audit = audit
        self._runtime_config = runtime_config

    async def enable_user(
        self,
        context: PersonAdminContext,
        *,
        audit: ControlAuditRef,
    ) -> PrivateUserSetting:
        return await self._set(context, True, audit=audit)

    async def disable_user(
        self,
        context: PersonAdminContext,
        *,
        audit: ControlAuditRef,
    ) -> PrivateUserSetting:
        target = context.canonical_target
        if type(target) is not PersonControlTarget:
            raise TypeError("canonical_target must be PersonControlTarget")
        if target.lockout_protected:
            raise ValueError("不能关闭超级用户的私聊权限。")
        return await self._set(context, False, audit=audit)

    async def _set(
        self,
        context: PersonAdminContext,
        enabled: bool,
        *,
        audit: ControlAuditRef,
    ) -> PrivateUserSetting:
        require_capability(context, "control.private_access.mutate")
        target_user_id = person_storage_id(context)
        started = time.perf_counter()
        initial_affection: int | None = None
        initial_trust: int | None = None
        if self._runtime_config is not None:
            try:
                runtime = await self._runtime_config.snapshot(user_id=target_user_id)
            except IdentityDualWriteError as exc:
                if not enabled or exc.category != _CANONICAL_OWNER_DISABLED:
                    raise
                runtime = await self._runtime_config.snapshot()
            initial_affection = runtime.relationship.initial_affection
            initial_trust = runtime.relationship.initial_trust
        async with self._audit.transaction() as session:
            before = await self._private_users.get(target_user_id, session=session)
            after = await self._private_users.set_enabled(
                target_user_id,
                enabled,
                initial_affection=initial_affection,
                initial_trust=initial_trust,
                session=session,
            )
            await self._audit.record(
                actor=audit,
                capability="private_access",
                operation="enable" if enabled else "disable",
                target_type="user",
                target_id=target_user_id,
                before={"enabled": before.enabled if before else None},
                after={"enabled": enabled},
                success=True,
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
        return after
