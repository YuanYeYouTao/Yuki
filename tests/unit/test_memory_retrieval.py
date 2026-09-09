"""Query-driven Memory V2 retrieval, targeting, and scale regressions."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import text
from tests.conftest import make_settings

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.memory.attribution import (
    MemoryAttributionJob,
    MemoryAttributionOutput,
    MemoryAttributionWorker,
    MemoryExposure,
    MemoryExposureSource,
)
from qq_ai_bot.memory.context import MemoryContextService
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryContextMode,
    MemoryKind,
    MemoryRetrievalMode,
    MemoryScopeType,
    MemorySourceType,
    MemoryTargetRole,
    SelfMemoryVisibility,
)
from qq_ai_bot.memory.errors import MemoryRetrievalError
from qq_ai_bot.memory.fts import SQLiteMemoryFTSIndex, build_safe_lexical_query
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.models import (
    MemoryEntityTarget,
    MemoryFact,
    MemoryFactCreate,
    MemoryLexicalCandidate,
    MemoryQuery,
    MemoryQueryIntent,
    MemoryRetrievalBlock,
    MemoryRetrievalHit,
    MemoryRetrievalResult,
)
from qq_ai_bot.memory.query import MemoryQueryBuilder
from qq_ai_bot.memory.ranking import MemoryRanker
from qq_ai_bot.memory.receipt import MemoryRecallRepository
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.memory.runtime.query_plane import apply_total_hit_limit
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.targets import MemoryTargetResolver
from qq_ai_bot.model_runtime.executor import ModelExecutor
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.people_repository import PeopleRepository


async def test_attribution_worker_evaluates_no_use_but_not_failed_requests(database: Database):
    runtime_config = RuntimeConfigService(settings=make_settings(database.url), database=database)
    runtime = await runtime_config.snapshot(user_id="1001", group_id=None)
    context = Mock(spec=MemoryContextService)
    context.mark_attributed_used = AsyncMock(return_value=())
    context.set_attribution_outcome = AsyncMock()
    worker = MemoryAttributionWorker(
        models=Mock(spec=ModelExecutor),
        memory_context=context,
        runtime_config=runtime_config,
        metrics=MemoryLifecycleMetrics(),
    )
    worker._structured.run = AsyncMock(return_value=MemoryAttributionOutput())
    job = MemoryAttributionJob(
        turn_id="synthetic",
        user_id="1001",
        group_id=None,
        user_question="synthetic",
        final_response="synthetic",
        intent=MemoryQueryIntent(),
        runtime=runtime,
        enqueued_at=datetime.now(UTC),
        exposures=(
            MemoryExposure(
                memory_ref="M1",
                fact_id=1,
                kind="fact",
                category="test",
                content="synthetic",
                target_role="current_person",
                source=MemoryExposureSource.AUTOMATIC,
            ),
        ),
    )
    await worker._process(job)
    context.mark_attributed_used.assert_awaited_once_with(
        "synthetic",
        (),
        evaluated_fact_ids=(1,),
    )
    context.mark_attributed_used.reset_mock()
    worker._structured.run = AsyncMock(side_effect=TimeoutError)
    await worker._process(job)
    context.mark_attributed_used.assert_not_awaited()
    context.set_attribution_outcome.assert_awaited_once_with("synthetic", "failed", "timeout")
    worker._structured.run = AsyncMock(return_value=MemoryAttributionOutput(used_refs=("M99",)))
    await worker._process(job)
    context.mark_attributed_used.assert_not_awaited()
    context.set_attribution_outcome.assert_awaited_with("synthetic", "failed", "invalid")


async def test_recall_receipt_tracks_zero_partial_evaluation_and_interruption(database: Database):
    import json

    from sqlalchemy.exc import SQLAlchemyError
    from tests.conftest import build_harness

    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.memory.receipt import MemoryRecallTurn
    from qq_ai_bot.memory.runtime.partition_lookup import DatabaseMemoryPartitionLookup
    from qq_ai_bot.memory.runtime.turn_session import TurnMemorySession
    from qq_ai_bot.runtime.authority import TurnAuthority
    from qq_ai_bot.runtime.origin import TurnOrigin
    from qq_ai_bot.services.agent_tools import ToolRuntime

    facts = MemoryFactService(MemoryFactRepository(database))
    first = await _remember(facts, user_id="1001", memory_key="first", content="synthetic first")
    second = await _remember(facts, user_id="1001", memory_key="second", content="synthetic second")
    receipts = MemoryRecallRepository(database)
    result = MemoryRetrievalResult(
        blocks=(),
        hits=(),
        candidate_count=0,
        selected_count=0,
        query_hash="",
        mode=MemoryRetrievalMode.RELEVANT,
    )
    context = MemoryContextService(
        query_builder=MemoryQueryBuilder(MemoryTargetResolver(PeopleRepository(database))),
        retriever=MemoryRetriever(
            repository=facts.repository, lexical_index=SQLiteMemoryFTSIndex(database)
        ),
        facts=facts,
        receipts=receipts,
    )
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url), database=database
    ).snapshot(user_id="1001")
    memory_session = TurnMemorySession.open(
        inbound=InboundMessage(
            message_id="synthetic",
            event_type="message:private:friend",
            scope_type=ScopeType.PRIVATE,
            sender=SenderIdentity(user_id="1001", nickname="test"),
            text="synthetic",
            bot_user_id="8000",
        ),
        identity=ConversationScope.private("8000", "1001"),
        runtime=runtime,
        memory_context=context,
        partition_lookup=DatabaseMemoryPartitionLookup(database),
        origin=TurnOrigin.USER_MESSAGE,
        user_question="synthetic",
        authority=TurnAuthority(
            actor_user_id="1001",
            bot_user_id="8000",
            origin=TurnOrigin.USER_MESSAGE,
            permission_ceiling=frozenset(),
            delegated_authority=None,
            authority_revision=1,
        ),
    )
    # No prefetch or prompt exposure: execution creates only a zero-exposure receipt.
    await memory_session.record_read_outcome("success")
    async with database.sessions() as session:
        turn = MemoryRecallTurn(
            (
                await session.execute(text("SELECT turn_id FROM memory_recall_receipts"))
            ).scalar_one(),
            (),
        )
    async with database.sessions() as session:
        assert (
            await session.execute(
                text(
                    "SELECT injected_count, attribution_status, attribution_reason "
                    "FROM memory_recall_receipts"
                )
            )
        ).one() == (0, "skipped", "no_memory")
    await receipts.record_tool_injected(turn.turn_id, (first.id, second.id))
    await memory_session.record_read_outcome("duplicate")
    await receipts.set_attribution_outcome(turn.turn_id, "pending", "queued")
    await receipts.mark_attributed_used(turn.turn_id, (), evaluated_fact_ids=(first.id,))
    await receipts.set_attribution_outcome(turn.turn_id, "failed", "interrupted")
    async with database.sessions() as session:
        assert (
            await session.execute(
                text(
                    "SELECT attribution_status, attribution_reason, injected_count, used_count "
                    "FROM memory_recall_receipts"
                )
            )
        ).one() == ("succeeded", "no_used", 2, 0)
        assert (
            await session.execute(
                text(
                    "SELECT fact_id, attribution_evaluated, used "
                    "FROM memory_recall_items ORDER BY fact_id"
                )
            )
        ).all() == [(first.id, 1, 0), (second.id, 0, 0)]
    pending = await receipts.record_initial(
        conversation_key="synthetic",
        trigger_message_id="next",
        origin="user_message",
        intent=MemoryQueryIntent(),
        result=result,
        injected_fact_ids=(),
        retention_days=30,
    )
    await receipts.set_attribution_outcome(pending.turn_id, "pending", "queued")
    await receipts.recover_pending_attribution()
    async with database.sessions() as session:
        assert (
            await session.execute(
                text(
                    "SELECT attribution_status, attribution_reason FROM memory_recall_receipts "
                    "WHERE turn_id=:turn"
                ),
                {"turn": pending.turn_id},
            )
        ).one() == ("failed", "interrupted")
    with pytest.raises(ValueError, match="invalid attribution"):
        await receipts.set_attribution_outcome(turn.turn_id, "failed", "secret exception payload")
    report = await receipts.summarize(since=datetime(2020, 1, 1, tzinfo=UTC))
    assert report["evaluated"] == 1
    assert report["evaluated_use_rate"] == 0
    assert report["evaluation_coverage"] == 0.5
    assert report["tool_reads"] == {
        "success": 1,
        "empty": 0,
        "ambiguous": 0,
        "permission_denied": 0,
        "duplicate": 1,
        "infrastructure_failure": 0,
    }
    # Exercise the actual tool boundary, including failed observability. A read
    # failure must remain an infrastructure error, not turn into invalid arguments.
    tools = build_harness(database, make_settings(database.url)).processor._chat._tools
    tools._memory_context = context
    tool_runtime = ToolRuntime(
        inbound=memory_session._inbound,
        gateway=None,
        allow_generic_onebot=False,
        runtime_config=runtime,
        memory_session=memory_session,
    )
    with patch.object(tools, "_person_memories", AsyncMock(side_effect=SQLAlchemyError("secret"))):
        failure = json.loads(await tools.execute("get_person_memories", "{}", tool_runtime))
    assert failure["error"] == "database_failure"
    assert failure["retryable"] is True
    assert context.metrics.count("memory_read_infrastructure_failure") == 1
    assert (await receipts.summarize(since=datetime(2020, 1, 1, tzinfo=UTC)))["tool_reads"][
        "infrastructure_failure"
    ] == 1
    with (
        patch.object(tools, "_person_memories", AsyncMock(return_value='{"ok":true,"data":{}}')),
        patch.object(
            context, "record_tool_read_outcome", AsyncMock(side_effect=RuntimeError("secret"))
        ),
    ):
        success = json.loads(await tools.execute("get_person_memories", "{}", tool_runtime))
    assert success["ok"] is True and success["data"] == {}
    assert success["evidence_state"] == {
        "source": "memory_tool",
        "query_status": "empty",
        "returned_count": 0,
        "truncated": False,
        "partial_failure": False,
        "source_refs": [],
        "delivery": "staged",
    }


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
        always_on_explicit_preference_limit=2,
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


@pytest.mark.asyncio
async def test_query_builder_adds_self_target_only_for_enabled_explicit_recall(
    database: Database,
) -> None:
    settings = make_settings(database.url, self_memory_enabled=True)
    runtime = await RuntimeConfigService(settings=settings, database=database).snapshot(
        user_id="1001"
    )
    builder = MemoryQueryBuilder(MemoryTargetResolver(PeopleRepository(database)))
    inbound = InboundMessage(
        message_id="self-recall-target",
        event_type="message:private",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text="你喜欢咖啡吗",
        bot_user_id="8000",
    )

    disabled_for_turn = await builder.build(
        inbound=inbound,
        content=inbound.text,
        runtime=runtime,
        self_recall=False,
    )
    enabled_for_turn = await builder.build(
        inbound=inbound,
        content=inbound.text,
        runtime=runtime,
        self_recall=True,
    )
    assert MemoryTargetRole.CURRENT_SELF not in {
        target.role for target in disabled_for_turn.targets
    }
    self_target = next(
        target
        for target in enabled_for_turn.targets
        if target.role is MemoryTargetRole.CURRENT_SELF
    )
    assert self_target.visibility_type is SelfMemoryVisibility.PRIVATE
    assert self_target.visibility_user_id == "1001"


@pytest.mark.asyncio
async def test_self_episode_has_no_automatic_bypass_but_active_query_still_works(
    database: Database,
) -> None:
    people = PeopleRepository(database)
    await people.observe(user_id="1001", nickname="远野", group_id="2001")
    memories, retriever = _retriever(database)
    first = await _remember(
        memories,
        content="我记得大家第一次一起讨论海边散步时，最后把计划说得很认真",
        memory_key="self_episode:first_walk",
        user_id=None,
        scope=MemoryScopeType.SELF,
        kind=MemoryKind.EPISODE,
        category="self_episode",
        visibility_type=SelfMemoryVisibility.GROUP,
        visibility_group_id="2001",
        authority=MemoryAuthority.AGENT_REFLECTION,
    )
    second = await _remember(
        memories,
        content="后来又聊到海边散步，我发现自己其实很期待那次见面",
        memory_key="self_episode:second_walk",
        user_id=None,
        scope=MemoryScopeType.SELF,
        kind=MemoryKind.EPISODE,
        category="self_episode",
        visibility_type=SelfMemoryVisibility.GROUP,
        visibility_group_id="2001",
        authority=MemoryAuthority.AGENT_REFLECTION,
    )
    other_group = await _remember(
        memories,
        content="另一个群也提到海边散步",
        memory_key="self_episode:other_group",
        user_id=None,
        scope=MemoryScopeType.SELF,
        kind=MemoryKind.EPISODE,
        category="self_episode",
        visibility_type=SelfMemoryVisibility.GROUP,
        visibility_group_id="2002",
        authority=MemoryAuthority.AGENT_REFLECTION,
    )
    context = MemoryContextService(
        query_builder=MemoryQueryBuilder(MemoryTargetResolver(people)),
        retriever=retriever,
        facts=memories,
    )
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url, self_memory_enabled=True),
        database=database,
    ).snapshot(user_id="1001", group_id="2001")
    inbound = InboundMessage(
        message_id="natural-self-episode",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", nickname="远野"),
        text="你还记得我们聊海边散步吗",
        group_id="2001",
        bot_user_id="8000",
    )

    automatic = await context.retrieve_for_turn(
        inbound=inbound,
        content=inbound.text,
        runtime=runtime,
        self_recall=False,
    )
    auto_self = [hit for hit in automatic.hits if hit.target.role is MemoryTargetRole.CURRENT_SELF]
    assert auto_self == []  # No calibrated profile, and no exact match.

    explicit = await context.search(
        text=inbound.text,
        mode=MemoryRetrievalMode.RELEVANT,
        targets=await context.resolve_targets(inbound, runtime, self_recall=True),
        runtime=runtime,
    )
    explicit_self_ids = {
        hit.fact.id for hit in explicit.hits if hit.target.role is MemoryTargetRole.CURRENT_SELF
    }
    assert {first.id, second.id} <= explicit_self_ids
    assert other_group.id not in explicit_self_ids


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


@pytest.mark.asyncio
async def test_no_match_only_keeps_bounded_explicit_person_preferences(
    database: Database,
) -> None:
    memories, retriever = _retriever(database)
    await _remember(
        memories,
        content="完全无关的高重要事实",
        memory_key="unrelated",
        importance=5,
    )
    preferred = await _remember(
        memories,
        content="回答要简短",
        memory_key="reply:length",
        kind=MemoryKind.PREFERENCE,
        source=MemorySourceType.EXPLICIT,
        importance=5,
    )
    await _remember(
        memories,
        content="群偏好不应常驻",
        memory_key="group:preference",
        user_id=None,
        group_id="2001",
        scope=MemoryScopeType.GROUP,
        kind=MemoryKind.PREFERENCE,
        source=MemorySourceType.EXPLICIT,
    )

    result = await retriever.retrieve(
        _query("量子火箭发动机", _target("1001"), _group_target("2001"))
    )
    assert [hit.fact.id for hit in result.hits] == [preferred.id]
    assert result.hits[0].selection_reason == "always_on_explicit_preference"

    matched_preference = await retriever.retrieve(_query("回答要简短", _target("1001")))
    assert [hit.fact.id for hit in matched_preference.hits] == [preferred.id]


@pytest.mark.asyncio
async def test_overview_and_each_target_have_independent_limits(database: Database) -> None:
    memories, retriever = _retriever(database)
    for user_id in ("1001", "1002"):
        for index in range(3):
            await _remember(
                memories,
                content=f"{user_id} 的事实 {index}",
                memory_key=f"fact:{index}",
                user_id=user_id,
                importance=5 - index,
            )
    result = await retriever.retrieve(
        _query(
            "你记得什么",
            _target("1001"),
            _target("1002", role=MemoryTargetRole.REFERENCED_PERSON),
            mode=MemoryRetrievalMode.OVERVIEW,
            limit=2,
        )
    )
    assert [len(block.hits) for block in result.blocks] == [2, 2]
    assert all(
        hit.fact.subject_user_id == block.target.subject_user_id
        for block in result.blocks
        for hit in block.hits
    )


@pytest.mark.asyncio
async def test_target_resolver_uses_only_real_current_event_references(
    database: Database,
) -> None:
    people = PeopleRepository(database)
    await people.observe(user_id="1001", nickname="当前", group_id="2001")
    await people.observe(user_id="1002", nickname="被提及", group_id="2001")
    await people.observe(user_id="1003", nickname="被回复", group_id="2001")
    await people.observe(user_id="1004", nickname="其他群", group_id="2002")
    inbound = InboundMessage(
        message_id="targets-1",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", nickname="当前"),
        text="问他们",
        group_id="2001",
        bot_user_id="8000",
        mentioned_user_ids=("1001", "8000", "1002", "1004", "1002"),
        reply_sender_user_id="1003",
    )

    targets = await MemoryTargetResolver(people).resolve(inbound, max_referenced=5)
    referenced = [
        target.subject_user_id
        for target in targets
        if target.role is MemoryTargetRole.REFERENCED_PERSON_GROUP
    ]
    assert referenced == ["1002", "1003"]
    assert all(target.group_id == "2001" for target in targets if target.group_id)


@pytest.mark.asyncio
async def test_canonical_target_resolver_skips_presence_and_external(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    people = PeopleRepository(database)
    seen: list[str] = []
    real_members = PeopleRepository.members_in_group

    async def members(
        self: PeopleRepository,
        user_ids: tuple[str, ...],
        group_id: str,
    ) -> frozenset[str]:
        seen.extend(user_ids)
        return await real_members(self, user_ids, group_id)

    monkeypatch.setattr(PeopleRepository, "members_in_group", members)
    await people.observe(user_id="1001", nickname="当前", group_id="2001")
    await people.observe(user_id="1002", nickname="乙", group_id="2001")
    await people.observe(user_id="1003", nickname="被回复", group_id="2001")
    inbound = InboundMessage(
        message_id="v2-targets",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", nickname="当前"),
        text="问他们",
        group_id="2001",
        bot_user_id="8000",
        mentioned_user_ids=("7777", "6666", "1002"),
        reply_sender_user_id="1003",
    )
    targets = await MemoryTargetResolver(people).resolve(inbound, max_referenced=5)
    referenced = [
        target.subject_user_id
        for target in targets
        if target.role is MemoryTargetRole.REFERENCED_PERSON_GROUP
    ]
    assert referenced == ["1002", "1003"]
    assert "7777" not in seen
    assert "6666" not in seen
    assert "8000" not in seen

    private = replace(inbound, scope_type=ScopeType.PRIVATE, group_id=None)
    private_targets = await MemoryTargetResolver(people).resolve(private, max_referenced=5)
    assert [target.role for target in private_targets] == [MemoryTargetRole.CURRENT_PERSON]


@pytest.mark.asyncio
async def test_personal_overview_drops_referenced_people(database: Database) -> None:
    people = PeopleRepository(database)
    await people.observe(user_id="1001", nickname="当前", group_id="2001")
    await people.observe(user_id="1002", nickname="被提及", group_id="2001")
    inbound = InboundMessage(
        message_id="overview-targets",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", nickname="当前"),
        text="你记得我什么",
        group_id="2001",
        bot_user_id="8000",
        mentioned_user_ids=("1002",),
    )
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot(user_id="1001", group_id="2001")
    query = await MemoryQueryBuilder(MemoryTargetResolver(people)).build(
        inbound=inbound,
        content=inbound.text,
        runtime=runtime,
        memory_intent=MemoryQueryIntent(mode=MemoryContextMode.OVERVIEW),
    )

    assert query.mode is MemoryRetrievalMode.OVERVIEW
    assert all(
        target.role
        not in {
            MemoryTargetRole.REFERENCED_PERSON,
            MemoryTargetRole.REFERENCED_PERSON_GROUP,
        }
        for target in query.targets
    )


@pytest.mark.asyncio
async def test_planner_memory_modes_control_semantic_retrieval(database: Database) -> None:
    people = PeopleRepository(database)
    await people.observe(user_id="1001", nickname="当前用户")
    inbound = InboundMessage(
        message_id="planner-memory-mode",
        event_type="message:private:friend",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001", nickname="当前用户"),
        text="之前聊过的音乐",
        bot_user_id="8000",
    )
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot(user_id="1001")
    builder = MemoryQueryBuilder(MemoryTargetResolver(people))

    lexical = await builder.build(
        inbound=inbound,
        content=inbound.text,
        runtime=runtime,
        memory_mode=MemoryContextMode.LEXICAL,
    )
    hybrid = await builder.build(
        inbound=inbound,
        content=inbound.text,
        runtime=runtime,
        memory_mode=MemoryContextMode.HYBRID,
    )
    overview = await builder.build(
        inbound=inbound,
        content=inbound.text,
        runtime=runtime,
        memory_mode=MemoryContextMode.OVERVIEW,
    )

    assert lexical.mode is MemoryRetrievalMode.RELEVANT
    assert lexical.semantic_enabled is False
    assert hybrid.mode is MemoryRetrievalMode.RELEVANT
    assert hybrid.semantic_enabled is runtime.memory.semantic_enabled
    assert overview.mode is MemoryRetrievalMode.OVERVIEW
    assert overview.semantic_enabled is False
    await _assert_none_memory_mode_returns_empty_result(database)


async def _assert_none_memory_mode_returns_empty_result(database: Database) -> None:
    people = PeopleRepository(database)
    repository = MemoryFactRepository(database)
    context = MemoryContextService(
        query_builder=MemoryQueryBuilder(MemoryTargetResolver(people)),
        retriever=MemoryRetriever(
            repository=repository,
            lexical_index=SQLiteMemoryFTSIndex(database),
        ),
        facts=MemoryFactService(repository),
    )
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot(user_id="1001")
    inbound = InboundMessage(
        message_id="planner-memory-none",
        event_type="message:private:friend",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001", nickname=""),
        text="嗯",
        bot_user_id="8000",
    )

    result = await context.retrieve_for_turn(
        inbound=inbound,
        content=inbound.text,
        runtime=runtime,
        memory_mode=MemoryContextMode.NONE,
    )

    assert result.hits == ()
    assert result.blocks == ()
    assert result.semantic_status == "skipped"


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
        min_similarity=0.0,
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
async def test_disabled_retrieval_cannot_inject_unrelated_current_background(
    database: Database,
) -> None:
    people = PeopleRepository(database)
    await people.observe(user_id="1001", nickname="当前", group_id="2001")
    await people.observe(user_id="1002", nickname="被提及", group_id="2001")
    repository = MemoryFactRepository(database)
    memories = MemoryFactService(repository)
    current = await _remember(
        memories, content="当前人物事实", memory_key="current", user_id="1001"
    )
    await _remember(memories, content="其他人物事实", memory_key="other", user_id="1002")
    context = MemoryContextService(
        query_builder=MemoryQueryBuilder(MemoryTargetResolver(people)),
        retriever=MemoryRetriever(
            repository=repository,
            lexical_index=SQLiteMemoryFTSIndex(database),
        ),
        facts=memories,
    )
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot(user_id="1001", group_id="2001")
    runtime = replace(runtime, memory=replace(runtime.memory, retrieval_enabled=False))
    inbound = InboundMessage(
        message_id="disabled-retrieval",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", nickname="当前"),
        text="无关查询",
        group_id="2001",
        bot_user_id="8000",
        mentioned_user_ids=("1002",),
    )
    result = await context.retrieve_for_turn(
        inbound=inbound,
        content=inbound.text,
        runtime=runtime,
    )

    assert result.hits == ()
    assert current.id in {hit.fact.id for hit in result.trace_hits}
    assert all(
        block.target.role
        not in {
            MemoryTargetRole.REFERENCED_PERSON,
            MemoryTargetRole.REFERENCED_PERSON_GROUP,
        }
        for block in result.blocks
    )
    assert all(hit.selection_reason.startswith("rejected_") for hit in result.trace_hits)


@pytest.mark.asyncio
async def test_missing_fts_surfaces_stable_error_instead_of_full_scan(database: Database) -> None:
    _, retriever = _retriever(database)
    async with database.sessions() as session, session.begin():
        await session.execute(text("DROP TABLE memory_facts_fts"))
    with pytest.raises(MemoryRetrievalError) as captured:
        await retriever.retrieve(_query("任意查询", _target("1001")))
    assert captured.value.code == "memory_index_unavailable"


def test_ranker_uses_exact_fields_then_stable_fact_id() -> None:
    target = _target("1001")
    now = datetime(2026, 8, 1, tzinfo=UTC)

    def fact(
        fact_id: int,
        *,
        key: str,
        category: str,
        content: str,
        importance: int = 3,
        confidence: float = 0.8,
    ) -> MemoryFact:
        return MemoryFact(
            id=fact_id,
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind=MemoryKind.FACT,
            memory_key=key,
            category=category,
            content=content,
            normalized_content=content.casefold(),
            importance=importance,
            confidence=confidence,
            source_type=MemorySourceType.AUTOMATIC,
            status="active",
            created_at=now,
            updated_at=now,
        )

    facts = (
        fact(4, key="other", category="other", content="数学竞赛相关"),
        fact(3, key="other", category="数学竞赛", content="相关内容"),
        fact(2, key="other", category="other", content="数学竞赛"),
        fact(1, key="数学竞赛", category="other", content="相关内容"),
    )
    candidates = tuple(
        MemoryLexicalCandidate(fact_id=item.id, target=target, fts_rank=1.0) for item in facts
    )
    ranked = MemoryRanker().rank_lexical(
        facts=facts,
        candidates=candidates,
        target=target,
        normalized_query="数学竞赛",
        limit=10,
    )
    assert [hit.fact.id for hit in ranked] == [1, 2, 3, 4]
    assert [hit.selection_reason for hit in ranked[:3]] == [
        "memory_key_exact",
        "content_exact",
        "category_exact",
    ]

    tied = (
        fact(9, key="other", category="other", content="匹配词条", importance=4, confidence=0.7),
        fact(8, key="other", category="other", content="匹配词条", importance=5),
        fact(7, key="other", category="other", content="匹配词条", importance=4, confidence=0.9),
        fact(6, key="other", category="other", content="匹配词条", importance=4, confidence=0.9),
    )
    tied_candidates = tuple(
        MemoryLexicalCandidate(fact_id=item.id, target=target, fts_rank=1.0) for item in tied
    )
    stable = MemoryRanker().rank_lexical(
        facts=tied,
        candidates=tied_candidates,
        target=target,
        normalized_query="别的查询",
        limit=10,
    )
    assert [hit.fact.id for hit in stable] == [8, 6, 7, 9]


@pytest.mark.asyncio
async def test_global_topics_precede_background_and_preserve_total_order(
    database: Database,
) -> None:
    memories, _ = _retriever(database)
    current, other = _target("1001"), _target("1002", role=MemoryTargetRole.REFERENCED_PERSON)
    specs = ((current, 0.68), (other, 0.86), (other, 0.84), (other, 0.82), (other, 0.80))
    hits = []
    for index, (target, score) in enumerate(specs):
        fact = await _remember(
            memories,
            content=f"独立测试事实 {index}",
            memory_key=f"test:{index}",
            user_id=target.subject_user_id,
        )
        hits.append(
            MemoryRetrievalHit(
                fact=fact,
                target=target,
                rank=1,
                lexical_score=1,
                semantic_score=score,
                selection_reason="hybrid_match",
            )
        )
    query = _query("主题", current, other)
    ranked = MemoryRanker.rank_global(tuple(hits), query)
    assert [hit.fact.id for hit in ranked] == [hit.fact.id for hit in hits[1:]] + [hits[0].fact.id]
    assert [hit.semantic_rank for hit in ranked] == [1, 2, 3, 4, 5]
    profile = "a" * 64
    result = MemoryRetrievalResult(
        hits=ranked,
        blocks=tuple(
            MemoryRetrievalBlock(
                target=target, hits=tuple(hit for hit in ranked if hit.target == target)
            )
            for target in (current, other)
        ),
        trace_hits=ranked,
        candidate_count=5,
        selected_count=5,
        query_hash="synthetic",
        mode=query.mode,
        embedding_profile=profile,
        semantic_status="ready",
    )
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url), database=database
    ).snapshot()
    runtime = replace(
        runtime,
        memory=replace(
            runtime.memory,
            automatic_calibrated_profile=profile,
            automatic_topic_threshold=0.75,
            automatic_background_threshold=0.60,
        ),
    )
    topics = MemoryContextService._limit_automatic_result(result, None, runtime)
    assert [hit.fact.id for hit in topics.hits] == [hit.fact.id for hit in ranked[:4]]
    assert all(hit.target == other and hit.selection_reason == "topic" for hit in topics.hits)
    runtime = replace(runtime, memory=replace(runtime.memory, automatic_topic_threshold=0.81))
    mixed = MemoryContextService._limit_automatic_result(result, None, runtime)
    assert [hit.selection_reason for hit in mixed.hits] == ["topic", "topic", "topic", "background"]
    assert mixed.hits[-1].fact.id == hits[0].fact.id
    capped = apply_total_hit_limit(mixed, 2)
    assert capped.hits == mixed.hits[:2]
    no_topics = result.model_copy(update={"hits": (hits[0],)})
    assert MemoryContextService._limit_automatic_result(no_topics, None, runtime).hits == ()
    uncalibrated = result.model_copy(update={"embedding_profile": "unknown"})
    assert MemoryContextService._limit_automatic_result(uncalibrated, None, runtime).hits == ()
    from qq_ai_bot.memory.match_projection import match_projection

    assert match_projection(hits[0], result, runtime) == {
        "lexical_match": True,
        "semantic_candidate": True,
        "topic_admission": "not_passed",
    }
    assert match_projection(hits[1], result, runtime)["topic_admission"] == "passed"
    assert match_projection(hits[1], uncalibrated, runtime)["topic_admission"] == "unknown"
    overview = result.model_copy(update={"mode": MemoryRetrievalMode.OVERVIEW})
    assert match_projection(hits[1], overview, runtime)["topic_admission"] == "unknown"


@pytest.mark.asyncio
async def test_superseded_fact_is_physically_indexed_but_not_retrieved(
    database: Database,
) -> None:
    memories, retriever = _retriever(database)
    old = await _remember(memories, content="准备考研", memory_key="education:plan", user_id="1001")
    new = await _remember(
        memories, content="决定直接工作", memory_key="education:plan", user_id="1001"
    )
    old_result = await retriever.retrieve(_query("准备考研", _target("1001")))
    new_result = await retriever.retrieve(_query("直接工作", _target("1001")))
    assert old.id not in {hit.fact.id for hit in old_result.hits}
    assert [hit.fact.id for hit in new_result.hits] == [new.id]
