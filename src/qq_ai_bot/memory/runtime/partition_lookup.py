"""Explicit Memory partition lookup port.

``conversation_key`` is an identity-epoch + binding value, not a retrieval
concern. Production opens one session and calls
``resolve_memory_partition_from_scope`` so v1/v2 gates stay in one place.
"""

from __future__ import annotations

from typing import Protocol

from qq_ai_bot.memory.partition import resolve_memory_partition_from_scope
from qq_ai_bot.persistence.database import Database


class MemoryPartitionLookup(Protocol):
    """Resolve one Memory conversation_key from trusted inbound scope."""

    async def resolve_from_scope(
        self,
        *,
        group_id: str | None,
        private_peer_user_id: str | None,
    ) -> str: ...


class DatabaseMemoryPartitionLookup:
    """One-session adapter over ``resolve_memory_partition_from_scope``."""

    def __init__(self, database: Database) -> None:
        self._database = database

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
