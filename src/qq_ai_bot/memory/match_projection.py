"""Explain candidate matching without treating retrieval ranks as truth scores."""

from typing import Literal, TypedDict

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.memory.enums import MemoryRetrievalMode
from qq_ai_bot.memory.models import MemoryRetrievalHit, MemoryRetrievalResult


class MatchProjection(TypedDict):
    lexical_match: bool
    semantic_candidate: bool
    topic_admission: Literal["passed", "not_passed", "unknown"]


def match_projection(
    hit: MemoryRetrievalHit, result: MemoryRetrievalResult, runtime: RuntimeConfigSnapshot
) -> MatchProjection:
    """Describe the topic gate; never prune active overview/search/detail results."""
    admission: Literal["passed", "not_passed", "unknown"] = "unknown"
    config = runtime.memory
    calibrated = bool(
        config.automatic_calibrated_profile
        and config.automatic_calibrated_profile == result.embedding_profile
        and not result.semantic_degraded
        and config.automatic_topic_threshold >= config.automatic_background_threshold
    )
    if result.mode is not MemoryRetrievalMode.OVERVIEW:
        if hit.selection_reason in {"memory_key_exact", "content_exact"}:
            admission = "passed"
        elif calibrated and hit.semantic_score is not None:
            admission = (
                "passed" if hit.semantic_score >= config.automatic_topic_threshold else "not_passed"
            )
    return {
        "lexical_match": hit.lexical_score is not None,
        "semantic_candidate": hit.semantic_score is not None,
        "topic_admission": admission,
    }
