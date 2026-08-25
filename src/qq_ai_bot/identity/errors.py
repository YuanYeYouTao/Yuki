"""Operational errors for identity backfill. Messages never include paths."""

from __future__ import annotations


class IdentityBackfillError(Exception):
    """Expected backfill failure with a stable, non-sensitive category."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__("identity backfill failed")


class IdentityBackfillPreconditionError(IdentityBackfillError):
    """Missing database, incomplete C7 schema, or invalid runtime state."""
