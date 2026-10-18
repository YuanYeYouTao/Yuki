"""Read old raw QQ AT segments without rewriting their source ledger event."""

import logging
from typing import Any

from yuki.adapters.onebot.normalizer import (
    MentionProjection,
    SegmentProjectionError,
    project_serialized_segments,
)


def project_legacy_segments(
    segments: tuple[dict[str, Any], ...], bot_account: str
) -> MentionProjection | None:
    if not any(segment.get("type") == "at" for segment in segments):
        return None
    try:
        projection = project_serialized_segments(
            segments, yuki_account_ids=frozenset({bot_account})
        )
    except SegmentProjectionError as exc:
        logging.getLogger(__name__).warning(
            "historical_segment_projection_fallback category=%s", str(exc)
        )
        return None
    return projection
