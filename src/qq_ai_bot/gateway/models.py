"""In-memory connection records. Bot handles stay off every snapshot DTO."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import final


@final
class ConnectionHealth(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    AMBIGUOUS = "ambiguous"


@final
@dataclass(frozen=True, slots=True)
class ConnectionSnapshot:
    """Secret-free view of one live or historical connection slot."""

    connection_id: str
    gateway_instance_id: str
    provider: str
    platform: str
    external_account_id: str
    presence_id: str | None
    generation: int
    healthy: bool
    capabilities: frozenset[str]


@final
@dataclass(frozen=True, slots=True)
class PresenceConnectionSnapshot:
    """Per-Presence runtime snapshot for Control Query."""

    health: ConnectionHealth
    generation: int | None
    connection_id: str | None
    gateway_instance_id: str | None
    live_count: int
    provider: str | None
    capabilities: frozenset[str]
