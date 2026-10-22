"""Dream sends local aliases while original paid preview proofs remain readable."""

import hashlib
import json

import pytest
from sqlalchemy import func, select
from tests.conftest import make_settings
from tests.support.event_seed import seed_event
from tests.support.model_executor import InjectedModelExecutor
from tests.unit.test_memory_dream import _empty_dream_statistics, _services

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.conversations import ScopeType
from yuki.llm.fake import FakeLLMProvider
from yuki.memory.dream.db_models import (
    MemoryDreamClusterPreviewModel,
    MemoryDreamOperationModel,
)
from yuki.memory.dream.models import DreamAction, DreamOperationType, DreamOutput, DreamRunMode
from yuki.memory.dream.repository import fact_signature
from yuki.memory.dream.service import DreamService
from yuki.memory.embedding.fake import FakeEmbeddingProvider
from yuki.memory.embedding.runtime import MemoryEmbeddingRuntime
from yuki.memory.enums import (
    MemoryAuthority,
    MemoryEvidenceRelation,
    MemoryKind,
    MemoryScopeType,
    MemorySourceType,
    SelfMemoryVisibility,
)
from yuki.memory.fts import SQLiteMemoryFTSIndex
from yuki.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from yuki.memory.retrieval import MemoryRetriever
from yuki.persistence.database import Database
from yuki.services.concurrency import ConcurrencyManager


def _service(database, provider):
    mutations, facts, ledger, dreams = _services(database)
    settings = make_settings(
        database.url, memory_embedding_enabled=False, memory_dream_max_model_calls_per_run=1
    )
    embeddings = MemoryEmbeddingRuntime(
        settings=settings,
        database=database,
        facts=facts,
        retriever=MemoryRetriever(
            repository=facts.repository, lexical_index=SQLiteMemoryFTSIndex(database)
        ),
        provider=FakeEmbeddingProvider(),
    )
    service = DreamService(
        settings=settings,
        repository=dreams,
        facts=facts,
        mutations=mutations,
        embeddings=embeddings,
        models=InjectedModelExecutor(provider),
        concurrency=ConcurrencyManager(),
    )
    return service, facts, ledger, dreams


async def _scene(database, provider, *, owner):
    service, facts, ledger, dreams = _service(database, provider)
    rows = []
    for index in range(2):
        private = owner == "self_private"
        event, _ = await seed_event(
            ledger,
            bot_user_id="8000",
            platform_message_id=f"owner-proof-{owner}-{index}",
            scope_type=ScopeType.PRIVATE if private else ScopeType.GROUP,
            sender_user_id="1001",
            direction="inbound",
            content=f"original memory and evidence {index}",
            group_id=None if private else "3001",
        )
        async with database.read_session() as reader:
            conversation = await reader.get(
                CanonicalConversationModel, event.canonical_conversation_id
            )
            space_id = conversation.space_id
        own_person = owner == "person_group"
        row = await facts.remember(
            MemoryFactCreate(
                scope_type=MemoryScopeType.PERSON_GROUP if own_person else MemoryScopeType.SELF,
                canonical_subject_person_id=event.author_person_id if own_person else None,
                canonical_subject_space_id=space_id if own_person else None,
                visibility_type=(
                    None
                    if own_person
                    else SelfMemoryVisibility.PRIVATE
                    if private
                    else SelfMemoryVisibility.GROUP
                ),
                canonical_visibility_person_id=event.author_person_id if private else None,
                canonical_visibility_space_id=(space_id if owner == "self_group" else None),
                kind=MemoryKind.FACT,
                memory_key=f"owner-proof:{index}",
                category="profile",
                content=event.content,
                source_type=MemorySourceType.AUTOMATIC,
                authority=MemoryAuthority.SELF_REPORT,
            ),
            evidence=MemoryEvidenceCreate(
                event_id=event.id,
                source_speaker_user_id="1001",
                relation=MemoryEvidenceRelation.SELF_STATEMENT,
                authority=MemoryAuthority.SELF_REPORT,
                excerpt=event.content,
            ),
        )
        rows.append(row)
    rows = tuple(rows)
    run = await dreams.create_run(
        mode=DreamRunMode.FULL,
        statistics=_empty_dream_statistics(),
        clusters=(
            (
                "owner-proof",
                "partition",
                "8000",
                "fact",
                tuple(row.id for row in rows),
                service._cluster_fingerprint(rows),
            ),
        ),
        snapshot_max_fact_id=rows[-1].id,
        actor_user_id=None,
        scheduled_slot=None,
    )
    assert await dreams.start_run(run.public_id)
    cluster = await dreams.claim_next_cluster(run.public_id)
    return service, facts, dreams, run, cluster, rows


