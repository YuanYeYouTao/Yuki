"""Owner-bound keyset pages and content capability isolation over original facts."""

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select
from tests.support.social_identity_cases import social_env
from tests.unit.test_control_plane_foundation import context

from qq_ai_bot.control_plane import ControlQueryError, ControlQueryService, PageRequest, ProblemCode
from qq_ai_bot.control_plane.query_types import MemoryQueryFilter
from qq_ai_bot.domain.identity import PersonId, SpaceId
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemoryFactModel,
    MemoryToolReceiptModel,
)


@pytest.fixture
async def memory_scene(database, tmp_path):
    env = await social_env(database, tmp_path)
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        source = await session.scalar(
            select(ChatEventModel.id).where(
                ChatEventModel.canonical_conversation_id == env.context.conversation_id,
                ChatEventModel.direction == "inbound",
            )
        )
        facts = []
        for index in range(36):
            scope = "person" if index < 33 else "group" if index == 33 else "self"
            row = MemoryFactModel(
                scope_type=scope,
                visibility_type="global" if index == 34 else "group" if index == 35 else None,
                canonical_subject_person_id=env.person if scope == "person" else None,
                canonical_subject_space_id=env.space if scope == "group" else None,
                canonical_visibility_space_id=env.space if index == 35 else None,
                kind="fact",
                memory_key=f"private-key-{index}",
                category="fixture",
                content=f"private-fact-{index}",
                normalized_content=f"private-fact-{index}",
                source_type="explicit",
                authority="self_report" if scope != "self" else "agent_reflection",
                status="active",
                created_at=now,
                updated_at=now,
                last_injected_at=None,
            )
            session.add(row)
            facts.append(row)
        await session.flush()
        for row in facts:
            session.add(
                MemoryEvidenceModel(
                    fact_id=row.id,
                    event_id=source,
                    source_speaker_user_id="10001",
                    relation="self_statement",
                    excerpt="private-evidence",
                    created_at=now,
                )
            )
        receipts = []
        for index in range(3):
            row = MemoryToolReceiptModel(
                conversation_key_hash="a" * 64,
                trigger_event_id=source,
                bot_user_id="80001",
                canonical_space_id=env.space,
                provider_id="fixture",
                tool_name="inspect",
                execution_id=f"original-execution-{index}",
                success=True,
                result_excerpt="private-tool",
                result_characters=12,
                created_at=now,
                expires_at=now + timedelta(days=1),
            )
            session.add(row)
            receipts.append(row)
        await session.flush()
        for row in receipts:
            session.add(
                MemoryEvidenceModel(
                    fact_id=facts[0].id,
                    tool_receipt_id=row.id,
                    source_speaker_user_id="80001",
                    relation="agent_reflection",
                    excerpt="private-tool-evidence",
                    created_at=now,
                )
            )
    return env, [row.id for row in facts], source, [row.id for row in receipts]


async def test_all_pages_owner_self_visibility_and_source(database, memory_scene):
    env, ids, source, receipts = memory_scene
    queries = ControlQueryService(ControlQueryAdapter(database))
    ctx = context("control.memory.metadata.read")
    owner = MemoryQueryFilter(person_id=PersonId.parse(env.person))
    first = await queries.list_memory_facts(ctx, PageRequest(limit=30), scope=owner)
    second = await queries.list_memory_facts(
        ctx, PageRequest(limit=30, cursor=first.next_cursor), scope=owner
    )
    assert [row.fact_id for row in (*first.items, *second.items)] == ids[:33]
    assert second.next_cursor is None
    assert all(row.content is None for row in first.items)
    group = await queries.list_memory_facts(
        ctx, PageRequest(), scope=MemoryQueryFilter(space_id=SpaceId.parse(env.space))
    )
    assert [row.fact_id for row in group.items] == [ids[33]]
    visible = await queries.list_memory_facts(
        ctx,
        PageRequest(),
        scope=MemoryQueryFilter(scope_type="self", visibility_space_id=SpaceId.parse(env.space)),
    )
    assert [row.fact_id for row in visible.items] == [ids[35]]
    evidence = await queries.list_memory_evidence(
        ctx, PageRequest(limit=2), scope=MemoryQueryFilter(fact_id=ids[0])
    )
    rest = await queries.list_memory_evidence(
        ctx,
        PageRequest(limit=2, cursor=evidence.next_cursor),
        scope=MemoryQueryFilter(fact_id=ids[0]),
    )
    assert len(evidence.items) == len(rest.items) == 2
    assert rest.next_cursor is None
    assert evidence.items[0].event_id == source
    assert rest.items[-1].execution_id == "original-execution-2"
    receipt = await queries.list_memory_facts(
        ctx, PageRequest(), scope=MemoryQueryFilter(tool_receipt_id=receipts[0])
    )
    assert [row.fact_id for row in receipt.items] == [ids[0]]
    missing = await queries.list_memory_evidence(
        ctx, PageRequest(), scope=MemoryQueryFilter(event_id=source + 999)
    )
    assert not missing.items
    metadata = await queries.read_memory_fact(ctx, ids[0])
    assert "content" not in metadata.fields and "memory_key" not in metadata.fields
    content = await queries.read_memory_fact(
        context("control.memory.metadata.read", "control.memory.content.read"), ids[0]
    )
    assert content.fields["content"] == "private-fact-0"
    assert content.fields["canonical_subject_person_id"] == env.person
    assert content.fields["last_injected_at"] is None


