"""Per-turn memory contract (frozen by R2 §3.1).

The contract is derived from trusted origin/config/authority before the
first model call. Every read and mutation rechecks execution authority.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


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


def active_read_contract(
    *,
    persistent_write_allowed: bool = True,
) -> MemoryTurnContract:
    return MemoryTurnContract(
        availability=MemoryAvailability.ENABLED,
        read_policy=MemoryReadPolicy.EAGER,
        persistent_write_allowed=persistent_write_allowed,
    )


def forbidden_contract() -> MemoryTurnContract:
    return MemoryTurnContract(
        availability=MemoryAvailability.FORBIDDEN,
        read_policy=MemoryReadPolicy.DENIED,
        persistent_write_allowed=False,
    )
