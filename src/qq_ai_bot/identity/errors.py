"""Operational errors for identity backfill. Messages never include paths."""

from __future__ import annotations


class IdentityBackfillError(Exception):
    """Expected backfill failure with a stable, non-sensitive category."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__("identity backfill failed")


class IdentityBackfillPreconditionError(IdentityBackfillError):
    """Missing database, incomplete C7 schema, or invalid runtime state."""


class IdentityDualWriteError(Exception):
    """v1 dual-write failed closed. Messages never include paths or raw dumps."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__("identity dual-write failed")


class IdentityCutoverError(Exception):
    """Expected cutover failure with a stable, non-sensitive category."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__("identity cutover failed")


class IdentityCutoverPreconditionError(IdentityCutoverError):
    """Missing database, incomplete 0048 schema, or invalid cutover evidence."""
