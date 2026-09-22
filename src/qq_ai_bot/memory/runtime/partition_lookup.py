"""Explicit Memory partition lookup port.

``conversation_key`` is a canonical binding value, not a retrieval concern.
Production opens one session and calls ``resolve_memory_partition_from_scope``.
"""

from __future__ import annotations

from typing import Protocol

from qq_ai_bot.memory.partition import resolve_memory_partition_from_scope
from qq_ai_bot.memory.self_origin import SelfMemoryOrigin, resolve_self_origin
from qq_ai_bot.persistence.database import Database


class MemoryPartitionLookup(Protocol):
    """Resolve one Memory conversation_key from trusted inbound scope."""

    async def resolve_from_scope(
        self,
        *,
        group_id: str | None,
        private_peer_user_id: str | None,
    ) -> str: ...

    async def resolve_self_origin(
        self, *, initiative_run_id: str, canonical_conversation_id: str
    ) -> SelfMemoryOrigin: ...


class DatabaseMemoryPartitionLookup:
    """One-session adapter over ``resolve_memory_partition_from_scope``."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def resolve_self_origin(
        self, *, initiative_run_id: str, canonical_conversation_id: str
    ) -> SelfMemoryOrigin:
        async with self._database.sessions() as session:
            return await resolve_self_origin(
                session,
                initiative_run_id=initiative_run_id,
                canonical_conversation_id=canonical_conversation_id,
            )

    async def resolve_from_scope(
        self,
        *,
        group_id: str | None,
        private_peer_user_id: str | None,
    ) -> str:
        async with self._database.sessions() as session:
            partition = await resolve_memory_partition_from_scope(
                session,
                group_id=group_id,
                private_peer_user_id=private_peer_user_id,
            )
        return partition.value
