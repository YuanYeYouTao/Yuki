"""Concrete per-turn memory session (R2 C5)."""

from __future__ import annotations

from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.memory.runtime.capability_view import build_capability_view
from qq_ai_bot.memory.runtime.contract import (
    MemoryTurnContract,
    active_read_contract,
    forbidden_contract,
)
from qq_ai_bot.memory.runtime.partition_lookup import MemoryPartitionLookup
from qq_ai_bot.memory.runtime.resolver import (
    MemoryAccessDecision,
    MemoryAccessReason,
    resolve_inbound_scope,
    resolve_memory_access,
)
from qq_ai_bot.memory.runtime.state import (
    MemorySessionState,
)
from qq_ai_bot.runtime.contracts import MemoryCapabilityView
from qq_ai_bot.runtime.errors import InvalidTurnContextError
from qq_ai_bot.runtime.keys import ResolvedMemoryScope
from qq_ai_bot.runtime.origin import TurnOrigin


class TurnMemorySession:
    """I/O session that Chat may call.  It never holds ChatService."""

    def __init__(
        self,
        *,
        decision: MemoryAccessDecision,
        scope: ResolvedMemoryScope,
    ) -> None:
        self._decision = decision
        self._state = MemorySessionState(decision.contract, scope)

    @classmethod
    def open(
        cls,
        *,
        inbound: InboundMessage,
        origin: TurnOrigin,
        memory_available: bool = True,
    ) -> TurnMemorySession:
        if origin is TurnOrigin.SELF_INITIATIVE:
            # SELF continues under its own run identity via open_self_origin.
            raise InvalidTurnContextError("invalid turn principal")
        scope = resolve_inbound_scope(inbound)
        decision = resolve_memory_access(
            origin=origin,
            memory_available=memory_available,
        )
        return cls(
            decision=decision,
            scope=scope,
        )

    @property
    def contract(self) -> MemoryTurnContract:
        return self._state.contract

    @classmethod
    async def open_self_origin(
        cls,
        *,
        initiative_run_id: str,
        canonical_conversation_id: str,
        identity: ConversationScope,
        partition_lookup: MemoryPartitionLookup,
        memory_available: bool = True,
    ) -> TurnMemorySession:
        source = await partition_lookup.resolve_self_origin(
            initiative_run_id=initiative_run_id,
            canonical_conversation_id=canonical_conversation_id,
            group_id=identity.group_id,
        )
        if (
            identity.scope_type is not ScopeType.GROUP
            or identity.group_id != source.group_id
            or identity.bot_user_id != source.bot_user_id
        ):
            raise ValueError("SELF Memory scope does not match its initiative")
        decision = MemoryAccessDecision(
            contract=active_read_contract(
                persistent_write_allowed=True,
            )
            if memory_available
            else forbidden_contract(),
            reason=MemoryAccessReason.SELF_ORIGIN,
        )
        return cls(
            decision=decision,
            scope=ResolvedMemoryScope.for_group(source.group_id),
        )

    @property
    def scope(self) -> ResolvedMemoryScope:
        return self._state.scope

    @property
    def reason(self) -> MemoryAccessReason:
        return self._decision.reason

    def capability_view(self) -> MemoryCapabilityView:
        return build_capability_view(
            self._state.contract,
            transition_revision=1,
        )

    async def close(self) -> None:
        self._state.close()
