"""Small deterministic builders shared by Memory V2 contract tests."""

from __future__ import annotations

from yuki.memory.enums import MemoryKind, MemoryScopeType, MemorySourceType
from yuki.memory.models import MemoryFactCreate


def person_fact(user_id: str, content: str, *, key: str = "test-fact") -> MemoryFactCreate:
    return MemoryFactCreate(
        scope_type=MemoryScopeType.PERSON,
        subject_user_id=user_id,
        kind=MemoryKind.FACT,
        memory_key=key,
        category="fact",
        content=content,
        source_type=MemorySourceType.AUTOMATIC,
    )
