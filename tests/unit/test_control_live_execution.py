"""The live panel and event links are observations of real Runner evidence."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, update
from tests.support.social_identity_cases import social_env
from tests.unit.test_control_plane_foundation import context

from qq_ai_bot.control_plane import ControlQueryError, ControlQueryService, PageRequest
from qq_ai_bot.control_plane.query_types import ExecutionTraceFilter
from qq_ai_bot.domain.identity import ConversationId
from qq_ai_bot.execution_trace.db_models import ExecutionTraceEntryModel as Trace
from qq_ai_bot.execution_trace.recorder import TraceRecorder, trace_span
from qq_ai_bot.model_runtime.db_models import ModelInvocationModel
from qq_ai_bot.persistence.control_live_execution import (
    list_event_turns,
    read_conversation_execution,
)
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.persistence.models import ChatEventModel


@pytest.mark.asyncio
async def test_live_execution_requires_real_process_span_and_expiring_evidence(database, tmp_path):
    env = await social_env(database, tmp_path)
    conversation = ConversationId.parse(env.context.conversation_id)
    recorder = TraceRecorder(database)

    async with trace_span(
        "semantic_participation", {}, recorder=recorder, conversation_id=conversation.text
    ):
        observing = await read_conversation_execution(database.sessions, conversation, recorder)
        assert observing.fields["state"] == "idle"

    async with trace_span(
        "chat_processing", {}, recorder=recorder, conversation_id=conversation.text
    ):
        active = await read_conversation_execution(database.sessions, conversation, recorder)
        assert active.fields["state"] == "active"
        assert len(active.fields["active"]) == 1
        assert active.fields["active"][0]["evidence"] == "live_runner_span"
        assert active.fields["active"][0]["steps"][0]["kind"] == "chat_processing_start"

    completed = await read_conversation_execution(database.sessions, conversation, recorder)
    assert completed.fields["state"] == "idle"
    assert completed.fields["recent"][0]["status"] == "completed"
    assert completed.fields["coverage_note"] is None
    statements: list[tuple[str, object]] = []

    def capture(_connection, _cursor, statement, parameters, *_args):
        statements.append((statement, parameters))

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        await read_conversation_execution(database.sessions, conversation, recorder)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    trace_queries = [sql for sql, _ in statements if "execution_trace_entries" in sql]
    assert trace_queries and all("LIMIT" in sql.upper() for sql in trace_queries)
    assert all("payload_gzip" not in sql for sql in trace_queries)
    root_sql, root_params = next(
        (sql, params) for sql, params in statements if "INDEXED BY ix_execution_trace_roots" in sql
    )
    step_sql, step_params = next(
        (sql, params)
        for sql, params in statements
        if "INDEXED BY ix_execution_trace_turn_id" in sql and "LIMIT 33" in sql
    )
    async with database.engine.connect() as connection:
        root_plan = (
            await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + root_sql, root_params)
        ).all()
        plan = (
            await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + step_sql, step_params)
        ).all()
    assert any("USING INDEX ix_execution_trace_roots" in row[3] for row in root_plan)
    assert any("USING INDEX ix_execution_trace_turn_id" in row[3] for row in plan)

    # A start without its terminal receipt must not keep a turn "running"
    # after the actual process span has exited.
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(Trace)
            .where(Trace.conversation_id == conversation.text, Trace.kind == "chat_processing_end")
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    incomplete = await read_conversation_execution(database.sessions, conversation, recorder)
    assert incomplete.fields["state"] == "evidence_insufficient"
    assert incomplete.fields["active"] == ()
    assert incomplete.fields["coverage_note"] is not None

    async with database.sessions() as session, session.begin():
        await session.execute(
            update(Trace)
            .where(Trace.conversation_id == conversation.text)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    expired = await read_conversation_execution(database.sessions, conversation, recorder)
    assert expired.fields["state"] == "idle" and expired.fields["recent"] == ()


@pytest.mark.asyncio
async def test_live_execution_keeps_two_recent_turns_with_bounded_root_scan(database, tmp_path):
    env = await social_env(database, tmp_path)
    conversation = ConversationId.parse(env.context.conversation_id)
    recorder = TraceRecorder(database)
    for _ in range(3):
        async with trace_span(
            "chat_processing", {}, recorder=recorder, conversation_id=conversation.text
        ):
            async with trace_span("turn", {}):
                pass
    view = await read_conversation_execution(database.sessions, conversation, recorder)
    assert view.fields["state"] == "idle"
    recent = view.fields["recent"]
    assert len(recent) == 2
    assert len({item["turn_id"] for item in recent}) == 2


@pytest.mark.asyncio
async def test_live_turn_links_received_message_and_requires_content_grant(database, tmp_path):
    env = await social_env(database, tmp_path)
    conversation = ConversationId.parse(env.context.conversation_id)
    async with database.sessions() as session:
        inbound = await session.scalar(
            select(ChatEventModel).where(
                ChatEventModel.canonical_conversation_id == conversation.text,
                ChatEventModel.direction == "inbound",
            )
        )
        assert inbound is not None
        event_id = inbound.id
        saved_content = inbound.content
    recorder = TraceRecorder(database)
    async with trace_span(
        "chat_processing",
        {},
        recorder=recorder,
        conversation_id=conversation.text,
        source_event_id=event_id,
    ):
        pass
    turn_id = (await read_conversation_execution(database.sessions, conversation, recorder)).fields[
        "recent"
    ][0]["turn_id"]
    async with database.sessions() as session, session.begin():
        session.add(
            ModelInvocationModel(
                task="chat_agent",
                profile_id="main",
                provider="fixture",
                model="offline",
                success=True,
                latency_seconds=1,
                created_at=datetime.now(UTC),
                runtime_turn_id=turn_id,
                prompt_tokens=100,
                cached_prompt_tokens=60,
                completion_tokens=20,
                total_tokens=120,
            )
        )
    queries = ControlQueryService(ControlQueryAdapter(database, trace_recorder=recorder))
    metadata = (
        await queries.read_conversation_execution(
            context("control.execution.metadata.read"),
            conversation,
        )
    ).fields["recent"][0]["messages"]
    assert metadata[0]["event_id"] == event_id
    assert metadata[0]["direction"] == "received"
    assert metadata[0]["content"] is None
    usage = (
        await queries.read_conversation_execution(
            context("control.execution.metadata.read"), conversation
        )
    ).fields["recent"][0]["usage"]
    assert usage == {
        "calls": 1,
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "cached_input_tokens": 60,
        "missing_usage_calls": 0,
    }
    contents = (
        await queries.read_conversation_execution(
            context("control.execution.metadata.read", "control.chat.content.read"),
            conversation,
            include_content=True,
        )
    ).fields["recent"][0]["messages"]
    assert contents[0]["content"] == saved_content


@pytest.mark.asyncio
async def test_inbound_event_links_only_real_runner_turns_and_can_have_multiple(database, tmp_path):
    env = await social_env(database, tmp_path)
    conversation = ConversationId.parse(env.context.conversation_id)
    async with database.sessions() as session:
        chat_event = await session.scalar(
            select(ChatEventModel).where(
                ChatEventModel.canonical_conversation_id == conversation.text
            )
        )
        assert chat_event is not None
        event_id = chat_event.id
    recorder = TraceRecorder(database)

    no_turn = await list_event_turns(
        database.sessions,
        PageRequest(),
        conversation_id=conversation,
        event_id=event_id,
        direction="inbound",
    )
    assert no_turn.items == ()

    async with trace_span(
        "semantic_participation",
        {},
        recorder=recorder,
        conversation_id=conversation.text,
        source_event_id=event_id,
    ):
        pass
    jev_only = await list_event_turns(
        database.sessions,
        PageRequest(),
        conversation_id=conversation,
        event_id=event_id,
        direction="inbound",
    )
    assert jev_only.items == ()

    for _ in range(2):
        async with trace_span(
            "chat_processing",
            {},
            recorder=recorder,
            conversation_id=conversation.text,
            source_event_id=event_id,
        ):
            async with trace_span("turn", {}):
                pass
    statements: list[tuple[str, object]] = []

    def capture(_connection, _cursor, statement, parameters, *_args):
        statements.append((statement, parameters))

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        linked = await list_event_turns(
            database.sessions,
            PageRequest(),
            conversation_id=conversation,
            event_id=event_id,
            direction="inbound",
        )
        turns = await ControlQueryService(ControlQueryAdapter(database)).list_execution_trace(
            context("control.execution.metadata.read"),
            PageRequest(limit=20, number=1),
            scope=ExecutionTraceFilter(
                conversation_id=conversation,
                turn_id=linked.items[0].fields["turn_id"],
                descending=False,
            ),
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert linked.total == 2
    assert len({item.fields["turn_id"] for item in linked.items}) == 2
    assert all(item.fields["trace_status"] == "completed" for item in linked.items)
    assert turns.total == 4
    assert [item.id for item in turns.items] == sorted(item.id for item in turns.items)
    assert all(
        item.turn_id == linked.items[0].fields["turn_id"] and item.conversation_id == conversation
        for item in turns.items
    )
    indexed_sql = [
        (sql, params)
        for sql, params in statements
        if "INDEXED BY ix_execution_trace_turn_id" in sql
    ]
    assert len(indexed_sql) >= 3
    assert any("count(" in sql.lower() for sql, _ in indexed_sql)
    assert any("INDEXED BY ix_execution_trace_source_event" in sql for sql, _ in indexed_sql)
    async with database.engine.connect() as connection:
        for sql, params in indexed_sql:
            plan = (await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, params)).all()
            assert any("USING INDEX ix_execution_trace_turn_id" in row[3] for row in plan)
            if "INDEXED BY ix_execution_trace_source_event" in sql:
                assert any("USING INDEX ix_execution_trace_source_event" in row[3] for row in plan)


@pytest.mark.asyncio
async def test_outbound_event_links_to_original_conversation_turn(database, tmp_path):
    env = await social_env(database, tmp_path)
    assert await env.router.cas_takeover_person(env.person) == "taken"
    recorder = TraceRecorder(database)
    async with trace_span(
        "turn", {}, recorder=recorder, conversation_id=env.context.conversation_id
    ):
        sent = await env.service.execute(
            "send_message",
            {"text": "private delivery", "target": {"kind": "person", "target_id": env.person}},
            env.context,
        )
    async with database.sessions() as session:
        delivered = await session.get(ChatEventModel, sent["event_id"])
        assert delivered is not None
        target = ConversationId.parse(delivered.canonical_conversation_id)
    assert target.text != env.context.conversation_id
    linked = await list_event_turns(
        database.sessions,
        PageRequest(),
        conversation_id=target,
        event_id=delivered.id,
        direction="outbound",
    )
    assert linked.total == 1
    assert linked.items[0].fields["original_conversation_id"] == env.context.conversation_id
    origin = ConversationId.parse(env.context.conversation_id)
    summary = await read_conversation_execution(
        database.sessions, origin, recorder, include_content=True
    )
    sent_messages = summary.fields["recent"][0]["messages"]
    assert any(
        item["event_id"] == delivered.id
        and item["direction"] == "sent"
        and item["conversation_id"] == target.text
        and item["content"] == "private delivery"
        for item in sent_messages
    )


@pytest.mark.asyncio
async def test_live_query_service_requires_execution_metadata_capability(database, tmp_path):
    env = await social_env(database, tmp_path)
    conversation = ConversationId.parse(env.context.conversation_id)
    queries = ControlQueryService(ControlQueryAdapter(database))
    with pytest.raises(TypeError, match="trace_recorder"):
        ControlQueryAdapter(database, trace_recorder=object())
    async with database.sessions() as session:
        event = await session.scalar(select(ChatEventModel))
        assert event is not None
    with pytest.raises(ControlQueryError):
        await queries.read_conversation_execution(
            context("control.chat.metadata.read"), conversation
        )
    with pytest.raises(ControlQueryError):
        await queries.read_conversation_execution(
            context("control.execution.metadata.read"), conversation, include_content=True
        )
    with pytest.raises(ControlQueryError):
        await queries.list_event_turns(
            context("control.chat.metadata.read"),
            PageRequest(),
            conversation_id=conversation,
            event_id=event.id,
            direction="inbound",
        )
