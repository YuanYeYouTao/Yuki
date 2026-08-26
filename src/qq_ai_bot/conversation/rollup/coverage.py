"""Shared session-level Prompt coverage selection."""

from __future__ import annotations

from typing import Protocol


class OverlayCoverageView(Protocol):
    generation: int
    base_semantic_revision: int
    covered_through_event_id: int
    summary_text: str


class SemanticCoverageView(Protocol):
    generation: int
    revision: int
    covered_through_event_id: int


def valid_same_generation_semantic(
    semantic: SemanticCoverageView | None,
    *,
    generation: int,
    starts_after: int,
    last_event_id: int,
) -> bool:
    """True when the durable checkpoint is usable by prompt assembly."""

    return bool(
        semantic is not None
        and semantic.generation == generation
        and starts_after <= semantic.covered_through_event_id <= last_event_id
    )


def valid_same_generation_overlay(
    overlay: OverlayCoverageView | None,
    *,
    generation: int,
    starts_after: int,
    last_event_id: int,
    semantic_revision: int,
) -> bool:
    """True when an emergency overlay is the live Prompt coverage watermark."""

    if overlay is None:
        return False
    return (
        overlay.generation == generation
        and overlay.base_semantic_revision == semantic_revision
        and starts_after <= overlay.covered_through_event_id <= last_event_id
        and bool(overlay.summary_text.strip())
    )


def session_effective_coverage(
    *,
    generation: int,
    starts_after: int,
    last_event_id: int,
    overlay: OverlayCoverageView | None,
    semantic: SemanticCoverageView | None,
) -> int:
    """Return the id watermark used by final Prompt / compact for one session.

    Order is identical to rollup prompt assembly: a valid same-generation overlay
    first, else same-generation semantic coverage, else the generation fence.
    """

    semantic_is_valid = valid_same_generation_semantic(
        semantic,
        generation=generation,
        starts_after=starts_after,
        last_event_id=last_event_id,
    )
    semantic_revision = semantic.revision if semantic is not None else 0
    if valid_same_generation_overlay(
        overlay,
        generation=generation,
        starts_after=starts_after,
        last_event_id=last_event_id,
        semantic_revision=semantic_revision,
    ):
        assert overlay is not None
        return overlay.covered_through_event_id
    if semantic_is_valid and semantic is not None:
        return semantic.covered_through_event_id
    return starts_after
