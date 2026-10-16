"""Trusted memory scope and access resolution.

Scope and the initial ``MemoryTurnContract`` are derived from host facts
only — never from model output or a phrase dictionary. Ordinary natural
language uses the fixed authorized tool surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from qq_ai_bot.memory.runtime.contract import (
    MemoryTurnContract,
    active_read_contract,
    forbidden_contract,
)
from qq_ai_bot.runtime.origin import TurnOrigin

_WRITE_ORIGINS = frozenset({TurnOrigin.USER_MESSAGE, TurnOrigin.AUTONOMOUS_GROUP})
_MESSAGE_ORIGINS = frozenset({TurnOrigin.USER_MESSAGE, TurnOrigin.AUTONOMOUS_GROUP})


class MemoryAccessReason(StrEnum):
    """Content-free reason for the initial contract.  Safe to persist."""

    AUTHORITY_FORBIDDEN = "authority_forbidden"
    ORIGIN_RESTRICTED = "origin_restricted"
    ORDINARY_NATURAL_LANGUAGE = "ordinary_natural_language"
    SELF_ORIGIN = "self_origin"


@dataclass(frozen=True, slots=True)
class MemoryAccessDecision:
    """Resolver output: a valid contract plus why it was chosen."""

    contract: MemoryTurnContract
    reason: MemoryAccessReason


def origin_allows_persistent_write(origin: TurnOrigin) -> bool:
    """User @ turns and admitted autonomous group turns may persist writes."""

    return origin in _WRITE_ORIGINS


def resolve_memory_access(
    *,
    origin: TurnOrigin,
    memory_available: bool = True,
) -> MemoryAccessDecision:
    """Choose the initial memory contract from trusted host evidence.

    Every mutation validates its source, target, evidence, and original effect
    receipt independently of this initial contract.
    """

    if not memory_available:
        return MemoryAccessDecision(
            contract=forbidden_contract(),
            reason=MemoryAccessReason.AUTHORITY_FORBIDDEN,
        )

    write_allowed = origin_allows_persistent_write(origin)
    if origin not in _MESSAGE_ORIGINS:
        return MemoryAccessDecision(
            contract=active_read_contract(
                persistent_write_allowed=False,
            ),
            reason=MemoryAccessReason.ORIGIN_RESTRICTED,
        )

    reason = MemoryAccessReason.ORDINARY_NATURAL_LANGUAGE
    return MemoryAccessDecision(
        contract=active_read_contract(persistent_write_allowed=write_allowed),
        reason=reason,
    )
