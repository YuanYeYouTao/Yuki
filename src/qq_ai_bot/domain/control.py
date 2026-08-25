"""Transport-neutral control decision context.

``DecisionContext`` only carries request, correlation, principal, source,
canonical target, and reason. ``PrincipalT`` and ``SourceT`` are left unbound so
later control work can attach a richer principal and a transport StrEnum without
changing this envelope. This module stays I/O-free and does not define RBAC,
capabilities, or command/result types.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, final

from qq_ai_bot.domain.identity import PrincipalId, RequestId


@final
class YukiControlTarget(StrEnum):
    """Synthetic singleton for Yuki Presence registration. Not a table row."""

    PERMANENT_YUKI = "permanent_yuki"


class DecisionPrincipal(Protocol):
    """Minimal principal surface for a decision.

    Later control work may bind a richer principal that still exposes
    ``principal_id``. Roles, capabilities, and authentication state stay out of
    this module.
    """

    @property
    def principal_id(self) -> PrincipalId: ...


@dataclass(frozen=True, slots=True)
class DecisionContext[PrincipalT: DecisionPrincipal, SourceT, TargetT]:
    """Immutable decision envelope shared by future control transports."""

    request_id: RequestId
    principal: PrincipalT
    source: SourceT
    canonical_target: TargetT
    reason: str = ""
    correlation_id: RequestId | None = None

    def __post_init__(self) -> None:
        if type(self.request_id) is not RequestId:
            raise TypeError("request_id must be RequestId")
        if self.correlation_id is not None and type(self.correlation_id) is not RequestId:
            raise TypeError("correlation_id must be RequestId or None")
        if type(self.reason) is not str:
            raise TypeError("reason must be a str")
        principal_id = getattr(self.principal, "principal_id", None)
        if type(principal_id) is not PrincipalId:
            raise TypeError("principal must expose principal_id as PrincipalId")
        if self.canonical_target is None:
            raise ValueError("canonical_target is required")
