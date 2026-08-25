"""Control principal. Receives an already-resolved canonical Person only."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import final

from qq_ai_bot.control_plane.capabilities import (
    is_forbidden_control_capability,
    is_protocol_capability,
    normalize_capability_id,
)
from qq_ai_bot.domain.identity import PersonId, PrincipalId

_ROLE_TOKEN = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")


@final
class PrincipalSource(StrEnum):
    QQ = "qq"
    CLI = "cli"
    FUTURE_WEB = "future_web"
    SYSTEM = "system"


def _normalize_roles(values: object) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise TypeError("roles must be a collection of tokens")
    if not isinstance(values, Iterable):
        raise TypeError("roles must be a collection of tokens")
    normalized: set[str] = set()
    for item in values:
        if type(item) is not str:
            raise TypeError("role items must be str")
        token = item.strip().casefold()
        if not token or _ROLE_TOKEN.fullmatch(token) is None:
            raise ValueError("illegal role")
        normalized.add(token)
    return frozenset(normalized)


def _normalize_capabilities(values: object) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise TypeError("granted_capabilities must be a collection of tokens")
    if not isinstance(values, Iterable):
        raise TypeError("granted_capabilities must be a collection of tokens")
    normalized: set[str] = set()
    for item in values:
        token = normalize_capability_id(item)
        if is_forbidden_control_capability(token) or not is_protocol_capability(token):
            raise ValueError("forbidden capability")
        normalized.add(token)
    return frozenset(normalized)


@final
@dataclass(frozen=True, slots=True)
class ControlPrincipal:
    """Authenticated control actor bound to at most one canonical Person.

    C9 does not resolve QQ numbers or Bindings and does not invent Person
    identities. Multiple independent QQ-resolved principals may coexist.
    """

    principal_id: PrincipalId
    person_id: PersonId | None
    source: PrincipalSource
    roles: frozenset[str]
    granted_capabilities: frozenset[str]
    authenticated: bool
    active: bool

    def __init__(
        self,
        *,
        principal_id: PrincipalId,
        person_id: PersonId | None,
        source: PrincipalSource,
        roles: Iterable[str] = (),
        granted_capabilities: Iterable[str] = (),
        authenticated: bool,
        active: bool,
    ) -> None:
        if type(principal_id) is not PrincipalId:
            raise TypeError("principal_id must be PrincipalId")
        if person_id is not None and type(person_id) is not PersonId:
            raise TypeError("person_id must be PersonId or None")
        if type(source) is not PrincipalSource:
            raise TypeError("source must be PrincipalSource")
        if type(authenticated) is not bool:
            raise TypeError("authenticated must be a bool")
        if type(active) is not bool:
            raise TypeError("active must be a bool")
        if source is PrincipalSource.QQ and person_id is None:
            raise ValueError("qq principal requires a resolved person_id")
        object.__setattr__(self, "principal_id", principal_id)
        object.__setattr__(self, "person_id", person_id)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "roles", _normalize_roles(roles))
        object.__setattr__(
            self, "granted_capabilities", _normalize_capabilities(granted_capabilities)
        )
        object.__setattr__(self, "authenticated", authenticated)
        object.__setattr__(self, "active", active)

    def allows(self, capability: object) -> bool:
        """Default-deny authorization check. Invalid tokens are denied."""

        if not self.authenticated or not self.active:
            return False
        try:
            token = normalize_capability_id(capability)
        except (TypeError, ValueError):
            return False
        if is_forbidden_control_capability(token) or not is_protocol_capability(token):
            return False
        return token in self.granted_capabilities
