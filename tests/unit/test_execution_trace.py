"""Real HTTP boundaries and durable, authorized diagnostics without paid calls."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import event, insert, select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.fixed_contract_fixture import bind_main_contract
from tests.support.runtime_wire import install_wire
from tests.support.social_identity_cases import social_env
from tests.unit.test_commands_and_chat import inbound
from tests.unit.test_control_plane_foundation import context

from qq_ai_bot.control_plane import ControlQueryError, ControlQueryService, PageRequest, ProblemCode
from qq_ai_bot.control_plane.query_types import ExecutionTraceFilter
from qq_ai_bot.control_plane.wire import control_response
from qq_ai_bot.domain.identity import ConversationId
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.execution_trace.db_models import ExecutionTraceEntryModel
from qq_ai_bot.execution_trace.payload import decode_payload, encode_payload
from qq_ai_bot.execution_trace.recorder import (
    TraceRecorder,
    current_trace,
    record_trace,
    trace_span,
)
from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.llm.openai_responses import OpenAIResponsesProvider
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.persistence.models import ChatEventModel, ConversationMediaItemModel
from qq_ai_bot.persistence.people_repository import PeopleRepository


def decoded(row):
    return decode_payload(row.payload_gzip, size=row.payload_bytes, digest=row.payload_sha256)


async def rows(database):
    async with database.sessions() as session:
        return list(
            await session.scalars(
                select(ExecutionTraceEntryModel).order_by(ExecutionTraceEntryModel.id)
            )
        )


def test_media_and_opaque_state_are_copied_without_mutating_recovery():
    value = {
        "reasoning_content": "readable thought",
        "content": [
            {"type": "thinking", "thinking": "readable", "signature": "secret-signature"},
            {"type": "redacted_thinking", "data": "opaque-claude"},
            {"type": "reasoning.encrypted", "data": "opaque-openrouter"},
            {"type": "image", "source": {"data": "image-bytes"}},
        ],
        "encrypted_content": "opaque-responses",
        "thoughtSignature": "opaque-gemini",
        "inlineData": {"data": "video-frame"},
    }
    original = json.dumps(value)
    encoded = encode_payload(value, 4096)
    assert encoded.status == "redacted"
    evidence = decode_payload(encoded.compressed, size=encoded.size, digest=encoded.sha256)
    assert evidence["data"]["reasoning_content"] == "readable thought"
    assert evidence["data"]["content"][0]["thinking"] == "readable"
    serialized = json.dumps(evidence)
    for secret in (
        "secret-signature",
        "opaque-claude",
        "opaque-openrouter",
        "image-bytes",
        "opaque-responses",
        "opaque-gemini",
        "video-frame",
    ):
        assert secret not in serialized
    assert json.dumps(value) == original
    oversized = encode_payload({"content": "x" * 2048}, 1024)
    assert oversized.status == "omitted_size" and oversized.compressed is None
    with pytest.raises(ValueError):
        decode_payload(encoded.compressed, size=encoded.size - 1, digest=encoded.sha256)


@pytest.mark.parametrize(
    "kind",
    [OpenAICompatibleProvider, OpenAIResponsesProvider, AnthropicMessagesProvider, GeminiProvider],
)
async def test_all_protocols_capture_the_actual_wire_and_readable_reasoning(database, kind):
    captured = []
    if kind is AnthropicMessagesProvider:
        body = {
            "stop_reason": "end_turn",
            "content": [
                {"type": "thinking", "thinking": "evidence", "signature": "opaque"},
                {"type": "text", "text": "done"},
            ],
        }
    elif kind is GeminiProvider:
        body = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [
                            {"thought": True, "text": "evidence", "thoughtSignature": "opaque"},
                            {"text": "done"},
                        ]
                    },
                }
            ]
        }
    elif kind is OpenAIResponsesProvider:
        body = {
            "id": "resp-test",
            "status": "completed",
            "output": [
                {
                    "type": "reasoning",
                    "id": "reason-test",
                    "summary": [{"type": "summary_text", "text": "evidence"}],
                    "encrypted_content": "opaque",
                },
                {
                    "type": "message",
                    "id": "msg-test",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "done"}],
                },
            ],
        }
    else:
        body = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "done",
                        "reasoning_content": "evidence",
                        "reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque"}],
                    },
                }
            ]
        }

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=body)

    recorder = TraceRecorder(database)
    async with httpx.AsyncClient(
        base_url="https://trace.invalid/v1/", transport=httpx.MockTransport(handler)
    ) as client:
        provider = kind(
            base_url="https://trace.invalid/v1/",
            api_key="never-persist-this-key",
            timeout_seconds=2,
            max_retries=0,
            client=client,
        )
        async with trace_span("turn", {"source": "test"}, recorder=recorder):
            response = await provider.complete(
                ChatRequest(
                    messages=(ChatMessage("system", "persona"), ChatMessage("user", "question")),
                    model="model",
                    max_output_tokens=1024,
                )
            )
    assert response.content == "done" and len(captured) == 1
    evidence = await rows(database)
    starts = [row for row in evidence if row.kind == "provider_start"]
    assert len(starts) == 1
    assert decoded(starts[0])["data"]["body"] == captured[0]
    returned = [row for row in evidence if row.kind == "provider_response"]
    text = json.dumps(decoded(returned[0]))
    assert "evidence" in text and '"opaque"' not in text
    assert all("never-persist-this-key" not in json.dumps(decoded(row)) for row in evidence)
    assert len({row.turn_id for row in evidence}) == 1
    assert starts[0].parent_operation_id == evidence[0].operation_id
    assert recorder.record_failures == 0 and current_trace.get() is None


async def test_each_retry_is_distinct_and_preserves_upstream_error(database):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return (
            httpx.Response(500, json={"error": {"message": "upstream failed"}})
            if calls == 1
            else httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "done"},
                        }
                    ]
                },
            )
        )

    async with httpx.AsyncClient(
        base_url="https://trace.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://trace.invalid",
            api_key="test",
            timeout_seconds=2,
            max_retries=1,
            client=client,
        )
        async with trace_span("turn", {}, recorder=TraceRecorder(database)):
            response = await provider.complete(
                ChatRequest(messages=(ChatMessage("user", "question"),), model="model")
            )
    assert response.content == "done" and calls == 2
    evidence = await rows(database)
    starts = [row for row in evidence if row.kind == "provider_start"]
    assert len(starts) == 2 and starts[0].operation_id != starts[1].operation_id
    assert [
        decoded(row)["data"]["http_status"] for row in evidence if row.kind == "provider_response"
    ] == [500, 200]
    assert any(row.kind == "provider_error" for row in evidence)


async def test_recording_failure_does_not_discard_response_or_repeat_effect(database):
    recorder = TraceRecorder(database)

    def fail_trace_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO execution_trace_entries"):
            raise RuntimeError("diagnostic storage unavailable")

    event.listen(database.engine.sync_engine, "before_cursor_execute", fail_trace_insert)
    effects = []
    try:
        async with trace_span("turn", {}, recorder=recorder) as span:
            effects.append("once")
            span.result = "success"
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", fail_trace_insert)
    assert effects == ["once"] and recorder.record_failures == 2
    assert current_trace.get() is None


async def test_cancel_preserves_error_and_clears_scope(database):
    started = asyncio.Event()

    async def run():
        async with trace_span("turn", {}, recorder=TraceRecorder(database)):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    evidence = await rows(database)
    assert [row.kind for row in evidence] == ["turn_start", "turn_error"]
    assert decoded(evidence[-1])["data"]["error_category"] == "CancelledError"
    assert current_trace.get() is None


async def test_query_authorization_paging_retention_and_corruption(database):
    recorder = TraceRecorder(database, max_payload_bytes=1024)
    async with trace_span("turn", {"prompt": "private"}, recorder=recorder):
        await record_trace("tool_result", {"content": "x" * 2048})
    service = ControlQueryService(ControlQueryAdapter(database))
    scope = ExecutionTraceFilter()
    metadata = context("control.execution.metadata.read")
    content = context("control.execution.metadata.read", "control.execution.content.read")
    with pytest.raises(ControlQueryError) as denied:
        await service.list_execution_trace(context(), PageRequest(), scope=scope)
    assert denied.value.problem.code is ProblemCode.CAPABILITY_DENIED
    selected_sql = []

    def capture_sql(conn, cursor, statement, parameters, execution_context, executemany):
        if statement.startswith("SELECT") and "FROM execution_trace_entries" in statement:
            selected_sql.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture_sql)
    try:
        first = await service.list_execution_trace(metadata, PageRequest(limit=1), scope=scope)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture_sql)
    assert selected_sql and all("payload_gzip" not in statement for statement in selected_sql)
    assert first.items[0].payload is None and first.next_cursor
    with pytest.raises(ControlQueryError):
        await service.list_execution_trace(
            metadata, PageRequest(), scope=scope, include_content=True
        )
    with pytest.raises(ControlQueryError):
        await service.read_execution_trace(metadata, first.items[0].id)
    with pytest.raises(ControlQueryError) as crossed:
        await service.list_execution_trace(
            metadata,
            PageRequest(cursor=first.next_cursor),
            scope=ExecutionTraceFilter(turn_id="other"),
        )
    assert crossed.value.problem.code is ProblemCode.VALIDATION_ERROR
    next_page = await service.list_execution_trace(
        content, PageRequest(limit=1, cursor=first.next_cursor), scope=scope, include_content=True
    )
    assert (
        next_page.items[0].payload_status == "omitted_size" and next_page.items[0].payload is None
    )
    full = await service.read_execution_trace(content, first.items[0].id)
    assert full.payload["data"]["prompt"] == "private"
    wire = json.loads(json.dumps(control_response(content.request_id, full)))
    assert wire["data"]["id"] == full.id
    assert wire["data"]["payload"]["data"]["prompt"] == "private"
    with pytest.raises(TypeError):
        full.payload["new"] = "mutable"
    with pytest.raises(ControlQueryError):
        await service.read_execution_trace(content, full.id, conversation_id=ConversationId.new())
    for invalid_blob in (None, b"corrupt gzip"):
        async with database.sessions() as session, session.begin():
            row = await session.get(ExecutionTraceEntryModel, full.id)
            row.payload_gzip = invalid_blob
        with pytest.raises(ControlQueryError) as corrupt:
            await service.read_execution_trace(content, full.id)
        assert corrupt.value.problem.code is ProblemCode.STATE_MISMATCH
    assert await recorder.cleanup_expired(now=datetime.now(UTC) + timedelta(days=31)) == 3
    assert not (await service.list_execution_trace(metadata, PageRequest(), scope=scope)).items
    async with trace_span("turn", {}, recorder=recorder):
        pass
    assert (await rows(database))[0].id > 3


async def test_expiry_cleanup_drains_backlog_in_separate_short_transactions(database):
    now = datetime.now(UTC)
    expired = dict(
        turn_id="expired",
        operation_id="expired",
        kind="model_start",
        payload_status="omitted_size",
        payload_bytes=2048,
        created_at=now - timedelta(days=31),
        expires_at=now - timedelta(days=1),
    )
    async with database.sessions() as session, session.begin():
        await session.execute(
            insert(ExecutionTraceEntryModel), [dict(expired) for _ in range(1001)]
        )
        await session.execute(
            insert(ExecutionTraceEntryModel),
            dict(expired, turn_id="live", expires_at=now + timedelta(days=1)),
        )
    commits = []

    def committed(conn):
        commits.append(conn)

    event.listen(database.engine.sync_engine, "commit", committed)
    try:
        assert await TraceRecorder(database).cleanup_expired(now=now) == 1001
    finally:
        event.remove(database.engine.sync_engine, "commit", committed)
    assert len(commits) == 3
    remaining = await rows(database)
    assert len(remaining) == 1 and remaining[0].turn_id == "live"
    assert await TraceRecorder(database).cleanup_expired(now=now) == 0


async def test_privacy_forget_fences_inflight_context_and_new_calls_can_record(database):
    from qq_ai_bot.identity.canonical_repository import ensure_person

    async with database.sessions() as session, session.begin():
        await ensure_person(session, "1001")
    recorder = TraceRecorder(database)
    async with trace_span("turn", {"context": "old-person-data"}, recorder=recorder):
        assert await PeopleRepository(database).delete_person("1001")
        await record_trace("provider_response", {"old": "old-person-data"})
    assert not await rows(database)
    async with trace_span("turn", {"context": "new"}, recorder=recorder):
        pass
    assert len(await rows(database)) == 2


@pytest.mark.parametrize("work_enabled", [False, True])
async def test_real_runner_records_tools_and_original_chat_delivery(
    database, tmp_path, work_enabled
):
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        if work_enabled and count == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "accept",
                        ToolFunction(
                            "task_control",
                            '{"action":"accept","goal":"Say hello","output_kind":"answer"}',
                        ),
                    ),
                ),
            )
        if count == 1 + int(work_enabled):
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall("inspect", ToolFunction("request_tools", '{"query":"send_message"}')),
                ),
            )
        if count == 2 + int(work_enabled):
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall("delivery", ToolFunction("send_message", '{"text":"hello"}')),
                ),
            )
        return ChatResponse("内部收尾，不再自动发送", 0)

    env = await social_env(database, tmp_path)
    from qq_ai_bot.identity.db_models import CanonicalSpaceModel

    async with database.sessions() as session, session.begin():
        space = await session.get(CanonicalSpaceModel, env.space)
        space.enabled = True
    fake = FakeLLMProvider(respond)
    harness = build_harness(
        database,
        make_settings(database.url, enabled_groups_csv="20001", runtime_work_enabled=work_enabled),
        fake,
    )
    bind_main_contract(harness, tmp_path)
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    client, captured = install_wire(chat, fake, "chat_completions")
    chat._models.traces = TraceRecorder(database)
    sender = MemorySender()
    try:
        await harness.processor.handle(
            replace(
                inbound(
                    "say hello",
                    message_id="trace-message",
                    user_id="10001",
                    group_id="20001",
                    mentions_bot=True,
                ),
                bot_user_id="80001",
                conversation_id=env.context.conversation_id,
                legacy_conversation_key="bot:80001:group:20001",
                person_id=env.person,
                space_id=env.space,
                presence_id=env.presence,
            ),
            sender,
        )
    finally:
        await client.aclose()
    evidence = await rows(database)
    assert evidence and chat._models.traces.record_failures == 0
    assert sum(row.kind == "model_start" for row in evidence) == len(captured)
    assert any(
        row.kind == "tool_start" and "delivery" in json.dumps(decoded(row)) for row in evidence
    )
    async with database.sessions() as session:
        admitted = await session.scalar(
            select(ChatEventModel)
            .where(ChatEventModel.direction == "inbound")
            .order_by(ChatEventModel.id.desc())
        )
        conversation_id = ConversationId.parse(admitted.canonical_conversation_id)
        session.add(
            ConversationMediaItemModel(
                source_event_id=admitted.id,
                attachment_index=0,
                conversation_id=conversation_id.text,
                generation=0,
                segment_index=0,
                kind="image",
                display_name="image",
                created_at=datetime.now(UTC),
                cache_status="expired",
            )
        )
        await session.commit()
    service = ControlQueryService(ControlQueryAdapter(database))
    operator = context(
        "control.chat.metadata.read",
        "control.chat.content.read",
        "control.execution.metadata.read",
        "control.execution.content.read",
    )
    messages = await service.list_chat_events(
        operator, PageRequest(), conversation_id=conversation_id, include_content=True
    )
    assert any(item.direction == "outbound" and item.content == "hello" for item in messages.items)
    assert next(
        item for item in messages.items if item.event_id == admitted.id
    ).attachment_indexes == (0,)
    receipts = await service.list_social_receipts(
        operator, PageRequest(), conversation_id=conversation_id
    )
    succeeded = [item for item in receipts.items if item.status == "succeeded"]
    assert len(succeeded) == 1 and succeeded[0].event_id in {
        item.event_id for item in messages.items
    }
    assert [action for action, _ in env.bot.calls].count("send_group_msg") == 1
    assert sender.calls == 0
    full = await service.list_execution_trace(
        operator,
        PageRequest(limit=100),
        scope=ExecutionTraceFilter(conversation_id=conversation_id),
        include_content=True,
    )
    assert full.items and any(item.source_event_id == admitted.id for item in full.items)
    assert any(item.work_id for item in full.items) is work_enabled
    assert len({item.turn_id for item in full.items}) == 1
    from sqlalchemy import delete

    from qq_ai_bot.persistence.database import Database
    from qq_ai_bot.runtime.work_schema_v1 import journal

    if work_enabled:
        work_id = next(item.work_id for item in full.items if item.work_id)
        by_work = await service.list_execution_trace(
            operator,
            PageRequest(limit=100),
            scope=ExecutionTraceFilter(conversation_id=conversation_id, work_id=work_id),
        )
        assert {item.id for item in by_work.items} == {item.id for item in full.items}
        async with database.sessions() as session, session.begin():
            await session.execute(delete(journal).where(journal.c.work_id == work_id))
    restarted = Database(database.url)
    try:
        restarted_service = ControlQueryService(ControlQueryAdapter(restarted))
        retained = await restarted_service.list_execution_trace(
            operator,
            PageRequest(limit=100),
            scope=ExecutionTraceFilter(
                conversation_id=conversation_id, source_event_id=admitted.id
            ),
            include_content=True,
        )
        assert retained.items and all(
            item.source_event_id == admitted.id for item in retained.items
        )
        assert any(item.kind == "provider_start" for item in retained.items)
    finally:
        await restarted.close()


async def test_tool_batch_retains_reused_denied_and_parallel_results(database):
    from types import SimpleNamespace

    from qq_ai_bot.services.agent_runner import AgentRunner
    from qq_ai_bot.services.concurrency import ConcurrencyManager

    runner = AgentRunner(FakeLLMProvider(), ConcurrencyManager(2))
    executed = []
    active = 0
    maximum = 0
    both_started = asyncio.Event()

    class Backend:
        def begin_batch(self, *args):
            pass

        def parallel_safe(self, *args):
            return True

        def is_side_effecting(self, *args):
            return False

        async def execute(self, name, arguments, runtime):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=2)
            executed.append(json.loads(arguments))
            active -= 1
            return '{"ok":true}'

    calls = (
        ToolCall("first", ToolFunction("read", '{"item":1}')),
        ToolCall("duplicate", ToolFunction("read", '{"item":1}')),
        ToolCall("second", ToolFunction("read", '{"item":2}')),
        ToolCall("denied", ToolFunction("unknown", "{}")),
    )
    runtime = SimpleNamespace(work_control=None)
    cache = {}
    async with trace_span("turn", {}, recorder=TraceRecorder(database)):
        result = await runner._execute_tool_batch(
            calls,
            Backend(),
            runtime,
            remaining_calls=8,
            max_parallel_calls=2,
            reusable_results=cache,
            cacheable_names=frozenset({"read"}),
            declared_names=frozenset({"read"}),
        )
        again = await runner._execute_tool_batch(
            (ToolCall("reuse", calls[0].function),),
            Backend(),
            runtime,
            remaining_calls=8,
            max_parallel_calls=2,
            reusable_results=cache,
            cacheable_names=frozenset({"read"}),
            declared_names=frozenset({"read"}),
        )
    assert sorted(executed, key=lambda value: value["item"]) == [{"item": 1}, {"item": 2}]
    assert maximum == 2 and again.reused_count == 1
    assert len(result.calls) == 4
    evidence = await rows(database)
    batch_ends = [
        decoded(row)["data"]["result"] for row in evidence if row.kind == "tool_batch_end"
    ]
    first_calls = batch_ends[0]["calls"]
    assert {call[0]["id"] for call in first_calls} == {"first", "duplicate", "second", "denied"}
    assert any(call[0]["id"] == "denied" and not call[2] for call in first_calls)
    assert batch_ends[1]["reused_count"] == 1
    assert sum(row.kind == "tool_start" for row in evidence) == 2
