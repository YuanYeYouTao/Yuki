"""Query-driven Memory V2 retrieval, targeting, and scale regressions."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryKind,
    MemoryRetrievalMode,
    MemoryScopeType,
    MemorySourceType,
    MemoryTargetRole,
    SelfMemoryVisibility,
)
from qq_ai_bot.memory.errors import MemoryRetrievalError
from qq_ai_bot.memory.fts import SQLiteMemoryFTSIndex, build_safe_lexical_query
from qq_ai_bot.memory.models import (
    MemoryEntityTarget,
    MemoryFact,
    MemoryFactCreate,
    MemoryQuery,
)
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database


def _target(
    user_id: str,
    *,
    group_id: str | None = None,
    role: MemoryTargetRole | None = None,
) -> MemoryEntityTarget:
    return MemoryEntityTarget(
        role=role
        or (MemoryTargetRole.CURRENT_PERSON_GROUP if group_id else MemoryTargetRole.CURRENT_PERSON),
        scope_type=(MemoryScopeType.PERSON_GROUP if group_id else MemoryScopeType.PERSON),
        subject_user_id=user_id,
        group_id=group_id,
        block_id=f"target:{user_id}:{group_id or 'global'}",
    )


def _group_target(group_id: str) -> MemoryEntityTarget:
    return MemoryEntityTarget(
        role=MemoryTargetRole.CURRENT_GROUP,
        scope_type=MemoryScopeType.GROUP,
        group_id=group_id,
        block_id=f"group:{group_id}",
    )


def _query(
    value: str,
    *targets: MemoryEntityTarget,
    mode: MemoryRetrievalMode = MemoryRetrievalMode.RELEVANT,
    limit: int = 8,
) -> MemoryQuery:
    return MemoryQuery(
        text=value,
        normalized_text=value.casefold(),
        mode=mode,
        targets=targets,
        candidate_limit=50,
        limit_per_target=limit,
        query_term_limit=12,
        short_query_fallback_enabled=True,
    )


async def _remember(
    service: MemoryFactService,
    *,
    content: str,
    memory_key: str,
    user_id: str | None = "1001",
    group_id: str | None = None,
    scope: MemoryScopeType = MemoryScopeType.PERSON,
    kind: MemoryKind = MemoryKind.FACT,
    source: MemorySourceType = MemorySourceType.AUTOMATIC,
    category: str = "profile",
    importance: int = 3,
    confidence: float = 0.8,
    visibility_type: SelfMemoryVisibility | None = None,
    visibility_user_id: str | None = None,
    visibility_group_id: str | None = None,
    authority: MemoryAuthority = MemoryAuthority.SELF_REPORT,
) -> MemoryFact:
    return await service.remember(
        MemoryFactCreate(
            scope_type=scope,
            subject_user_id=user_id,
            group_id=group_id,
            visibility_type=visibility_type,
            visibility_user_id=visibility_user_id,
            visibility_group_id=visibility_group_id,
            kind=kind,
            memory_key=memory_key,
            category=category,
            content=content,
            importance=importance,
            confidence=confidence,
            source_type=source,
            authority=authority,
        )
    )


def _self_target(
    visibility: SelfMemoryVisibility,
    *,
    user_id: str | None = None,
    group_id: str | None = None,
) -> MemoryEntityTarget:
    return MemoryEntityTarget(
        role=MemoryTargetRole.CURRENT_SELF,
        scope_type=MemoryScopeType.SELF,
        visibility_type=visibility,
        visibility_user_id=user_id,
        visibility_group_id=group_id,
        block_id="current_self",
    )


@pytest.mark.asyncio
async def test_self_retrieval_hard_filters_global_and_current_visibility(
    database: Database,
) -> None:
    memories, retriever = _retriever(database)
    common = dict(
        content="Yuki 喜欢认真讨论记忆架构",
        user_id=None,
        group_id=None,
        scope=MemoryScopeType.SELF,
        category="self_preference",
        authority=MemoryAuthority.AGENT_REFLECTION,
    )
    global_fact = await _remember(
        memories,
        memory_key="self:global",
        visibility_type=SelfMemoryVisibility.GLOBAL,
        **common,
    )
    private_fact = await _remember(
        memories,
        memory_key="self:private:1001",
        visibility_type=SelfMemoryVisibility.PRIVATE,
        visibility_user_id="1001",
        **common,
    )
    other_private = await _remember(
        memories,
        memory_key="self:private:1002",
        visibility_type=SelfMemoryVisibility.PRIVATE,
        visibility_user_id="1002",
        **common,
    )
    group_fact = await _remember(
        memories,
        memory_key="self:group:2001",
        visibility_type=SelfMemoryVisibility.GROUP,
        visibility_group_id="2001",
        **common,
    )
    other_group = await _remember(
        memories,
        memory_key="self:group:2002",
        visibility_type=SelfMemoryVisibility.GROUP,
        visibility_group_id="2002",
        **common,
    )

    private_result = await retriever.retrieve(
        _query(
            "认真讨论记忆架构",
            _self_target(SelfMemoryVisibility.PRIVATE, user_id="1001"),
        )
    )
    assert {hit.fact.id for hit in private_result.hits} == {global_fact.id, private_fact.id}
    assert other_private.id not in {hit.fact.id for hit in private_result.hits}

    group_result = await retriever.retrieve(
        _query(
            "认真讨论记忆架构",
            _self_target(SelfMemoryVisibility.GROUP, group_id="2001"),
        )
    )
    assert {hit.fact.id for hit in group_result.hits} == {global_fact.id, group_fact.id}
    assert other_group.id not in {hit.fact.id for hit in group_result.hits}


def _retriever(database: Database) -> tuple[MemoryFactService, MemoryRetriever]:
    repository = MemoryFactRepository(database)
    return (
        MemoryFactService(repository),
        MemoryRetriever(
            repository=repository,
            lexical_index=SQLiteMemoryFTSIndex(database),
        ),
    )


@pytest.mark.asyncio
async def test_lexical_search_hard_filters_people_groups_and_person_groups(
    database: Database,
) -> None:
    memories, retriever = _retriever(database)
    zhang = await _remember(
        memories, content="喜欢数学竞赛", memory_key="hobby:math", user_id="1001"
    )
    await _remember(memories, content="喜欢数学竞赛", memory_key="hobby:math", user_id="1002")
    first_group = await _remember(
        memories,
        content="群里讨论数学竞赛",
        memory_key="topic:math",
        user_id=None,
        group_id="2001",
        scope=MemoryScopeType.GROUP,
    )
    await _remember(
        memories,
        content="群里讨论数学竞赛",
        memory_key="topic:math",
        user_id=None,
        group_id="2002",
        scope=MemoryScopeType.GROUP,
    )
    member = await _remember(
        memories,
        content="在本群喜欢数学竞赛",
        memory_key="member:math",
        user_id="1001",
        group_id="2001",
        scope=MemoryScopeType.PERSON_GROUP,
    )
    await _remember(
        memories,
        content="在另一个群喜欢数学竞赛",
        memory_key="member:math",
        user_id="1001",
        group_id="2002",
        scope=MemoryScopeType.PERSON_GROUP,
    )

    result = await retriever.retrieve(
        _query("数学竞赛", _target("1001"), _group_target("2001"), _target("1001", group_id="2001"))
    )

    assert {hit.fact.id for hit in result.hits} == {zhang.id, first_group.id, member.id}
    assert [block.target.scope_type for block in result.blocks] == [
        MemoryScopeType.PERSON,
        MemoryScopeType.GROUP,
        MemoryScopeType.PERSON_GROUP,
    ]


@pytest.mark.asyncio
async def test_short_query_and_unsafe_symbols_stay_inside_subject(database: Database) -> None:
    memories, retriever = _retriever(database)
    expected = await _remember(memories, content="住在杭州", memory_key="city", user_id="1001")
    await _remember(memories, content="住在杭州", memory_key="city", user_id="1002")

    short = await retriever.retrieve(_query("杭州"[:2], _target("1001")))
    assert [hit.fact.id for hit in short.hits] == [expected.id]
    safe = build_safe_lexical_query('杭州" OR * (秘密) NEAR', term_limit=4)
    assert "*" not in safe.fts_expression
    assert "(" not in safe.fts_expression
    assert len(safe.terms) <= 4


async def test_strict_time_filters_before_candidate_limits(database: Database) -> None:
    from qq_ai_bot.memory.embedding.codec import Float32VectorCodec
    from qq_ai_bot.memory.embedding.models import EmbeddingProviderProfile, EmbeddingVector
    from qq_ai_bot.memory.embedding.repository import MemoryEmbeddingRepository
    from qq_ai_bot.memory.embedding.semantic import MemorySemanticIndex
    from qq_ai_bot.memory.embedding.text import EmbeddingDocumentBuilder
    from qq_ai_bot.memory.tool_intent import parse_memory_tool_intent
    from qq_ai_bot.persistence.models import MemoryEmbeddingModel

    facts, retriever = _retriever(database)
    old = await _remember(facts, user_id="1001", memory_key="camera", content="camera old")
    inside = await _remember(
        facts, user_id="1001", memory_key="camera-inside", content="camera inside"
    )
    end = await _remember(facts, user_id="1001", memory_key="camera-end", content="camera end")
    unknown = await _remember(
        facts, user_id="1001", memory_key="camera-unknown", content="camera unknown"
    )
    async with database.sessions() as session, session.begin():
        for fact, occurred in (
            (old, "2026-08-01 00:00:00.000000"),
            (inside, "2026-09-08 16:00:00.000000"),
            (end, "2026-09-09 16:00:00.000000"),
        ):
            await session.execute(
                text("UPDATE memory_facts SET valid_from=:time WHERE id=:id"),
                {"time": occurred, "id": fact.id},
            )
        await session.execute(
            text("UPDATE memory_facts SET importance=5 WHERE id=:id"), {"id": old.id}
        )
    intent = parse_memory_tool_intent(
        {
            "query": "camera",
            "start_at": "2026-09-09T00:00:00+08:00",
            "end_at": "2026-09-10T00:00:00+08:00",
        }
    )
    query = _query("camera", _target("1001"), limit=1).model_copy(
        update={"intent": intent, "candidate_limit": 1, "always_on_explicit_preference_limit": 0}
    )
    result = await retriever.retrieve(query)
    assert [hit.fact.id for hit in result.hits] == [inside.id]
    overview = await retriever.retrieve(
        query.model_copy(update={"mode": MemoryRetrievalMode.OVERVIEW})
    )
    assert [hit.fact.id for hit in overview.hits] == [inside.id]
    short_query = await retriever.retrieve(
        query.model_copy(update={"text": "c", "normalized_text": "c"})
    )
    assert [hit.fact.id for hit in short_query.hits] == [inside.id]
    vectors = MemoryEmbeddingRepository(database)
    profile = await vectors.ensure_profile(
        EmbeddingProviderProfile(
            provider_id="synthetic",
            model_id="synthetic",
            dimensions=2,
            document_template_version=1,
            endpoint_identity="synthetic",
        )
    )
    documents = EmbeddingDocumentBuilder(template_version=1, max_characters=1000)
    vector = EmbeddingVector(values=(1.0, 0.0), dimensions=2)
    async with database.sessions() as session, session.begin():
        for fact in (old, inside, end, unknown):
            session.add(
                MemoryEmbeddingModel(
                    fact_id=fact.id,
                    profile_id=profile.id,
                    content_hash=documents.content_hash_fields(
                        kind=fact.kind.value,
                        category=fact.category,
                        memory_key=fact.memory_key,
                        content=fact.content,
                    ),
                    vector_blob=Float32VectorCodec().encode(vector),
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
    semantic = await MemorySemanticIndex(vectors, documents=documents).search(
        target=_target("1001"),
        query_vector=vector,
        profile=profile.profile,
        profile_id=profile.id,
        candidate_limit=1,
        kinds=(MemoryKind.FACT,),
        temporal=intent.temporal,
    )
    assert [item.fact_id for item in semantic] == [inside.id]
    assert not {old.id, end.id, unknown.id} & {hit.fact.id for hit in result.hits}
    assert parse_memory_tool_intent({}).mode.value == "overview"
    assert parse_memory_tool_intent({"query": "camera"}).mode.value == "hybrid"
    for invalid in (
        {"purpose": "invented"},
        {"preferred_kinds": ["invented"]},
        {"entities": "camera"},
        {"mode": "none"},
        {"start_at": "2026-09-09"},
        {"start_at": "invalid"},
        {"start_at": "2026-09-10T00:00:00Z", "end_at": "2026-09-09T00:00:00Z"},
    ):
        with pytest.raises(ValueError):
            parse_memory_tool_intent(invalid)


@pytest.mark.asyncio
async def test_missing_fts_surfaces_stable_error_instead_of_full_scan(database: Database) -> None:
    _, retriever = _retriever(database)
    async with database.sessions() as session, session.begin():
        await session.execute(text("DROP TABLE memory_facts_fts"))
    with pytest.raises(MemoryRetrievalError) as captured:
        await retriever.retrieve(_query("任意查询", _target("1001")))
    assert captured.value.code == "memory_index_unavailable"


@pytest.mark.asyncio
async def test_superseded_fact_is_physically_indexed_but_not_retrieved(
    database: Database,
) -> None:
    memories, retriever = _retriever(database)
    old = await _remember(memories, content="准备考研", memory_key="education:plan", user_id="1001")
    new = await memories.correct_fact(old.id, content="决定直接工作", actor_user_id="1001")
    old_result = await retriever.retrieve(_query("准备考研", _target("1001")))
    new_result = await retriever.retrieve(_query("直接工作", _target("1001")))
    assert old.id not in {hit.fact.id for hit in old_result.hits}
    assert [hit.fact.id for hit in new_result.hits] == [new.id]
