"""Immutable identity/route/control capability descriptors.

Existing PermissionCatalog / ConfigRegistry remain the
catalog sources. This module is only a descriptor table plus a pure
projection boundary. It is not a fourth registry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, final

from qq_ai_bot.control_plane.tokens import MAX_CAPABILITY_ID_LENGTH

_CAPABILITY_TOKEN = re.compile(r"\A[a-z][a-z0-9_]*(?:[.:][a-z][a-z0-9_]*)*\Z")
_SEGMENT_SPLIT = re.compile(r"[.:]+")
_LEGAL_FIXED_TOOLS: Final[frozenset[str]] = frozenset({"web_search"})
_SECRET_VERBS: Final[frozenset[str]] = frozenset({"read", "write", "get", "set", "value", "reveal"})
_SQL_VERBS: Final[frozenset[str]] = frozenset({"sql", "query", "execute", "raw"})
_INVOCATION_VERBS: Final[frozenset[str]] = frozenset(
    {"run", "call", "execute", "tool", "invoke", "arbitrary"}
)
_MANAGEMENT_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "read",
        "metadata",
        "doctor",
        "outbox",
        "status",
        "health",
        "enable",
        "disable",
        "approve",
        "retry",
        "refresh",
        "reconnect",
        "mutate",
    }
)
DENIED_CAPABILITY_ID: Final[str] = "control.denied"


@final
class CapabilityFamily(StrEnum):
    IDENTITY = "identity"
    ROUTE = "route"
    CONTROL = "control"
    CONVERSATION = "conversation"


@final
class CapabilitySensitivity(StrEnum):
    METADATA_READ = "metadata_read"
    EXTERNAL_ID_READ = "external_id_read"
    CONTENT_READ = "content_read"
    MUTATE = "mutate"
    DESTRUCTIVE = "destructive"


_READ_SENSITIVITY: Final[frozenset[CapabilitySensitivity]] = frozenset(
    {
        CapabilitySensitivity.METADATA_READ,
        CapabilitySensitivity.EXTERNAL_ID_READ,
        CapabilitySensitivity.CONTENT_READ,
    }
)
_WRITE_SENSITIVITY: Final[frozenset[CapabilitySensitivity]] = frozenset(
    {
        CapabilitySensitivity.MUTATE,
        CapabilitySensitivity.DESTRUCTIVE,
    }
)


def capability_segments(token: str) -> tuple[str, ...]:
    """Split a normalized capability on ``.`` and ``:``."""

    return tuple(part for part in _SEGMENT_SPLIT.split(token) if part)


def normalize_capability_id(value: object) -> str:
    if type(value) is not str:
        raise TypeError("capability id must be a str")
    token = value.strip().casefold()
    if len(token) > MAX_CAPABILITY_ID_LENGTH:
        raise ValueError("illegal capability id")
    if not token or _CAPABILITY_TOKEN.fullmatch(token) is None:
        raise ValueError("illegal capability id")
    return token


def _has_adjacent_database_sql(parts: tuple[str, ...]) -> bool:
    for index, part in enumerate(parts):
        if part != "database":
            continue
        neighbors: list[str] = []
        if index > 0:
            neighbors.append(parts[index - 1])
        if index + 1 < len(parts):
            neighbors.append(parts[index + 1])
        if any(neighbor in _SQL_VERBS for neighbor in neighbors):
            return True
    return False


def _segment_combines_database_sql(part: str) -> bool:
    bits = tuple(part.split("_"))
    return "database" in bits and any(verb in bits for verb in _SQL_VERBS)


def _token_bits(parts: tuple[str, ...]) -> tuple[str, ...]:
    bits: list[str] = []
    for part in parts:
        bits.extend(part.split("_"))
    return tuple(bits)


def _bits_include(parts: tuple[str, ...], tokens: frozenset[str]) -> bool:
    return any(bit in tokens for bit in _token_bits(parts))


def is_forbidden_control_capability(capability_id: str) -> bool:
    """Intrinsic danger only. Unapproved management stays reject-by-protocol."""

    token = capability_id.strip().casefold()
    if token in _LEGAL_FIXED_TOOLS:
        return False
    parts = capability_segments(token)
    if not parts:
        return True
    if parts[0] == "web_search":
        return True
    if any(part.startswith("onebot") or part.startswith("call_onebot_api") for part in parts):
        return True
    if any(part in {"sql", "raw_sql"} or _segment_combines_database_sql(part) for part in parts):
        return True
    if _has_adjacent_database_sql(parts):
        return True
    if parts[0] == "secret" or ("secret" in parts and any(part in _SECRET_VERBS for part in parts)):
        return True
    if "plugin" in parts or "mcp" in parts:
        if _bits_include(parts, _INVOCATION_VERBS):
            return True
        if not _bits_include(parts, _MANAGEMENT_TOKENS):
            return True
    return False


def is_protocol_capability(capability_id: str) -> bool:
    """Grantable only when listed on the reviewed table or the fixed toolset."""

    token = capability_id.strip().casefold()
    if token in _LEGAL_FIXED_TOOLS:
        return True
    return token in CONTROL_CAPABILITY_IDS


@final
@dataclass(frozen=True, slots=True)
class ControlCapabilityDescriptor:
    """Immutable control-protocol capability. Safe metadata only."""

    id: str
    family: CapabilityFamily
    sensitivity: CapabilitySensitivity
    mutating: bool

    def __post_init__(self) -> None:
        if type(self.family) is not CapabilityFamily:
            raise TypeError("family must be CapabilityFamily")
        if type(self.sensitivity) is not CapabilitySensitivity:
            raise TypeError("sensitivity must be CapabilitySensitivity")
        if type(self.mutating) is not bool:
            raise TypeError("mutating must be a bool")
        token = normalize_capability_id(self.id)
        object.__setattr__(self, "id", token)
        if is_forbidden_control_capability(token):
            raise ValueError("forbidden capability")
        parts = capability_segments(token)
        if not parts:
            raise ValueError("illegal capability id")
        try:
            family = CapabilityFamily(parts[0])
        except ValueError as exc:
            raise ValueError("capability family mismatch") from exc
        if family is not self.family:
            raise ValueError("capability family mismatch")
        if self.sensitivity in _READ_SENSITIVITY and self.mutating:
            raise ValueError("sensitivity mismatch")
        if self.sensitivity in _WRITE_SENSITIVITY and not self.mutating:
            raise ValueError("sensitivity mismatch")


def _descriptor(
    capability_id: str,
    family: CapabilityFamily,
    sensitivity: CapabilitySensitivity,
    *,
    mutating: bool,
) -> ControlCapabilityDescriptor:
    return ControlCapabilityDescriptor(
        id=capability_id,
        family=family,
        sensitivity=sensitivity,
        mutating=mutating,
    )


CONTROL_CAPABILITY_DESCRIPTORS: Final[tuple[ControlCapabilityDescriptor, ...]] = (
    _descriptor(
        "control.work.mutate",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "control.environment.file.mutate",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "control.terminal.mutate",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "control.terminal.content.read",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.CONTENT_READ,
        mutating=False,
    ),
    _descriptor(
        "control.workspace.metadata.read",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.METADATA_READ,
        mutating=False,
    ),
    _descriptor(
        "control.workspace.content.read",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.CONTENT_READ,
        mutating=False,
    ),
    _descriptor(
        "control.automation.content.read",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.CONTENT_READ,
        mutating=False,
    ),
    _descriptor(
        "control.execution.metadata.read",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.METADATA_READ,
        mutating=False,
    ),
    _descriptor(
        "control.execution.content.read",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.CONTENT_READ,
        mutating=False,
    ),
    _descriptor(
        "control.chat.metadata.read",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.METADATA_READ,
        mutating=False,
    ),
    _descriptor(
        "control.chat.content.read",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.CONTENT_READ,
        mutating=False,
    ),
    _descriptor(
        "identity.person.read",
        CapabilityFamily.IDENTITY,
        CapabilitySensitivity.METADATA_READ,
        mutating=False,
    ),
    _descriptor(
        "identity.person.enable",
        CapabilityFamily.IDENTITY,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "identity.person.disable",
        CapabilityFamily.IDENTITY,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "identity.person.forget",
        CapabilityFamily.IDENTITY,
        CapabilitySensitivity.DESTRUCTIVE,
        mutating=True,
    ),
    _descriptor(
        "identity.binding.read",
        CapabilityFamily.IDENTITY,
        CapabilitySensitivity.METADATA_READ,
        mutating=False,
    ),
    _descriptor(
        "identity.binding.read_external",
        CapabilityFamily.IDENTITY,
        CapabilitySensitivity.EXTERNAL_ID_READ,
        mutating=False,
    ),
    _descriptor(
        "identity.binding.attach",
        CapabilityFamily.IDENTITY,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "identity.space.read",
        CapabilityFamily.IDENTITY,
        CapabilitySensitivity.METADATA_READ,
        mutating=False,
    ),
    _descriptor(
        "identity.space.enable",
        CapabilityFamily.IDENTITY,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "identity.space.disable",
        CapabilityFamily.IDENTITY,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "identity.space.require_mention",
        CapabilityFamily.IDENTITY,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "identity.space.binding.attach",
        CapabilityFamily.IDENTITY,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "identity.presence.read",
        CapabilityFamily.IDENTITY,
        CapabilitySensitivity.METADATA_READ,
        mutating=False,
    ),
    _descriptor(
        "identity.presence.register",
        CapabilityFamily.IDENTITY,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "identity.presence.start",
        CapabilityFamily.IDENTITY,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "identity.presence.stop",
        CapabilityFamily.IDENTITY,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "identity.presence.set_ingest",
        CapabilityFamily.IDENTITY,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "identity.membership.read",
        CapabilityFamily.IDENTITY,
        CapabilitySensitivity.METADATA_READ,
        mutating=False,
    ),
    _descriptor(
        "conversation.metadata.read",
        CapabilityFamily.CONVERSATION,
        CapabilitySensitivity.METADATA_READ,
        mutating=False,
    ),
    _descriptor(
        "route.read", CapabilityFamily.ROUTE, CapabilitySensitivity.METADATA_READ, mutating=False
    ),
    _descriptor("route.set", CapabilityFamily.ROUTE, CapabilitySensitivity.MUTATE, mutating=True),
    _descriptor("route.pause", CapabilityFamily.ROUTE, CapabilitySensitivity.MUTATE, mutating=True),
    _descriptor(
        "route.resume", CapabilityFamily.ROUTE, CapabilitySensitivity.MUTATE, mutating=True
    ),
    _descriptor(
        "route.reconcile", CapabilityFamily.ROUTE, CapabilitySensitivity.MUTATE, mutating=True
    ),
    _descriptor(
        "control.system.read",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.METADATA_READ,
        mutating=False,
    ),
    _descriptor(
        "control.health.read",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.METADATA_READ,
        mutating=False,
    ),
    _descriptor(
        "control.audit.read",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.METADATA_READ,
        mutating=False,
    ),
    _descriptor(
        "control.operation.read",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.METADATA_READ,
        mutating=False,
    ),
    _descriptor(
        "control.operation.cancel",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "control.operation.retry",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "control.preference.read",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.METADATA_READ,
        mutating=False,
    ),
    _descriptor(
        "control.preference.mutate",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "control.group.mutate",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "control.private_access.mutate",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "control.config.file.content.read",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.CONTENT_READ,
        mutating=False,
    ),
    _descriptor(
        "control.config.file.mutate",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "control.config.read",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.METADATA_READ,
        mutating=False,
    ),
    _descriptor(
        "control.config.mutate",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "control.memory.metadata.read",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.METADATA_READ,
        mutating=False,
    ),
    _descriptor(
        "control.memory.content.read",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.CONTENT_READ,
        mutating=False,
    ),
    _descriptor(
        "control.memory.mutate",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "control.memory.rebuild",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "control.memory.dream",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "control.memory.maintenance",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "control.automation.read",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.METADATA_READ,
        mutating=False,
    ),
    _descriptor(
        "control.automation.mutate",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "control.plugin.config.content.read",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.CONTENT_READ,
        mutating=False,
    ),
    _descriptor(
        "control.plugin.config.mutate",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "control.plugin.read",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.METADATA_READ,
        mutating=False,
    ),
    _descriptor(
        "control.plugin.mutate",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
    _descriptor(
        "control.emoji.read",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.METADATA_READ,
        mutating=False,
    ),
    _descriptor(
        "control.emoji.mutate",
        CapabilityFamily.CONTROL,
        CapabilitySensitivity.MUTATE,
        mutating=True,
    ),
)

CONTROL_CAPABILITY_IDS: Final[frozenset[str]] = frozenset(
    item.id for item in CONTROL_CAPABILITY_DESCRIPTORS
)
if len(CONTROL_CAPABILITY_IDS) != len(CONTROL_CAPABILITY_DESCRIPTORS):
    raise ValueError("duplicate control capability id")
if any(is_forbidden_control_capability(item.id) for item in CONTROL_CAPABILITY_DESCRIPTORS):
    raise ValueError("control capability table contains a forbidden id")
if DENIED_CAPABILITY_ID in CONTROL_CAPABILITY_IDS:
    raise ValueError("denied placeholder must not be grantable")


def control_capability_descriptor(capability_id: str) -> ControlCapabilityDescriptor | None:
    token = normalize_capability_id(capability_id)
    return next((item for item in CONTROL_CAPABILITY_DESCRIPTORS if item.id == token), None)
