"""Small, labeled Chinese retrieval probe; synthetic facts, real SQLite search path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import update

from qq_ai_bot.identity.canonical_repository import active_person_id_for
from qq_ai_bot.memory.authorized_scope import AuthorizedMemoryScope
from qq_ai_bot.memory.context import MEMORY_GROUNDING_RULE
from qq_ai_bot.memory.embedding.codec import Float32VectorCodec
from qq_ai_bot.memory.embedding.fake import FakeEmbeddingProvider
from qq_ai_bot.memory.embedding.models import EmbeddingVector, MemoryEmbeddingProfileRecord
from qq_ai_bot.memory.embedding.provider import EmbeddingProviderError
from qq_ai_bot.memory.embedding.repository import MemoryEmbeddingRepository
from qq_ai_bot.memory.embedding.semantic import AuthorizedSemanticCandidate, MemorySemanticIndex
from qq_ai_bot.memory.embedding.text import EmbeddingDocumentBuilder, EmbeddingQueryBuilder
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryKind,
    MemoryRetrievalMode,
    MemoryScopeType,
    MemorySourceType,
    MemoryTargetRole,
    SelfMemoryVisibility,
)
from qq_ai_bot.memory.fts import SQLiteMemoryFTSIndex
from qq_ai_bot.memory.models import MemoryEntityTarget, MemoryFactCreate, MemoryQuery
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.memory.runtime.query_plane import apply_total_hit_limit
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import MemoryEmbeddingModel
from qq_ai_bot.persistence.people_repository import PeopleRepository


@dataclass(frozen=True)
class LabeledQuery:
    text: str
    relevant: frozenset[str]


CASES = (
    LabeledQuery("橘猫团子是谁养的", frozenset({"cat"})),
    LabeledQuery("读书会哪天讨论科幻小说", frozenset({"book_club"})),
    LabeledQuery("阿澈在读书会负责什么", frozenset({"recorder"})),
    LabeledQuery("小雨喜欢吃什么蛋糕", frozenset({"cake"})),
    LabeledQuery("Yuki 回复风格要怎样", frozenset({"self_global"})),
    LabeledQuery("小雨喜欢的菲尔兹奖是哪一届", frozenset()),
)


@pytest.mark.asyncio
async def test_search_memory_labeled_chinese_precision_probe(database: Database) -> None:
    people = PeopleRepository(database)
    await people.observe(user_id="1001", nickname="小雨", group_id="2001")
    await people.observe(user_id="1002", nickname="阿澈", group_id="2001")
    await people.observe(user_id="1003", nickname="路人", group_id="2002")

    service = MemoryFactService(MemoryFactRepository(database))
    examples = (
        ("cat", "小雨现在养一只叫团子的橘猫", "pet:cat", "1001", None, MemoryScopeType.PERSON),
        ("cake", "小雨喜欢蓝莓芝士蛋糕", "food:cake", "1001", None, MemoryScopeType.PERSON),
        ("exam", "阿澈正在备考雅思", "study:ielts", "1002", None, MemoryScopeType.PERSON),
        (
            "book_club",
            "读书会每周三晚上九点讨论科幻小说",
            "group:book_club",
            None,
            "2001",
            MemoryScopeType.GROUP,
        ),
        (
            "recorder",
            "阿澈在读书会负责会议记录",
            "role:recorder",
            "1002",
            "2001",
            MemoryScopeType.PERSON_GROUP,
        ),
        (
            "decoy_weekday",
            "小雨每周三晚上九点练习钢琴",
            "hobby:piano",
            "1001",
            None,
            MemoryScopeType.PERSON,
        ),
        (
            "secret",
            "摄影群每周三晚上九点展示私密照片",
            "group:secret",
            None,
            "2002",
            MemoryScopeType.GROUP,
        ),
        ("stranger", "路人拥有一只名叫团子的橘猫", "pet:cat", "1003", None, MemoryScopeType.PERSON),
    )
    fact_ids: dict[int, str] = {}
    for label, content, key, user_id, group_id, scope in examples:
        fact = await service.remember(
            MemoryFactCreate(
                scope_type=scope,
                subject_user_id=user_id,
                group_id=group_id,
                kind=MemoryKind.FACT,
                memory_key=key,
                category="probe",
                content=content,
                source_type=MemorySourceType.EXPLICIT,
            )
        )
        fact_ids[fact.id] = label
    self_fact = await service.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.SELF,
            visibility_type=SelfMemoryVisibility.GLOBAL,
            kind=MemoryKind.PREFERENCE,
            memory_key="self:style",
            category="probe",
            content="Yuki 回答应简短清楚",
            source_type=MemorySourceType.EXPLICIT,
            authority=MemoryAuthority.AGENT_REFLECTION,
        )
    )
    fact_ids[self_fact.id] = "self_global"

    async with database.sessions() as session:
        requester = await active_person_id_for(session, "1001")
    assert requester is not None
    scope = AuthorizedMemoryScope(
        requester_person_id=requester,
        current_private_person_id=requester,
        allowed_scopes=(
            MemoryScopeType.PERSON,
            MemoryScopeType.PERSON_GROUP,
            MemoryScopeType.GROUP,
            MemoryScopeType.SELF,
        ),
    )
    retriever = MemoryRetriever(
        repository=service.repository,
        lexical_index=SQLiteMemoryFTSIndex(database),
    )
    current_person = MemoryEntityTarget(
        scope_type=MemoryScopeType.PERSON,
        subject_user_id="1001",
        role=MemoryTargetRole.CURRENT_PERSON,
        block_id="current_person",
    )
    explicit_query = MemoryQuery(
        text="zzzznonexistentmemoryzzzz",
        normalized_text="zzzznonexistentmemoryzzzz",
        mode=MemoryRetrievalMode.RELEVANT,
        targets=(current_person,),
        candidate_limit=10,
        limit_per_target=10,
        always_on_explicit_preference_limit=0,
        query_term_limit=12,
        semantic_enabled=True,
    )
    missing = await retriever.retrieve(explicit_query)
    assert not missing.hits
    assert not missing.truncated
    assert not missing.exhaustive
    assert missing.partial_reason == "semantic_not_configured"
    bounded_explicit = await retriever.retrieve(
        explicit_query.model_copy(
            update={
                "text": "小雨",
                "normalized_text": "小雨",
                "candidate_limit": 1,
                "semantic_enabled": False,
            }
        )
    )
    assert bounded_explicit.candidate_count == 1
    assert bounded_explicit.truncated and not bounded_explicit.exhaustive
    assert bounded_explicit.partial_reason == "explicit_candidate_budget"
    exact_budget = await retriever.retrieve(
        explicit_query.model_copy(
            update={
                "text": "蛋糕",
                "normalized_text": "蛋糕",
                "candidate_limit": 1,
                "semantic_enabled": False,
            }
        )
    )
    assert len(exact_budget.hits) == 1
    assert not exact_budget.truncated and exact_budget.exhaustive
    limited_output = await retriever.retrieve(
        explicit_query.model_copy(
            update={
                "text": "小雨",
                "normalized_text": "小雨",
                "semantic_enabled": False,
                "limit_per_target": 1,
            }
        )
    )
    assert len(limited_output.hits) == 1
    assert limited_output.truncated and not limited_output.exhaustive
    assert limited_output.partial_reason == "explicit_result_limit"
    full_output = await retriever.retrieve(
        explicit_query.model_copy(
            update={"text": "小雨", "normalized_text": "小雨", "semantic_enabled": False}
        )
    )
    total_limited = apply_total_hit_limit(full_output, 1)
    assert len(total_limited.hits) == 1
    assert total_limited.truncated and not total_limited.exhaustive
    assert total_limited.partial_reason == "result_limit"
    results: dict[str, tuple[str, ...]] = {}
    queries: dict[str, MemoryQuery] = {}
    for case in CASES:
        query = MemoryQuery(
            text=case.text,
            normalized_text=case.text.casefold(),
            mode=MemoryRetrievalMode.RELEVANT,
            targets=(),
            candidate_limit=50,
            limit_per_target=10,
            always_on_explicit_preference_limit=0,
            query_term_limit=12,
            semantic_enabled=False,
        )
        queries[case.text] = query
        result = await retriever.retrieve_authorized(
            query,
            scope,
            limit=10,
        )
        labels = tuple(fact_ids[hit.fact.id] for hit in result.hits)
        assert "secret" not in labels and "stranger" not in labels
        results[case.text] = labels

    # The lexical baseline and limitations are recorded in the taskbook.
    # Keep the reliable behavior as a lower bound without freezing the current
    # no-answer false positive as a required result.
    answerable = tuple(case for case in CASES if case.relevant)
    recalled = sum(bool(set(results[case.text]) & case.relevant) for case in answerable)
    assert recalled >= 4
    assert results["读书会哪天讨论科幻小说"][0] == "book_club"
    assert results["阿澈在读书会负责什么"][0] == "recorder"
    person_only = scope.model_copy(update={"allowed_scopes": (MemoryScopeType.PERSON,)})
    restricted = await retriever.retrieve_authorized(
        queries["阿澈在读书会负责什么"], person_only, limit=10
    )
    assert "recorder" not in {fact_ids[hit.fact.id] for hit in restricted.hits}
    bounded = await retriever.retrieve_authorized(
        queries["读书会哪天讨论科幻小说"].model_copy(update={"candidate_limit": 1}),
        scope,
        limit=10,
    )
    assert bounded.truncated and not bounded.exhaustive
    output_bounded = await retriever.retrieve_authorized(
        queries["小雨喜欢吃什么蛋糕"].model_copy(
            update={"text": "小雨", "normalized_text": "小雨"}
        ),
        scope,
        limit=1,
    )
    assert output_bounded.ranked_count > 1
    assert output_bounded.truncated and not output_bounded.exhaustive
    assert output_bounded.partial_reason == "global_result_limit"

    # A deterministic semantic candidate can recover a word-order miss;
    # this checks merge/fallback mechanics, not the quality of real vectors.
    cat_id = next(fact_id for fact_id, label in fact_ids.items() if label == "cat")
    stranger_id = next(fact_id for fact_id, label in fact_ids.items() if label == "stranger")
    semantic = Mock(spec=MemorySemanticIndex)
    semantic.search_authorized = AsyncMock(
        return_value=(
            (
                AuthorizedSemanticCandidate(stranger_id, 0.95, 1),
                AuthorizedSemanticCandidate(cat_id, 0.9, 2),
            ),
            False,
            True,
        )
    )
    provider = FakeEmbeddingProvider()
    retriever.configure_semantic(
        semantic_index=semantic,
        provider=provider,
        profile=MemoryEmbeddingProfileRecord(
            id=1, profile=provider.profile, created_at=datetime.now(UTC)
        ),
        queries=EmbeddingQueryBuilder(max_characters=200),
    )
    cat_query = queries["橘猫团子是谁养的"].model_copy(update={"semantic_enabled": True})
    hybrid = await retriever.retrieve_authorized(cat_query, scope, limit=10)
    assert [fact_ids[hit.fact.id] for hit in hybrid.hits] == ["cat"]
    assert hybrid.hits[0].sources == ("semantic",)
    assert hybrid.semantic_status == "ready"

    broken = FakeEmbeddingProvider(
        error=EmbeddingProviderError("test_outage", "test outage", retryable=True)
    )
    retriever.configure_semantic(
        semantic_index=semantic,
        provider=broken,
        profile=MemoryEmbeddingProfileRecord(
            id=1, profile=broken.profile, created_at=datetime.now(UTC)
        ),
        queries=EmbeddingQueryBuilder(max_characters=200),
    )
    degraded = await retriever.retrieve_authorized(cat_query, scope, limit=10)
    assert degraded.hits == ()
    assert degraded.semantic_status == "test_outage"
    assert degraded.semantic_degraded and not degraded.exhaustive


@pytest.mark.asyncio
async def test_global_semantic_search_reports_incomplete_index(database: Database) -> None:
    await PeopleRepository(database).observe(user_id="1001", nickname="访客", group_id="2001")
    await PeopleRepository(database).observe(user_id="1002", nickname="陌生人", group_id="2002")
    service = MemoryFactService(MemoryFactRepository(database))
    facts = []
    for content in ("喜欢蓝色风筝", "喜欢绿色风筝"):
        facts.append(
            await service.remember(
                MemoryFactCreate(
                    scope_type=MemoryScopeType.PERSON,
                    subject_user_id="1001",
                    kind=MemoryKind.FACT,
                    memory_key=f"kite:{len(facts)}",
                    category="probe",
                    content=content,
                    source_type=MemorySourceType.EXPLICIT,
                )
            )
        )
    await service.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1002",
            kind=MemoryKind.FACT,
            memory_key="kite:private",
            category="probe",
            content="陌生人有一只紫色风筝",
            source_type=MemorySourceType.EXPLICIT,
        )
    )
    async with database.sessions() as session:
        requester = await active_person_id_for(session, "1001")
    assert requester is not None
    scope = AuthorizedMemoryScope(
        requester_person_id=requester,
        current_private_person_id=requester,
        allowed_scopes=(MemoryScopeType.PERSON,),
    )
    provider = FakeEmbeddingProvider(vectors={"完全不同的问题": (1.0, 0.0, 0.0, 0.0)})
    vectors = MemoryEmbeddingRepository(database)
    profile = await vectors.ensure_profile(provider.profile)
    documents = EmbeddingDocumentBuilder(template_version=1, max_characters=1000)
    codec = Float32VectorCodec()

    async def write_vector(fact_index: int, *, stale: bool = False) -> None:
        fact = facts[fact_index]
        async with database.sessions() as session, session.begin():
            session.add(
                MemoryEmbeddingModel(
                    fact_id=fact.id,
                    profile_id=profile.id,
                    content_hash=("0" * 64 if stale else documents.content_hash(fact)),
                    vector_blob=codec.encode(
                        EmbeddingVector(values=(1.0, 0.0, 0.0, 0.0), dimensions=4)
                    ),
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )

    retriever = MemoryRetriever(
        repository=service.repository,
        lexical_index=SQLiteMemoryFTSIndex(database),
    )
    retriever.configure_semantic(
        semantic_index=MemorySemanticIndex(vectors, documents=documents),
        provider=provider,
        profile=profile,
        queries=EmbeddingQueryBuilder(max_characters=200),
    )
    query = MemoryQuery(
        text="完全不同的问题",
        normalized_text="完全不同的问题",
        mode=MemoryRetrievalMode.RELEVANT,
        targets=(),
        candidate_limit=10,
        limit_per_target=10,
        always_on_explicit_preference_limit=0,
        query_term_limit=12,
        semantic_enabled=True,
        semantic_min_similarity=0.35,
    )

    await write_vector(0)
    partial = await retriever.retrieve_authorized(query, scope, limit=10)
    assert partial.semantic_status == "index_incomplete"
    assert partial.partial_reason == "semantic_index_incomplete"
    assert not partial.exhaustive
    await write_vector(1, stale=True)
    stale = await retriever.retrieve_authorized(query, scope, limit=10)
    assert stale.semantic_status == "index_incomplete"
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(MemoryEmbeddingModel)
            .where(MemoryEmbeddingModel.fact_id == facts[1].id)
            .values(content_hash=documents.content_hash(facts[1]))
        )
    complete = await retriever.retrieve_authorized(query, scope, limit=10)
    assert complete.semantic_status == "ready"
    assert complete.partial_reason is None
    assert complete.exhaustive


@pytest.mark.asyncio
async def test_global_two_character_followup_is_bounded_and_authorized(database: Database) -> None:
    await PeopleRepository(database).observe(user_id="1001", nickname="甲", group_id="2001")
    await PeopleRepository(database).observe(user_id="1002", nickname="乙", group_id="2002")
    service = MemoryFactService(MemoryFactRepository(database))
    answer = await service.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind=MemoryKind.FACT,
            memory_key="term:revision",
            category="probe",
            content="把自建称为自用节点",
            source_type=MemorySourceType.EXPLICIT,
        )
    )
    await service.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1002",
            kind=MemoryKind.FACT,
            memory_key="term:revision",
            category="probe",
            content="把自建称为私人机场",
            source_type=MemorySourceType.EXPLICIT,
        )
    )
    async with database.sessions() as session:
        requester = await active_person_id_for(session, "1001")
    assert requester is not None
    scope = AuthorizedMemoryScope(
        requester_person_id=requester,
        current_private_person_id=requester,
        allowed_scopes=(MemoryScopeType.PERSON,),
    )
    retriever = MemoryRetriever(
        repository=service.repository,
        lexical_index=SQLiteMemoryFTSIndex(database),
    )

    def query(text: str) -> MemoryQuery:
        return MemoryQuery(
            text=text,
            normalized_text=text,
            mode=MemoryRetrievalMode.RELEVANT,
            targets=(),
            candidate_limit=10,
            limit_per_target=10,
            always_on_explicit_preference_limit=0,
            query_term_limit=12,
            semantic_enabled=False,
        )

    broad = await retriever.retrieve_authorized(query("自建改称了什么"), scope, limit=10)
    assert broad.hits == ()
    focused = await retriever.retrieve_authorized(query("自建"), scope, limit=10)
    assert [hit.fact.id for hit in focused.hits] == [answer.id]
    assert "两个汉字" in MEMORY_GROUNDING_RULE
    assert "计划、答应和创建待办不能证明事情已完成" in MEMORY_GROUNDING_RULE
