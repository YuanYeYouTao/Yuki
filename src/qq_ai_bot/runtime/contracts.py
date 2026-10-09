"""Cross-domain pure data contracts.

Types that must be visible to more than one runtime (conversation, memory,
capability) live here so those packages never import each other directly.
Everything in this module is pure data plus pure policy functions — no I/O,
no service references.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from qq_ai_bot.runtime.delivery import DeliveryStatus


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


@dataclass(frozen=True, slots=True)
class MemoryReceiptHandle:
    """Content-free handle to one recall receipt produced this turn.

    ``receipt_turn_id`` is the receipt's own unique id (the pre-existing
    ``memory_recall_receipts.turn_id`` semantics), *not* the runtime turn id.
    """

    receipt_turn_id: str
    injected_fact_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class DeliverySummary:
    """What the delivery runtime hands to memory finalization/attribution.

    Carries the actually-delivered body because attribution must run on what
    the user saw, not on what the model drafted.  This object stays in
    process memory; it is never persisted.
    """

    final_agent_run_id: str
    status: DeliveryStatus
    delivered_text: str
    emoji_only: bool = False
    transport_receipt_ids: tuple[str, ...] = ()
