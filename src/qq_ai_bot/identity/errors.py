"""Stable, content-free identity error categories."""

from __future__ import annotations


class CanonicalIdentityError(Exception):
    """A canonical identity invariant failed closed."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__("canonical identity invariant failed")
