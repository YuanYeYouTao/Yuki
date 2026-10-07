"""Stable tool declarations and per-turn execution grants."""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.capabilities.catalog import UnifiedToolCatalog, UnifiedToolCatalogEntry
from qq_ai_bot.runtime.contracts import MemoryCapabilityView

NO_LONGER_AUTHORIZED = "capability_no_longer_authorized"


@dataclass(frozen=True, slots=True)
class ExposurePlan:
    entries: tuple[UnifiedToolCatalogEntry, ...]
    callable_ids: frozenset[str]


def stable_exposure_plan(
    *,
    catalog: UnifiedToolCatalog,
    requestable_ids: frozenset[str],
    memory_view: MemoryCapabilityView | None,
) -> ExposurePlan:
    """Declare every admitted tool; only the execution grants may change."""

    entries = catalog.entries
    callable_ids = frozenset(
        entry.descriptor.model_name
        for entry in entries
        if entry.descriptor.model_name in requestable_ids
    )
    return ExposurePlan(entries=entries, callable_ids=callable_ids)