async def test_numbered_memory_pages_count_filtered_rows_and_sort_by_record_time(
    database, memory_scene
):
    env, ids, _source, _receipts = memory_scene
    async with database.immediate_session() as session:
        first = await session.get(MemoryFactModel, ids[0])
        assert first is not None
        first.updated_at = datetime.now(UTC) + timedelta(hours=1)

    queries = ControlQueryService(ControlQueryAdapter(database))
    ctx = context("control.memory.metadata.read")
    scope = MemoryQueryFilter(person_id=PersonId.parse(env.person))
    first_page = await queries.list_memory_facts(ctx, PageRequest(limit=10, number=1), scope=scope)
    last_page = await queries.list_memory_facts(ctx, PageRequest(limit=10, number=4), scope=scope)
    beyond_end = await queries.list_memory_facts(ctx, PageRequest(limit=10, number=5), scope=scope)
    assert first_page.total == last_page.total == beyond_end.total == 33
    assert first_page.number == 1 and last_page.number == 4
    assert first_page.items[0].fact_id == ids[0]
    assert len(last_page.items) == 3
    assert beyond_end.items == () and beyond_end.next_cursor is None
    assert first_page.next_cursor is None

    group = await queries.list_memory_facts(
        ctx,
        PageRequest(limit=10, number=1),
        scope=MemoryQueryFilter(space_id=SpaceId.parse(env.space)),
    )
    assert group.total == 1
    assert [item.fact_id for item in group.items] == [ids[33]]


async def test_cursors_bind_all_filters_kind_and_content(database, memory_scene):
    env, ids, _, _ = memory_scene
    queries = ControlQueryService(ControlQueryAdapter(database))
    ctx = context("control.memory.metadata.read")
    scope = MemoryQueryFilter(person_id=PersonId.parse(env.person))
    first = await queries.list_memory_facts(ctx, PageRequest(limit=2), scope=scope)
    for changed in (
        MemoryQueryFilter(),
        dataclasses.replace(scope, kind="preference"),
        dataclasses.replace(scope, status="contested"),
    ):
        with pytest.raises(ControlQueryError) as exc:
            await queries.list_memory_facts(
                ctx, PageRequest(cursor=first.next_cursor), scope=changed
            )
        assert exc.value.problem.code is ProblemCode.VALIDATION_ERROR
    with pytest.raises(ControlQueryError):
        await queries.list_memory_facts(
            context("control.memory.metadata.read", "control.memory.content.read"),
            PageRequest(cursor=first.next_cursor),
            scope=scope,
        )
    with pytest.raises(ControlQueryError):
        await queries.list_memory_evidence(ctx, PageRequest(cursor=first.next_cursor), scope=scope)
    first = await queries.list_memory_evidence(
        ctx, PageRequest(limit=2), scope=MemoryQueryFilter(fact_id=ids[0])
    )
    with pytest.raises(ControlQueryError):
        await queries.list_memory_evidence(
            ctx, PageRequest(cursor=first.next_cursor), scope=MemoryQueryFilter(fact_id=ids[1])
        )


async def test_metadata_sql_never_loads_content_and_deny_precedes_reader(database, memory_scene):
    _, ids, _, _ = memory_scene
    queries = ControlQueryService(ControlQueryAdapter(database))
    statements = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement.lower().split("\nfrom ")[0])

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        ctx = context("control.memory.metadata.read")
        await queries.list_memory_facts(ctx, PageRequest())
        await queries.list_memory_evidence(ctx, PageRequest())
        await queries.read_memory_fact(ctx, ids[0])
        assert statements
        sql = "\n".join(statements)
        assert "memory_facts.content" not in sql and "normalized_content" not in sql
        assert "memory_evidence.excerpt" not in sql and "result_excerpt" not in sql
        assert "source_speaker_user_id" not in sql and "memory_key" not in sql
        before = len(statements)
        for method, args in (
            (queries.list_memory_facts, (PageRequest(),)),
            (queries.list_memory_evidence, (PageRequest(),)),
            (queries.read_memory_fact, (ids[0],)),
        ):
            with pytest.raises(ControlQueryError) as exc:
                await method(context(), *args)
            assert exc.value.problem.code is ProblemCode.CAPABILITY_DENIED
        assert len(statements) == before
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"event_id": True},
        {"fact_id": 0},
        {"tool_receipt_id": 2**63},
        {"person_id": "10001"},
        {"scope_type": "qq"},
        {"status": "done"},
        {"event_id": 1, "tool_receipt_id": 1},
    ],
)
def test_filter_rejects_external_and_invalid_identifiers(kwargs):
    with pytest.raises((ValueError, TypeError)):
        MemoryQueryFilter(**kwargs)


@pytest.mark.parametrize("fact_id", [True, 0, -1, 2**63, "1"])
async def test_detail_rejects_invalid_ids(database, fact_id):
    with pytest.raises(ControlQueryError) as exc:
        await ControlQueryAdapter(database).read_memory_fact(fact_id, include_content=False)
    assert exc.value.problem.code is ProblemCode.VALIDATION_ERROR
