"""Authorization helpers shared by administrator business services."""

from __future__ import annotations

from yuki.admin.models import AdminActor
from yuki.config import Settings


def require_real_superuser(actor: AdminActor, settings: Settings) -> None:
    """Consume the current canonical source's already-resolved authority."""

    if not actor.is_superuser or actor.person_id is None:
        raise PermissionError("只有当前真实超级管理员可以执行该操作")


def require_self_or_superuser(
    actor: AdminActor,
    target_user_id: str,
    settings: Settings,
) -> None:
    """Allow self-service, while protecting cross-person operations."""

    if target_user_id == actor.person_id:
        return
    require_real_superuser(actor, settings)
