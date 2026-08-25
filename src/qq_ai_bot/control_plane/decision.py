"""Policy decisions over the C1 DecisionContext envelope."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import final

from qq_ai_bot.control_plane.capabilities import (
    DENIED_CAPABILITY_ID,
    is_forbidden_control_capability,
    is_protocol_capability,
    normalize_capability_id,
)
from qq_ai_bot.control_plane.principal import ControlPrincipal, PrincipalSource
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.domain.control import DecisionContext


@final
class PolicyEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


def _decision_capability(value: object, *, allowing: bool) -> str:
    try:
        token = normalize_capability_id(value)
    except (TypeError, ValueError):
        if allowing:
            raise
        return DENIED_CAPABILITY_ID
    if allowing:
        if is_forbidden_control_capability(token) or not is_protocol_capability(token):
            raise ValueError("forbidden capability")
        return token
    if is_forbidden_control_capability(token) or not is_protocol_capability(token):
        return DENIED_CAPABILITY_ID
    return token


@final
@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Explicit allow or deny for one capability."""

    effect: PolicyEffect
    capability: str
    problem: Problem | None

    def __init__(
        self,
        *,
        effect: PolicyEffect,
        capability: str,
        problem: Problem | None,
    ) -> None:
        if type(effect) is not PolicyEffect:
            raise TypeError("effect must be PolicyEffect")
        if problem is not None and type(problem) is not Problem:
            raise TypeError("problem must be Problem or None")
        if effect is PolicyEffect.ALLOW and problem is not None:
            raise ValueError("allow cannot carry a problem")
        if effect is PolicyEffect.DENY and problem is None:
            raise ValueError("deny requires a problem")
        object.__setattr__(self, "effect", effect)
        object.__setattr__(
            self,
            "capability",
            _decision_capability(capability, allowing=effect is PolicyEffect.ALLOW),
        )
        object.__setattr__(self, "problem", problem)

    @property
    def allowed(self) -> bool:
        return self.effect is PolicyEffect.ALLOW

    @classmethod
    def allow(cls, capability: str) -> PolicyDecision:
        return cls(effect=PolicyEffect.ALLOW, capability=capability, problem=None)

    @classmethod
    def deny(cls, capability: str, problem: Problem) -> PolicyDecision:
        return cls(effect=PolicyEffect.DENY, capability=capability, problem=problem)


def decide(context: object, capability: object) -> PolicyDecision:
    """Authorize one capability from a C1 DecisionContext.

    Capability errors fail closed as DENY. A copied envelope is still rejected.
    Forbidden or unknown tokens are never echoed on the decision.
    """

    if type(context) is not DecisionContext:
        raise TypeError("context must be DecisionContext")
    principal = context.principal
    if type(principal) is not ControlPrincipal:
        raise TypeError("principal must be ControlPrincipal")
    if type(context.source) is not PrincipalSource:
        raise TypeError("source must be PrincipalSource")
    if context.source is not principal.source:
        raise ValueError("decision source must match principal source")
    try:
        token = normalize_capability_id(capability)
    except (TypeError, ValueError):
        return PolicyDecision.deny(DENIED_CAPABILITY_ID, Problem(ProblemCode.VALIDATION_ERROR))
    if is_forbidden_control_capability(token) or not is_protocol_capability(token):
        return PolicyDecision.deny(DENIED_CAPABILITY_ID, Problem(ProblemCode.CAPABILITY_DENIED))
    if not principal.authenticated:
        return PolicyDecision.deny(token, Problem(ProblemCode.UNAUTHENTICATED))
    if not principal.active:
        return PolicyDecision.deny(token, Problem(ProblemCode.PRECONDITION_FAILED))
    if token not in principal.granted_capabilities:
        return PolicyDecision.deny(token, Problem(ProblemCode.CAPABILITY_DENIED))
    return PolicyDecision.allow(token)
