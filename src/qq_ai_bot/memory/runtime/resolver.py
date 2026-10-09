"""Trusted memory scope and access resolution.

Scope and the initial ``MemoryTurnContract`` are derived from host facts
only — never from model output or a phrase dictionary.  Ordinary natural
language uses the fixed authorized tool surface; structured read/write commands are
supplied by the command router, not guessed from user text.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.enums import MemoryRecallPurpose
from qq_ai_bot.memory.runtime.contract import (
    MemoryTurnContract,
    active_read_contract,
    forbidden_contract,
)
from qq_ai_bot.runtime.errors import InvalidTurnContextError
from qq_ai_bot.runtime.keys import ResolvedMemoryScope
from qq_ai_bot.runtime.origin import TurnOrigin

if TYPE_CHECKING:
    from qq_ai_bot.domain.messages import InboundMessage

_WRITE_ORIGINS = frozenset({TurnOrigin.USER_MESSAGE, TurnOrigin.AUTONOMOUS_GROUP})
_MESSAGE_ORIGINS = frozenset({TurnOrigin.USER_MESSAGE, TurnOrigin.AUTONOMOUS_GROUP})


def resolve_inbound_scope(inbound: InboundMessage) -> ResolvedMemoryScope:
    """Memory partition from the trusted inbound scope, never from message content.

    Group scenes map to the group partition; private scenes map to the
    trusted sender's private partition.
    """

    if not inbound.sender.user_id:
        raise InvalidTurnContextError("memory scope requires an actor user id")
    if inbound.scope_type is ScopeType.GROUP:
        if not inbound.group_id:
            raise InvalidTurnContextError("group scene requires a group id")
        return ResolvedMemoryScope.for_group(inbound.group_id)
    if inbound.group_id is not None:
        raise InvalidTurnContextError("private scene must not carry a group id")
    return ResolvedMemoryScope.for_private(inbound.sender.user_id)


class MemoryStructuredCommand(StrEnum):
    """Host-routed command kind.  Never derived from substring matching."""

    NONE = "none"
    READ = "read"
    WRITE = "write"


class MemoryAccessReason(StrEnum):
    """Content-free reason for the initial contract.  Safe to persist."""

    AUTHORITY_FORBIDDEN = "authority_forbidden"
    ORIGIN_RESTRICTED = "origin_restricted"
    STRUCTURED_WRITE_COMMAND = "structured_write_command"
    STRUCTURED_READ_COMMAND = "structured_read_command"
    ORDINARY_NATURAL_LANGUAGE = "ordinary_natural_language"
    ORIGIN_WRITE_DENIED = "origin_write_denied"
    SELF_ORIGIN = "self_origin"


@dataclass(frozen=True, slots=True)
class MemoryAccessDecision:
    """Resolver output: a valid contract plus why it was chosen."""

    contract: MemoryTurnContract
    reason: MemoryAccessReason
    retrieval_degraded: bool = False


def origin_allows_persistent_write(origin: TurnOrigin) -> bool:
    """User @ turns and admitted autonomous group turns may persist writes."""

    return origin in _WRITE_ORIGINS


def resolve_memory_access(
    *,
    origin: TurnOrigin,
    reply_present: bool,
    structured_command: MemoryStructuredCommand = MemoryStructuredCommand.NONE,
    memory_available: bool = True,
    retrieval_enabled: bool = True,
) -> MemoryAccessDecision:
    """Choose the initial memory contract from trusted host evidence.

    ``retrieval_enabled=false`` never becomes FORBIDDEN; it only marks the
    decision as retrieval-degraded so the query plane can use overview
    fallback. Images do not change explicit text-write authority; every mutation
    still validates its source, target, evidence, and original effect receipt.
    """

    degraded = not retrieval_enabled
    if not memory_available:
        return MemoryAccessDecision(
            contract=forbidden_contract(MemoryRecallPurpose.BACKGROUND),
            reason=MemoryAccessReason.AUTHORITY_FORBIDDEN,
            retrieval_degraded=degraded,
        )

    write_allowed = origin_allows_persistent_write(origin)
    if origin not in _MESSAGE_ORIGINS:
        return MemoryAccessDecision(
            contract=active_read_contract(
                persistent_write_allowed=False,
            ),
            reason=MemoryAccessReason.ORIGIN_RESTRICTED,
            retrieval_degraded=degraded,
        )

    if structured_command is MemoryStructuredCommand.WRITE:
        if write_allowed:
            return MemoryAccessDecision(
                contract=active_read_contract(MemoryRecallPurpose.CORRECT),
                reason=MemoryAccessReason.STRUCTURED_WRITE_COMMAND,
                retrieval_degraded=degraded,
            )
        return MemoryAccessDecision(
            contract=_passive_contract(reply_present, persistent_write_allowed=False),
            reason=MemoryAccessReason.ORIGIN_WRITE_DENIED,
            retrieval_degraded=degraded,
        )

    if structured_command is MemoryStructuredCommand.READ:
        return MemoryAccessDecision(
            contract=active_read_contract(persistent_write_allowed=write_allowed),
            reason=MemoryAccessReason.STRUCTURED_READ_COMMAND,
            retrieval_degraded=degraded,
        )

    reason = MemoryAccessReason.ORDINARY_NATURAL_LANGUAGE
    if not origin_allows_persistent_write(origin):
        reason = MemoryAccessReason.ORIGIN_WRITE_DENIED
    return MemoryAccessDecision(
        contract=_passive_contract(reply_present, persistent_write_allowed=write_allowed),
        reason=reason,
        retrieval_degraded=degraded,
    )


def _passive_contract(reply_present: bool, *, persistent_write_allowed: bool) -> MemoryTurnContract:
    purpose = MemoryRecallPurpose.CONTINUATION if reply_present else MemoryRecallPurpose.BACKGROUND
    return active_read_contract(purpose, persistent_write_allowed=persistent_write_allowed)
