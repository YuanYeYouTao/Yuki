"""Content-free counters for historical rebuild operations."""

from __future__ import annotations

from collections import Counter

METRIC_NAMES = frozenset(
    {
        "rebuild_runs_planned",
        "rebuild_runs_started",
        "rebuild_runs_completed",
        "rebuild_runs_cancelled",
        "rebuild_events_matched",
        "rebuild_events_eligible",
        "rebuild_events_scanned",
        "rebuild_events_no_claims",
        "rebuild_events_skipped_processed",
        "rebuild_events_failed",
        "rebuild_proposals_staged",
        "rebuild_proposals_approved",
        "rebuild_proposals_rejected",
        "rebuild_proposals_committed",
        "rebuild_proposals_failed",
        "rebuild_facts_created",
        "rebuild_evidence_merged",
        "rebuild_facts_superseded",
        "rebuild_facts_contested",
        "rebuild_facts_invalidated",
        "rebuild_noops",
        "rebuild_extraction_requests",
        "rebuild_input_tokens",
        "rebuild_output_tokens",
        "rebuild_latency",
    }
)


class MemoryRebuildMetrics:
    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()

    def increment(self, name: str, count: int = 1) -> None:
        if name not in METRIC_NAMES:
            raise ValueError(f"unknown memory rebuild metric: {name}")
        self._counts[name] += count

    def snapshot(self) -> dict[str, int]:
        result = {name: int(self._counts[name]) for name in sorted(METRIC_NAMES)}
        result["memory_rebuild_claims"] = result["rebuild_proposals_staged"]
        return result
