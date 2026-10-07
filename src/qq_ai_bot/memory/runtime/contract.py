"""Per-turn memory contract (frozen by R2 §3.1).

The contract is derived from trusted origin/config/authority before the
first model call. Every read and mutation rechecks execution authority.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from qq_ai_bot.memory.enums import MemoryRecallPurpose


class MemoryReadPolicy(StrEnum):
    """How memory read tools may be exposed this turn."""

    DENIED = "denied"
    EAGER = "eager"


class MemoryAvailability(StrEnum):
    """Whether memory participates in this turn at all."""

    ENABLED = "enabled"
    FORBIDDEN = "forbidden"


class MemoryTurnContract(BaseModel):
    """Frozen read/write authority; execution still validates every operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    availability: MemoryAvailability
    read_policy: MemoryReadPolicy
    persistent_write_allowed: bool
    default_purpose: MemoryRecallPurpose


def active_read_contract(
    default_purpose: MemoryRecallPurpose = MemoryRecallPurpose.RECALL,
    *,
    persistent_write_allowed: bool = True,
) -> MemoryTurnContract:
    return MemoryTurnContract(
        availability=MemoryAvailability.ENABLED,
        read_policy=MemoryReadPolicy.EAGER,
        persistent_write_allowed=persistent_write_allowed,
        default_purpose=default_purpose,
    )


def forbidden_contract(default_purpose: MemoryRecallPurpose) -> MemoryTurnContract:
    return MemoryTurnContract(
        availability=MemoryAvailability.FORBIDDEN,
        read_policy=MemoryReadPolicy.DENIED,
        persistent_write_allowed=False,
        default_purpose=default_purpose,
    )
