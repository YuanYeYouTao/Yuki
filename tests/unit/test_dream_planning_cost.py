"""Unchanged incremental partitions need no pair work; changed ones keep neighbors."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.conftest import make_settings

from yuki.memory.dream.repository import DreamCandidate, DreamCandidateLoad
from yuki.memory.dream.service import DreamService
from yuki.memory.embedding.codec import Float32VectorCodec
from yuki.memory.embedding.models import EmbeddingVector
from yuki.memory.enums import (
    MemoryKind,
    MemoryScopeType,
    MemorySourceType,
    MemoryStatus,
    SelfMemoryVisibility,
)
from yuki.memory.models import MemoryFact


def planner_case():
    at = datetime(2026, 10, 20, tzinfo=UTC)
    candidates = tuple(
        DreamCandidate(
            fact=MemoryFact(
                id=identity,
                scope_type=MemoryScopeType.SELF,
                visibility_type=SelfMemoryVisibility.GLOBAL,
                kind=MemoryKind.FACT,
                memory_key=f"fact:{identity}",
                category="profile",
                content=f"fact {identity}",
                normalized_content=f"fact {identity}",
                importance=3,
                confidence=1,
                source_type=MemorySourceType.AUTOMATIC,
                status=MemoryStatus.ACTIVE,
                created_at=at,
                updated_at=at,
            ),
            bot_user_id="self",
            vector=EmbeddingVector(values=(1.0, 0.0), dimensions=2),
            signature=f"signature-{identity}",
        )
        for identity in range(1, 5)
    )
    planner = object.__new__(DreamService)
    planner._settings = make_settings("sqlite+aiosqlite:///:memory:")
    planner._codec = Float32VectorCodec()
    planner._repository = SimpleNamespace(
        checkpoint_map=AsyncMock(return_value={item.fact.id: item.signature for item in candidates})
    )
    return planner, DreamCandidateLoad(candidates, len(candidates), 0, 0)


async def test_incremental_clusters_skip_unchanged_facts_and_keep_changed_neighbors():
    planner, loaded = planner_case()
    clusters, isolated = await planner._clusters(loaded, incremental=True)
    assert clusters == isolated == ()
    full, _ = await planner._clusters(loaded, incremental=False)
    planner._repository.checkpoint_map.return_value.pop(1)
    incremental, isolated = await planner._clusters(loaded, incremental=True)
    assert incremental == full
    assert {item.fact.id for cluster in incremental for item in cluster} == {1, 2, 3, 4}
    assert isolated == ()
