"""Immutable scope and turn-generation contracts."""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import InboundMessage


@dataclass(frozen=True, slots=True)
class ConversationTurnSnapshot:
    """Database and in-process versions captured when a turn is admitted.

    ``scope_key`` is the canonical conversation's fixed primary alias.
    ``transport_scope_key`` is the current
    ingress alias when it differs; ledger ``bot_user_id`` stays on transport.
    """

    scope_id: int
    scope_key: str
    generation: int
    trigger_event_id: int
    coordinator_version: int
    transport_scope_key: str | None = None

    def __post_init__(self) -> None:
        if self.scope_id < 1:
            raise ValueError("scope_id must be positive")
        if not self.scope_key:
            raise ValueError("scope_key must not be empty")
        if self.generation < 1:
            raise ValueError("generation must be positive")
        if self.trigger_event_id < 1:
            raise ValueError("trigger_event_id must be positive")
        if self.coordinator_version < 1:
            raise ValueError("coordinator_version must be positive")
        if self.transport_scope_key is not None and not self.transport_scope_key:
            raise ValueError("transport_scope_key must not be empty")


def runtime_conversation_key(
    *,
    identity: ConversationScope,
    turn: ConversationTurnSnapshot | None = None,
    inbound: InboundMessage | None = None,
    primary_alias: str | None = None,
) -> str:
    """Single projection for coordinator, locks, hydrate, tools, and plugins."""

    if turn is not None:
        return turn.scope_key
    if primary_alias:
        return primary_alias
    if inbound is not None and inbound.legacy_conversation_key:
        return inbound.legacy_conversation_key
    if inbound is not None and inbound.conversation_id:
        raise ValueError("v2 conversation is missing primary runtime key")
    return identity.key


def plugin_conversation_key(message: InboundMessage, identity: ConversationScope) -> str:
    """SDK conversation_key: the canonical conversation's fixed primary alias."""

    return runtime_conversation_key(identity=identity, inbound=message)


def turn_covers_scope_key(turn: ConversationTurnSnapshot, scope_key: str) -> bool:
    """True when ``scope_key`` is this turn's runtime identity or its transport alias."""

    if scope_key == turn.scope_key:
        return True
    return turn.transport_scope_key is not None and scope_key == turn.transport_scope_key


def snapshot_transport_key(turn: ConversationTurnSnapshot) -> str:
    return turn.transport_scope_key or turn.scope_key


def turn_matches_hydrated_scope(
    turn: ConversationTurnSnapshot,
    *,
    scope_id: int,
    generation: int,
    transport_key: str,
    runtime_key: str | None,
) -> bool:
    """Strict fence: scope_id, generation, runtime key, and transport key."""

    resolved_runtime = runtime_key or transport_key
    return (
        turn.scope_id == scope_id
        and turn.generation == generation
        and turn.scope_key == resolved_runtime
        and transport_key == snapshot_transport_key(turn)
    )


__all__ = [
    "ConversationScope",
    "ConversationTurnSnapshot",
    "plugin_conversation_key",
    "runtime_conversation_key",
    "snapshot_transport_key",
    "turn_covers_scope_key",
    "turn_matches_hydrated_scope",
]
