"""Canonical identity value types.

These types are transport-neutral and persistence-free. They exist so later
commits can name persons, spaces, bindings, presences, conversations, and
requests without treating UUID strings as interchangeable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Self, final
from uuid import UUID, uuid4

_CANONICAL_UUID_TEXT = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)


@final
class AuthorKind(StrEnum):
    """Who authored an utterance or ledger event.

    Origin and event kind stay outside this type. Command, plugin, automation,
    scheduled task, and migration are not authors.
    """

    PERSON = "person"
    YUKI = "yuki"
    EXTERNAL_BOT = "external_bot"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class _CanonicalUuid4:
    """Nominal UUID4 identity with canonical TEXT(36) serialization."""

    value: UUID

    def __post_init__(self) -> None:
        if type(self.value) is not UUID:
            raise TypeError(f"{type(self).__name__} value must be uuid.UUID")
        if self.value.version != 4:
            raise ValueError(f"{type(self).__name__} requires a UUID4 value")

    @classmethod
    def new(cls) -> Self:
        """Create a new identity from :func:`uuid.uuid4`."""

        return cls(uuid4())

    @classmethod
    def parse(cls, raw: str | UUID) -> Self:
        """Parse a UUID4 from canonical TEXT(36) or a standard-library UUID.

        Strings must already be hyphenated TEXT(36). Case may vary and is
        normalized to lowercase. Other identity objects are rejected; callers
        must not rely on ``str(other_id)`` being applied implicitly.
        """

        if isinstance(raw, _CanonicalUuid4):
            raise TypeError(
                f"{cls.__name__} cannot parse {type(raw).__name__}; "
                "pass canonical TEXT or uuid.UUID, not another identity"
            )
        if type(raw) is UUID:
            parsed = raw
        elif type(raw) is str:
            if _CANONICAL_UUID_TEXT.fullmatch(raw) is None:
                raise ValueError(f"{cls.__name__} must be canonical UUID TEXT(36)")
            parsed = UUID(raw)
        else:
            raise TypeError(
                f"{cls.__name__} must be parsed from str or uuid.UUID, not {type(raw).__name__}"
            )
        if parsed.version != 4:
            raise ValueError(f"{cls.__name__} requires a UUID4 value")
        return cls(parsed)

    @property
    def text(self) -> str:
        """Canonical lowercase hyphenated TEXT(36)."""

        return str(self.value)

    def __str__(self) -> str:
        return self.text


@final
class PersonId(_CanonicalUuid4):
    """Stable person subject identity."""

    __slots__ = ()


@final
class SpaceId(_CanonicalUuid4):
    """Stable space subject identity."""

    __slots__ = ()


@final
class IdentityBindingId(_CanonicalUuid4):
    """Stable person-to-external-account binding identity."""

    __slots__ = ()


@final
class SpaceBindingId(_CanonicalUuid4):
    """Stable space-to-external-space binding identity."""

    __slots__ = ()


@final
class PresenceId(_CanonicalUuid4):
    """Stable Yuki platform-account identity."""

    __slots__ = ()


@final
class ConversationId(_CanonicalUuid4):
    """Stable canonical conversation identity."""

    __slots__ = ()


@final
class PrincipalId(_CanonicalUuid4):
    """Stable control-plane principal identity."""

    __slots__ = ()


@final
class RequestId(_CanonicalUuid4):
    """Stable request or correlation identity."""

    __slots__ = ()


type BindingId = IdentityBindingId | SpaceBindingId


@dataclass(frozen=True, slots=True)
class _CanonicalGeneration:
    """Nominal non-negative generation counter."""

    value: int

    def __post_init__(self) -> None:
        if type(self.value) is not int:
            raise TypeError(f"{type(self).__name__} value must be an int")
        if self.value < 0:
            raise ValueError(f"{type(self).__name__} value must be non-negative")

    def incremented(self) -> Self:
        """Return the next generation of the same type."""

        return type(self)(self.value + 1)


@final
class ConversationGeneration(_CanonicalGeneration):
    """Conversation reset generation. Route takeover must not use this type."""

    __slots__ = ()


@final
class RouteGeneration(_CanonicalGeneration):
    """Business-route generation. Conversation reset must not use this type."""

    __slots__ = ()


@final
class ConnectionGeneration(_CanonicalGeneration):
    """In-memory gateway connection generation."""

    __slots__ = ()
