"""Resolve the original canonical source of an actorless SELF turn."""

from __future__ import annotations

from typing import Protocol

from yuki.memory.self_origin import SelfMemoryOrigin, resolve_self_origin
from yuki.persistence.database import Database


class MemoryPartitionLookup(Protocol):
    """Read the exact original SELF run and conversation."""

    async def resolve_self_origin(
        self, *, initiative_run_id: str, canonical_conversation_id: str, group_id: str | None = None
    ) -> SelfMemoryOrigin: ...


class DatabaseMemoryPartitionLookup:
    """One-session adapter over the original SELF source reader."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def resolve_self_origin(
        self, *, initiative_run_id: str, canonical_conversation_id: str, group_id: str | None = None
    ) -> SelfMemoryOrigin:
        async with self._database.read_session() as session:
            return await resolve_self_origin(
                session,
                initiative_run_id=initiative_run_id,
                canonical_conversation_id=canonical_conversation_id,
                group_id=group_id,
            )
