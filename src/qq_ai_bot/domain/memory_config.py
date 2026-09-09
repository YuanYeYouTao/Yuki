"""Backend-owned configuration identity, independent of memory evidence authors."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MemoryConfigScope:
    person_id: str | None = None
    space_id: str | None = None
