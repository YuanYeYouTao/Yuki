"""Current SQL authorization and paid embedding accounting share one read path."""

import pytest
from sqlalchemy import delete, select
from tests.support.user_profiles_helpers import observe_members
from tests.unit.test_memory_partition import _owners
from tests.unit.test_memory_retrieval import _query

from qq_ai_bot.identity.db_models import IdentityBindingModel
from qq_ai_bot.memory.authorized_scope import AuthorizedMemoryScope
from qq_ai_bot.memory.embedding.fake import FakeEmbeddingProvider
from qq_ai_bot.memory.embedding.metrics import MemoryEmbeddingMetrics
from qq_ai_bot.memory.embedding.models import EmbeddingUsage
from qq_ai_bot.memory.embedding.provider import EmbeddingProviderError
from qq_ai_bot.memory.embedding.query_cache import QueryEmbeddingCache
from qq_ai_bot.memory.embedding.repository import MemoryEmbeddingRepository
from qq_ai_bot.memory.embedding.semantic import MemorySemanticIndex
from qq_ai_bot.memory.embedding.text import EmbeddingDocumentBuilder, EmbeddingQueryBuilder
from qq_ai_bot.memory.enums import MemoryScopeType
from qq_ai_bot.memory.fts import SQLiteMemoryFTSIndex
from qq_ai_bot.memory.models import MemoryFactCreate
from qq_ai_bot.memory.read_scope import MemoryReadScopeResolver
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.models import MembershipModel


async def remember(facts, person_id, text):
    return await facts.remember(
        MemoryFactCreate(
            scope_type="person",
            canonical_subject_person_id=person_id,
            memory_key=text,
            category="test",
            content=text,
            source_type="automatic",
        )
    )


async def semantic(database, provider):
    vectors = MemoryEmbeddingRepository(database)
    profile = await vectors.ensure_profile(provider.profile)
    metrics = MemoryEmbeddingMetrics()
    cache = QueryEmbeddingCache(ttl_seconds=900, max_entries=20)
    retriever = MemoryRetriever(
        repository=MemoryFactRepository(database),
        lexical_index=SQLiteMemoryFTSIndex(database),
    )
    retriever.configure_semantic(
        semantic_index=MemorySemanticIndex(
            vectors, documents=EmbeddingDocumentBuilder(template_version=1, max_characters=1000)
        ),
        provider=provider,
        profile=profile,
        queries=EmbeddingQueryBuilder(max_characters=1000),
        metrics=metrics,
        query_cache=cache,
    )
    return retriever, metrics


@pytest.mark.parametrize("withdraw", [False, True])
async def test_explicit_selection_keeps_owner_and_current_sql_acl_after_embedding_wait(
    database, withdraw
):
    await observe_members(database, ("1001", "1002"))
    await observe_members(database, ("1003",), group_id="2002")
    people, spaces = await _owners(database)
    facts = MemoryFactService(MemoryFactRepository(database))
    original = await remember(facts, people["1002"], "三消 游戏")
    foreign = await remember(facts, people["1003"], "三消 游戏")
    selected = await MemoryReadScopeResolver(database).person(people["1001"], people["1002"])
    scope = AuthorizedMemoryScope(
        requester_person_id=people["1001"],
        allowed_scopes=(MemoryScopeType.PERSON,),
        resolved_targets=selected.targets,
    )

    class RebindingProvider(FakeEmbeddingProvider):
        async def embed_query(self, text):
            async with database.immediate_session() as writer:
                binding = await writer.scalar(
                    select(IdentityBindingModel).where(
                        IdentityBindingModel.platform == "qq",
                        IdentityBindingModel.external_account_id == "1002",
                    )
                )
                binding.person_id = people["1003"]
                if withdraw:
                    await writer.execute(
                        delete(MembershipModel).where(
                            MembershipModel.canonical_person_id == people["1002"],
                            MembershipModel.canonical_space_id == spaces["2001"],
                        )
                    )
            return await super().embed_query(text)

    retriever, _ = await semantic(database, RebindingProvider())
    result = await retriever.retrieve_authorized(
        _query("三消 游戏").model_copy(update={"semantic_enabled": True}), scope, limit=20
    )
    assert [hit.fact.id for hit in result.hits] == ([] if withdraw else [original.id])
    assert foreign.id not in {hit.fact.id for hit in result.hits}


async def test_short_words_in_mixed_query_use_current_authorized_sql(database):
    await observe_members(database, ("1001",))
    await observe_members(database, ("1002",), group_id="2002")
    people, _ = await _owners(database)
    facts = MemoryFactService(MemoryFactRepository(database))
    own = await remember(facts, people["1001"], "一起玩三消游戏")
    foreign = await remember(facts, people["1002"], "一起玩三消游戏")
    scope = AuthorizedMemoryScope(
        requester_person_id=people["1001"], allowed_scopes=(MemoryScopeType.PERSON,)
    )
    retriever = MemoryRetriever(
        repository=facts.repository, lexical_index=SQLiteMemoryFTSIndex(database)
    )
    for text in ("三消 游戏", "三消 alpha", "三消"):
        result = await retriever.retrieve_authorized(_query(text), scope, limit=20)
        assert [hit.fact.id for hit in result.hits] == [own.id]
        assert foreign.id not in {hit.fact.id for hit in result.hits}


async def test_authorized_search_accounts_real_embedding_and_only_reuses_vector(database):
    await observe_members(database, ("1001",))
    people, _ = await _owners(database)
    facts = MemoryFactService(MemoryFactRepository(database))

    class UsageProvider(FakeEmbeddingProvider):
        async def embed_query(self, text):
            result = await super().embed_query(text)
            return result.model_copy(
                update={"usage": EmbeddingUsage(input_count=1, input_tokens=17)}
            )

    provider = UsageProvider()
    retriever, metrics = await semantic(database, provider)
    query = _query("三消 游戏").model_copy(update={"semantic_enabled": True})
    scope = AuthorizedMemoryScope(
        requester_person_id=people["1001"], allowed_scopes=(MemoryScopeType.PERSON,)
    )
    assert not (await retriever.retrieve_authorized(query, scope, limit=20)).hits
    created = await remember(facts, people["1001"], "三消 游戏")
    fresh = await retriever.retrieve_authorized(query, scope, limit=20)
    assert [hit.fact.id for hit in fresh.hits] == [created.id]
    snapshot = metrics.snapshot()
    assert provider.query_requests == snapshot.query_embedding_requests == 1
    assert snapshot.query_embedding_cache_hits == 1
    assert snapshot.query_embedding_input_tokens == 17 and snapshot.query_embedding_failures == 0
    provider._error = EmbeddingProviderError("synthetic_failure", "offline", retryable=False)
    await retriever.retrieve_authorized(
        query.model_copy(update={"text": "different", "normalized_text": "different"}),
        scope,
        limit=20,
    )
    assert provider.query_requests == 2
    assert metrics.snapshot().query_embedding_requests == 2
    assert metrics.snapshot().query_embedding_failures == 1
