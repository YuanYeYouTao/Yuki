"""Parse the configured origin whitelist for rollup model compaction."""

from __future__ import annotations


def parse_rollup_llm_origins(raw: str) -> frozenset[str]:
    """Preserve explicit configured origins; empty input permits no model origin.

    Changing this setting does not scan or wake parked jobs by itself. A later
    ledger or visual force signal increments signal_revision and sets
    next_attempt_at=now on the same job; the new whitelist then applies.
    """

    return frozenset(item.strip() for item in raw.split(",") if item.strip())
