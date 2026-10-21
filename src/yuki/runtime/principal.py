"""Canonical owner of durable work, independent of its trigger and transport."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class PrincipalRef:
    kind: Literal["person", "self"]
    id: str

    def __post_init__(self) -> None:
        if self.kind == "self":
            if self.id != "self":
                raise ValueError("invalid_self_principal")
        elif self.kind == "person":
            if not self.id or self.id == "self":
                raise ValueError("invalid_person_principal")
        else:
            raise ValueError("unknown_principal_kind")


SELF = PrincipalRef("self", "self")
