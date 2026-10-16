"""Canonical ownership shape of persisted Dream facts."""

from __future__ import annotations

import logging
import time
from typing import Any

from qq_ai_bot.memory.partition import canonical_fact_owner_complete

logger = logging.getLogger(__name__)

MIXED_DREAM_SOURCE = "mixed_dream_source"
INCOMPLETE_DREAM_SHAPE = "incomplete_dream_shape"

_UNRESOLVED_LOG_INTERVAL_SECONDS = 30.0
_last_unresolved_log: dict[str, float] = {}

DreamShape = tuple[str | None, str | None, str | None, str | None]


def log_owner_unresolved(reason: str) -> None:
    """Emit only a stable category and rate limit repeated topology gaps."""

    now = time.monotonic()
    last = _last_unresolved_log.get(reason, 0.0)
    if now - last < _UNRESOLVED_LOG_INTERVAL_SECONDS:
        return
    _last_unresolved_log[reason] = now
    logger.info("canonical_owner_unresolved reason=%s", reason)


def optional_dream_owner_from_facts(facts: tuple[Any, ...]) -> DreamShape | None:
    if not facts:
        log_owner_unresolved(INCOMPLETE_DREAM_SHAPE)
        return None
    shapes: set[DreamShape] = set()
    for fact in facts:
        if not canonical_fact_owner_complete(fact):
            log_owner_unresolved(INCOMPLETE_DREAM_SHAPE)
            return None
        shapes.add(
            (
                getattr(fact, "canonical_subject_person_id", None),
                getattr(fact, "canonical_subject_space_id", None),
                getattr(fact, "canonical_visibility_person_id", None),
                getattr(fact, "canonical_visibility_space_id", None),
            )
        )
    if len(shapes) != 1:
        log_owner_unresolved(MIXED_DREAM_SOURCE)
        return None
    return next(iter(shapes))


__all__ = ["optional_dream_owner_from_facts"]
