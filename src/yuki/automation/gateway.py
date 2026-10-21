"""Sanitized native transport failure shared by notification delivery."""

from __future__ import annotations


class ProactiveGatewayError(RuntimeError):
    """Sanitized gateway failure shared with plugin notification transport."""

    def __init__(self, category: str, *, uncertain: bool = False) -> None:
        super().__init__(category)
        self.category = category
        self.uncertain = uncertain
