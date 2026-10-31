"""The typed editable document and the actual saved/runtime configuration agree."""

import tomllib

from tests.integration.test_embedding_connection_bundle import evidence
from tests.integration.test_model_connection_http import model_http as model_http
from tests.unit.test_admin_api import admin as admin
from tests.unit.test_admin_api import command

from yuki.config import Settings
from yuki.memory.dream.repository import DreamRepository
from yuki.memory.dream.service import prepare_full_core
from yuki.memory.embedding.runtime import MemoryEmbeddingRuntime
from yuki.memory.fts import SQLiteMemoryFTSIndex
from yuki.memory.repository import MemoryFactRepository
from yuki.memory.retrieval import MemoryRetriever
from yuki.memory.service import MemoryFactService
from yuki.model_runtime.connections import EmbeddingConnection


async def test_profile_unset_fields_round_trip_over_real_http(model_http):
    client = model_http.client
    current = (await client.get("/admin/v1/models")).json()["data"]
    document = current["document"]
    document["profiles"]["main"].update(
        max_input_tokens=None, wire_options=None, reasoning_effort=None
    )
    saved = await client.post(
        "/admin/v1/models/apply", json=command(current["revision"], document=document)
    )
    assert saved.status_code == 200, saved.text
    raw = tomllib.loads(model_http.profiles.read_text())
    assert not {"max_input_tokens", "wire_options", "reasoning_effort"}.intersection(
        raw["profiles"]["main"]
    )
    assert not model_http.state["calls"]
    evidence(
        "unset-profile-fields",
        {"response": saved.json(), "stored_profile": raw["profiles"]["main"]},
    )


async def test_disable_unconfigured_embedding_needs_no_invented_identity(model_http):
    client = model_http.client
    current = (await client.get("/admin/v1/models")).json()["data"]
    document = current["document"]
    document["embedding"] = {"enabled": False}
    saved = await client.post(
        "/admin/v1/models/apply", json=command(current["revision"], document=document)
    )
    assert saved.status_code == 200, saved.text
    read = (await client.get("/admin/v1/models")).json()["data"]
    assert read["document"]["embedding"]["enabled"] is False
    assert not model_http.state["calls"]
    evidence(
        "disabled-empty-embedding",
        {"response": saved.json(), "connection": read["document"]["embedding"]},
    )


async def test_dream_uses_the_explicit_embedding_enabled_fact(database):
    settings = Settings(
        _env_file=None, memory_embedding_enabled=False, memory_embedding_worker_enabled=False
    )
    facts = MemoryFactService(MemoryFactRepository(database))
    retriever = MemoryRetriever(
        repository=facts.repository, lexical_index=SQLiteMemoryFTSIndex(database)
    )
    runtime = MemoryEmbeddingRuntime(
        settings=settings,
        database=database,
        facts=facts,
        retriever=retriever,
        connection=EmbeddingConnection(
            provider="openai_compatible",
            base_url="http://127.0.0.1:1/v1",
            model="fixture",
            dimensions=2,
        ),
    )
    await runtime.start()
    try:
        assert runtime.enabled and not settings.memory_embedding_enabled
        plan = await prepare_full_core(
            settings=settings, repository=DreamRepository(database), embeddings=runtime
        )
        assert plan.snapshot_max_fact_id == 0
        evidence(
            "dream-explicit-embedding",
            {
                "enabled": runtime.enabled,
                "profile_id": runtime.profile_id,
                "snapshot_max_fact_id": plan.snapshot_max_fact_id,
            },
        )
    finally:
        await runtime.close()
