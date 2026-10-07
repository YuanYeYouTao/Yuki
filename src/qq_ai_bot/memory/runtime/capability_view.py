"""Derive the capability-facing view from a memory contract.

The capability runtime never reads memory-internal state; it consumes the
pure ``MemoryCapabilityView`` built here.  Namespace ids follow the R3
migration table (R2 §8's ``memory.history.read`` is shorthand for the
history namespaces below).
"""

from __future__ import annotations

from qq_ai_bot.memory.runtime.contract import (
    MemoryAvailability,
    MemoryReadPolicy,
    MemoryTurnContract,
)
from qq_ai_bot.runtime.contracts import MemoryCapabilityView

MEMORY_READ_NAMESPACES: tuple[str, ...] = (
    "memory.history.recent",
    "memory.history.search",
    "memory.history.around",
    "memory.person.read",
    "memory.self.read",
    "memory.group.read",
    "memory.fact.read",
    "memory.evidence.read",
)
MEMORY_WRITE_NAMESPACE = "memory.state.write"


def build_capability_view(
    contract: MemoryTurnContract, *, transition_revision: int
) -> MemoryCapabilityView:
    """Project frozen read/write authority onto the fixed tool namespaces."""

    if contract.availability is MemoryAvailability.FORBIDDEN:
        return MemoryCapabilityView(
            eager_namespaces=(),
            requestable_namespaces=(),
            hidden_namespaces=(*MEMORY_READ_NAMESPACES, MEMORY_WRITE_NAMESPACE),
            transition_revision=transition_revision,
        )
    eager: tuple[str, ...] = ()
    requestable: tuple[str, ...] = ()
    hidden: tuple[str, ...] = ()
    if contract.read_policy is MemoryReadPolicy.EAGER:
        eager = MEMORY_READ_NAMESPACES
    else:
        hidden = MEMORY_READ_NAMESPACES

    if contract.persistent_write_allowed:
        requestable = (*requestable, MEMORY_WRITE_NAMESPACE)
    else:
        hidden = (*hidden, MEMORY_WRITE_NAMESPACE)

    return MemoryCapabilityView(
        eager_namespaces=eager,
        requestable_namespaces=requestable,
        hidden_namespaces=hidden,
        transition_revision=transition_revision,
    )
