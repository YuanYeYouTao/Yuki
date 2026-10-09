"""Delivery status vocabulary shared by turn delivery summaries."""

from __future__ import annotations

from enum import StrEnum


class DeliveryStatus(StrEnum):
    """Aggregate delivery classification for one turn."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    FAILED = "failed"
