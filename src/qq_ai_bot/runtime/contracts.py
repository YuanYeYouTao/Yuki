"""Cross-domain pure data contracts.

Types that must be visible to more than one runtime (conversation, memory,
capability) live here so those packages never import each other directly.
Everything in this module is pure data plus pure policy functions — no I/O,
no service references.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class MemoryCapabilityView(BaseModel):
    """What the memory runtime exposes to the capability runtime (R2 §8).

    The capability runtime consumes this view verbatim and never reads
    memory-internal state.  ``transition_revision`` increments on every
    contract transition so stale views are detectable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    eager_namespaces: tuple[str, ...]
    requestable_namespaces: tuple[str, ...]
    hidden_namespaces: tuple[str, ...]
    transition_revision: int
