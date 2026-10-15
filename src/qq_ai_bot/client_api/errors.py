"""Public client errors do not disclose inaccessible objects or credentials."""

from typing import Any


class ClientError(Exception):
    def __init__(self, code: str, *, details: dict[str, Any] | None = None) -> None:
        self.code = code
        self.details = details or {}
        super().__init__(code)