@pytest.mark.parametrize("owner", ["person_group", "self_private", "self_group"])
async def test_dream_provider_uses_local_fact_aliases_without_transport_owner_fields(
    database, owner
):
    provider = FakeLLMProvider(
        lambda _: '{"actions":[{"operation":"keep","source_refs":["memory_1","memory_2"]}]}'
    )
    service, _, _, run, cluster, rows = await _scene(database, provider, owner=owner)
    preview = await service.preview_cluster(run.public_id, cluster.id)
    assert preview.actions[0].source_refs == ("memory_1", "memory_2")
    assert len(provider.requests) == 1
    payload = json.loads(next(m.content for m in provider.requests[0].messages if m.role == "user"))
    assert not {
        "subject_user_id",
        "group_id",
        "visibility_user_id",
        "visibility_group_id",
    }.intersection(payload)
    assert payload["scope_type"] == rows[0].scope_type.value
    assert payload["kind"] == "fact"
    assert [row["ref"] for row in payload["memories"]] == ["memory_1", "memory_2"]
    assert [row["content"] for row in payload["memories"]] == [row.content for row in rows]
    assert [row["evidence"][0]["excerpt"] for row in payload["memories"]] == [
        row.content for row in rows
    ]
    if owner != "person_group":
        assert payload["visibility_type"] == rows[0].visibility_type.value


@pytest.mark.parametrize("owner", ["person_group", "self_private", "self_group"])
@pytest.mark.parametrize("transfer", [False, True])
async def test_cold_dream_reuses_original_paid_owner_proof_at_request_limit(
    database, owner, transfer
):
    provider = FakeLLMProvider()
    service, facts, dreams, run, cluster, rows = await _scene(database, provider, owner=owner)
    payload, _, current_fingerprint = await service._input(rows)
    # Freeze the historical proof codec separately from the new provider projection.
    legacy_input = {
        "scope_type": rows[0].scope_type.value,
        "subject_user_id": rows[0].subject_user_id,
        "group_id": rows[0].group_id,
        "visibility_type": rows[0].visibility_type.value if rows[0].visibility_type else None,
        "visibility_user_id": rows[0].visibility_user_id,
        "visibility_group_id": rows[0].visibility_group_id,
        "kind": "fact",
        "memories": [row.model_dump(mode="json") for row in payload.memories],
    }
    evidence = [await facts.list_evidence(row.id, limit=None) for row in rows]
    proof = {
        "input": legacy_input,
        "evidence": [
            (row.id, tuple(item.model_dump(mode="json") for item in sources))
            for row, sources in zip(rows, evidence, strict=True)
        ],
        "facts": tuple(
            (fact_signature(row), service._mutations._dream_partition(row), row.review_state)
            for row in rows
        ),
    }
    original_fingerprint = hashlib.sha256(
        json.dumps(proof, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert current_fingerprint != original_fingerprint
    assert await dreams.reserve_model_call(
        run_public_id=run.public_id, cluster_id=cluster.id, maximum=1
    )
    original_output = DreamOutput(
        actions=(
            DreamAction(
                operation=DreamOperationType.MERGE,
                source_refs=("memory_1", "memory_2"),
                anchor_ref="memory_1",
            ),
        )
    )
    preview_id = await dreams.save_preview(
        cluster_id=cluster.id,
        source_fingerprint=original_fingerprint,
        proposal=original_output,
        model_calls=1,
        source_characters=sum(len(row.content) for row in rows),
        output_characters=0,
    )
    async with database.read_session() as reader:
        original = await reader.scalar(
            select(MemoryDreamClusterPreviewModel).where(
                MemoryDreamClusterPreviewModel.public_id == preview_id
            )
        )
        original_preview = (
            original.id,
            original.public_id,
            original.source_fingerprint,
            original.proposal_json,
            original.schema_version,
            original.model_calls,
        )
    if transfer:
        from tests.support.memory_identity import transfer_account

        await transfer_account(database, "1001")
    await database.engine.dispose()

    def no_new_model(_request):
        raise AssertionError("the original paid preview must be reused after cold restore")

    cold_provider = FakeLLMProvider(no_new_model)
    cold_database = Database(database.url)
    try:
        cold_service, cold_facts, _, cold_dreams = _service(cold_database, cold_provider)
        cold_run = await cold_dreams.get_run(run.public_id)
        assert cold_run.model_calls == 1
        assert await cold_service.process_cluster(cold_run, cluster) == (0, 1, True)
        assert cold_provider.requests == []
        assert (await cold_dreams.get_run(run.public_id)).model_calls == 1
        async with cold_database.read_session() as reader:
            applied = await reader.get(MemoryDreamClusterPreviewModel, original_preview[0])
            assert (
                applied.id,
                applied.public_id,
                applied.source_fingerprint,
                applied.proposal_json,
                applied.schema_version,
                applied.model_calls,
            ) == original_preview
            assert applied.status == "applied"
            assert (
                await reader.scalar(select(func.count()).select_from(MemoryDreamOperationModel))
                == 1
            )
        current = [await cold_facts.get_fact(row.id) for row in rows]
        assert sorted(row.status.value for row in current) == ["active", "superseded"]
        active = next(row for row in current if row.status.value == "active")
        assert cold_service._mutations._dream_partition(
            active
        ) == cold_service._mutations._dream_partition(rows[0])
        assert {item.event_id for item in await cold_facts.list_evidence(active.id)} == {
            item.event_id for sources in evidence for item in sources
        }
    finally:
        await cold_database.close()
