"""Unchanged incremental partitions need no pair work; changed ones keep neighbors."""

import random
from dataclasses import replace
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
            normalized_vector=EmbeddingVector(values=(1.0, 0.0), dimensions=2),
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


def original_groups(planner, loaded, checkpoints, incremental):
    """The former dense-pair algorithm is a fixed behavioral oracle."""
    rows = sorted(loaded.candidates, key=lambda item: item.fact.id)
    changed = {
        row.fact.id
        for row in rows
        if not incremental or checkpoints.get(row.fact.id) != row.signature
    }
    if incremental and not changed:
        return ()
    vectors = {row.fact.id: row.normalized_vector for row in rows}
    weights = {}
    degree = dict.fromkeys(vectors, 0)
    threshold = planner._settings.memory_dream_similarity_threshold
    for position, left in enumerate(rows):
        for right in rows[position + 1 :]:
            score = planner._codec.dot_normalized(vectors[left.fact.id], vectors[right.fact.id])
            weights[min(left.fact.id, right.fact.id), max(left.fact.id, right.fact.id)] = score
            if score >= threshold:
                degree[left.fact.id] += 1
                degree[right.fact.id] += 1

    def similarity(left, right):
        return 1.0 if left == right else weights[min(left, right), max(left, right)]

    remaining, result = set(vectors), []
    while remaining:
        seed = max(remaining, key=lambda identity: (degree[identity], -identity))
        remaining.remove(seed)
        group = [seed]
        for identity in sorted(
            remaining, key=lambda identity: (-similarity(seed, identity), identity)
        ):
            if len(group) >= planner._settings.memory_dream_max_cluster_size:
                break
            if all(similarity(identity, member) >= threshold for member in group):
                group.append(identity)
                remaining.remove(identity)
        for removed in group:
            for identity in remaining:
                if similarity(removed, identity) >= threshold:
                    degree[identity] -= 1
        group = tuple(sorted(group))
        if not incremental or changed.intersection(group):
            result.append(group)
    return tuple(sorted(result))


def test_sparse_graph_matches_dense_oracle_with_ties_negative_edges_and_zero_degree_tail():
    rng = random.Random(20261026)
    planner, original = planner_case()
    template = original.candidates[0]
    for case in range(120):
        count = rng.randrange(0, 16)
        candidates = tuple(
            replace(
                template,
                fact=template.fact.model_copy(update={"id": identity}),
                normalized_vector=Float32VectorCodec.normalize(
                    EmbeddingVector(
                        values=(1.0, 0.0)
                        if case % 5 == 0
                        else tuple(rng.uniform(-1, 1) for _ in range(2)),
                        dimensions=2,
                    )
                ),
                signature=f"signature-{identity}",
            )
            for identity in range(1, count + 1)
        )
        loaded = DreamCandidateLoad(candidates, count, 0, 0)
        planner._settings = SimpleNamespace(
            memory_dream_similarity_threshold=(-1.0, 0.0, 0.8, 1.0)[case % 4],
            memory_dream_max_cluster_size=1 + case % 6,
        )
        checkpoints = {row.fact.id: row.signature for row in candidates if row.fact.id % 3}
        for incremental in (False, True):
            clusters, isolated = planner._cluster_candidates(
                loaded, checkpoints=checkpoints, incremental=incremental
            )
            actual = tuple(tuple(row.fact.id for row in group) for group in clusters)
            assert actual == original_groups(planner, loaded, checkpoints, incremental)
            assert isolated == ()
