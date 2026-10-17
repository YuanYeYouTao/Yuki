"""Capability checks for transport-neutral admin services.

Authorization uses ControlPrincipal / DecisionContext / protocol capabilities.
This module does not read startup SUPERUSERS and does not accept transport actors.
"""

from __future__ import annotations

from typing import Protocol

from yuki.admin.authority.decision import decide
from yuki.admin.authority.principal import ControlPrincipal
from yuki.admin.authority.targets import PersonControlTarget
from yuki.domain.control import DecisionContext

SUPERUSER_ONLY_MESSAGE = "只有当前真实超级管理员可以执行该操作"


class ControlAuditSubject(Protocol):
    """Minimal audit identity already resolved at the adapter boundary."""

    @property
    def user_id(self) -> str: ...


def require_capability(context: object, capability: str) -> None:
    """Fail closed unless decide() allows the protocol capability."""

    if type(context) is not DecisionContext:
        raise TypeError("context must be DecisionContext")
    decision = decide(context, capability)
    if not decision.allowed:
        raise PermissionError(SUPERUSER_ONLY_MESSAGE)


def is_self(context: object, audit: ControlAuditSubject) -> bool:
    """True when the already-resolved principal owns the person target."""

    if type(context) is not DecisionContext:
        return False
    principal = context.principal
    target = context.canonical_target
    if type(principal) is not ControlPrincipal or type(target) is not PersonControlTarget:
        return False
    return principal.person_id is not None and principal.person_id == target.person_id


def require_self_or_capability(
    context: object,
    capability: str,
    audit: ControlAuditSubject,
) -> None:
    """Allow the principal's own person, otherwise require a granted capability."""

    if type(context) is not DecisionContext:
        raise TypeError("context must be DecisionContext")
    principal = context.principal
    if type(principal) is not ControlPrincipal:
        raise TypeError("principal must be ControlPrincipal")
    if is_self(context, audit):
        if not principal.authenticated or not principal.active:
            raise PermissionError(SUPERUSER_ONLY_MESSAGE)
        return
    require_capability(context, capability)


def person_storage_id(context: object) -> str:
    target = getattr(context, "canonical_target", None)
    if type(target) is not PersonControlTarget:
        raise TypeError("canonical_target must be PersonControlTarget")
    if target.person_id is None:
        raise ValueError("missing_canonical_owner")
    return target.person_id.text
