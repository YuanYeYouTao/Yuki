"""Compact known Host context fields without changing their source records."""

from __future__ import annotations

from typing import Any


def project_short_state(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Present actual occupied slots without storage bookkeeping."""
    return [{"slot": row["slot"], "text": row["text"]} for row in rows if row["text"]]
