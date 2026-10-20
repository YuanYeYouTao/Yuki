"""Independent HTTP batches retain successful vectors and their original claims."""

import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import insert, select
from tests.support.canonical_identity import ensure_person

from yuki.memory.embedding.jobs import MemoryEmbeddingJobRepository
from yuki.memory.embedding.qwen import QwenDashScopeEmbeddingProvider
from yuki.memory.embedding.repository import MemoryEmbeddingRepository
from yuki.memory.embedding.text import EmbeddingDocumentBuilder
from yuki.memory.embedding.worker import MemoryEmbeddingWorker
from yuki.persistence.models import MemoryEmbeddingJobModel, MemoryEmbeddingModel, MemoryFactModel


@pytest.fixture
async def embedding_batches(database):
    requested = []
    failed = []

    def respond(request):
        texts = tuple(json.loads(request.content)["input"]["texts"])
        requested.append(texts)
        if len(texts) == 1 and not failed:
            failed.append(texts[0])
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "output": {
                    "embeddings": [
                        {"text_index": index, "embedding": [1.0] + [0.0] * 1023}
                        for index in range(len(texts))
                    ]
                }
            },
        )

    provider = QwenDashScopeEmbeddingProvider(
        base_url="https://embedding.invalid",
        api_key="offline",
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    profile = await MemoryEmbeddingRepository(database).ensure_profile(provider.profile)
    count = provider.capabilities.max_batch_size + 1
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        person = await ensure_person(session, "1001")
        await session.execute(
            insert(MemoryFactModel),
            [
                dict(
                    scope_type="person",
                    canonical_subject_person_id=person,
                    kind="fact",
                    category="profile",
                    memory_key=f"batch:{index}",
                    content=f"document {index}",
                    normalized_content=f"document {index}",
                    source_type="explicit",
                    authority="explicit",
                    status="active",
                    conflict_state="clear",
                    review_state="verified",
                    created_at=now,
                    updated_at=now,
                )
                for index in range(count)
            ],
        )
    jobs = MemoryEmbeddingJobRepository(
        database,
        profile=profile,
        documents=EmbeddingDocumentBuilder(template_version=1, max_characters=4000),
    )
    await jobs.reconcile()
    worker = MemoryEmbeddingWorker(
        provider=provider, jobs=jobs, interval_seconds=1, claim_limit=count, retry_initial_seconds=0
    )
    try:
        yield SimpleNamespace(
            provider=provider,
            jobs=jobs,
            worker=worker,
            count=count,
            requested=requested,
            failed=failed,
        )
    finally:
        await provider.close()


async def test_successful_http_batch_survives_other_batch_failure(database, embedding_batches):
    env = embedding_batches
    # A processing unit may already be one protocol-sized batch. Stop at the
    # first failed HTTP response, before asking the worker to retry any work.
    for _ in range(2):
        await env.worker.process_once()
        if env.failed:
            break
    assert len(env.failed) == 1
    async with database.sessions() as session:
        jobs = tuple(await session.scalars(select(MemoryEmbeddingJobModel)))
        vectors = tuple(await session.scalars(select(MemoryEmbeddingModel)))
    expected = env.provider.capabilities.max_batch_size
    assert len(vectors) == expected
    assert Counter(row.status for row in jobs) == {"done": expected, "pending": 1}
    assert all(row.profile_id == env.jobs.profile.id and row.attempts == 1 for row in jobs)
    by_fact = {row.fact_id: row for row in jobs}
    assert all(row.content_hash == by_fact[row.fact_id].content_hash for row in vectors)


async def test_retry_only_reissues_failed_documents(database, embedding_batches):
    env = embedding_batches
    for _ in range(3):
        await env.worker.process_once()
    async with database.sessions() as session:
        jobs = tuple(await session.scalars(select(MemoryEmbeddingJobModel)))
        vectors = tuple(await session.scalars(select(MemoryEmbeddingModel)))
    assert len(vectors) == env.count and all(row.status == "done" for row in jobs)
    paid = Counter(text for batch in env.requested for text in batch)
    assert len(env.failed) == 1 and paid.pop(env.failed[0]) == 2
    assert set(paid.values()) == {1}


async def test_protocol_sized_claim_keeps_success_and_retries_only_failed_document(
    database, embedding_batches
):
    env = embedding_batches
    env.worker._claim_limit = env.provider.capabilities.max_batch_size
    for _ in range(3):
        await env.worker.process_once()
    async with database.sessions() as session:
        jobs = tuple(await session.scalars(select(MemoryEmbeddingJobModel)))
        vectors = tuple(await session.scalars(select(MemoryEmbeddingModel)))
    assert len(vectors) == env.count and all(row.status == "done" for row in jobs)
    assert all(row.profile_id == env.jobs.profile.id for row in vectors)
    paid = Counter(text for batch in env.requested for text in batch)
    assert paid.pop(env.failed[0]) == 2
    assert set(paid.values()) == {1}


async def test_protocol_sized_claim_drains_ready_batches_without_waiting_for_poll(
    database, embedding_batches, monkeypatch
):
    env = embedding_batches
    env.failed.append("disable simulated failure in this successful drain")
    env.worker._interval_seconds = 3600
    finished = asyncio.Event()
    embed = env.provider.embed_documents

    async def completed(texts):
        result = await embed(texts)
        if sum(len(batch) for batch in env.requested) == env.count:
            env.worker._stop.set()
            finished.set()
        return result

    monkeypatch.setattr(env.provider, "embed_documents", completed)
    await env.worker.start()
    try:
        async with asyncio.timeout(5):
            await finished.wait()
    finally:
        await env.worker.close()
    async with database.sessions() as reader:
        assert len(tuple(await reader.scalars(select(MemoryEmbeddingModel)))) == env.count
    assert [len(batch) for batch in env.requested] == [env.provider.capabilities.max_batch_size, 1]
