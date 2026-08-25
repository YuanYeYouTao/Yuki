"""Transport-neutral admin targets. Persistence-free and I/O-free.

These name an already-resolved v1 storage key plus an optional canonical
Person/Space. Callers must not invent a PersonId or SpaceId here; missing
bindings stay None.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import final

from qq_ai_bot.domain.identity import PersonId, SpaceId


def _require_storage_id(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be a str")
    token = value.strip()
    if not token or token != value:
        raise ValueError(f"{name} must be a non-empty trimmed token")
    return token


@final
@dataclass(frozen=True, slots=True)
class PersonControlTarget:
    """One person-scoped admin target. ``person_id`` is set only after Binding."""

    person_id: PersonId | None
    storage_user_id: str
    lockout_protected: bool = False

    def __init__(
        self,
        *,
        person_id: PersonId | None,
        storage_user_id: str,
        lockout_protected: bool = False,
    ) -> None:
        if person_id is not None and type(person_id) is not PersonId:
            raise TypeError("person_id must be PersonId or None")
        if type(lockout_protected) is not bool:
            raise TypeError("lockout_protected must be a bool")
        object.__setattr__(self, "person_id", person_id)
        object.__setattr__(
            self, "storage_user_id", _require_storage_id(storage_user_id, "storage_user_id")
        )
        object.__setattr__(self, "lockout_protected", lockout_protected)


@final
@dataclass(frozen=True, slots=True)
class SpaceControlTarget:
    """One space-scoped admin target. ``space_id`` is set only after Binding."""

    space_id: SpaceId | None
    storage_group_id: str

    def __init__(self, *, space_id: SpaceId | None, storage_group_id: str) -> None:
        if space_id is not None and type(space_id) is not SpaceId:
            raise TypeError("space_id must be SpaceId or None")
        object.__setattr__(self, "space_id", space_id)
        object.__setattr__(
            self, "storage_group_id", _require_storage_id(storage_group_id, "storage_group_id")
        )
