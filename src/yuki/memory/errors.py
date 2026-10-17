"""Stable public error categories for Memory V2 retrieval."""

from __future__ import annotations


class MemoryRetrievalError(RuntimeError):
    """A retrieval failure whose code is safe for tools and diagnostics."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
