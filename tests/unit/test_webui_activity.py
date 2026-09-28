"""New history windows and media downloads retain original canonical boundaries."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event, insert
from tests.unit.test_canonical_ingress import _Bot, _message, _stack
from tests.unit.test_control_plane_foundation import context
from tests.unit.test_conversation_media import _Provider, _Resolver

from qq_ai_bot.control_plane import ControlQueryError, ControlQueryService, PageRequest
from qq_ai_bot.control_plane.query_types import ChatHistoryFilter, ExecutionTraceFilter
from qq_ai_bot.conversation.media_service import ConversationMediaService
from qq_ai_bot.domain.identity import ConversationId
from qq_ai_bot.identity.canonical_repository import ensure_presence
from qq_ai_bot.model_runtime.db_models import ModelInvocationModel
from qq_ai_bot.operations.reset_conversations import reset_all
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.persistence.models import ChatEventModel, ConversationMediaItemModel
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor


async def ingress(database):
    registry, resolver, uow = await _stack(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    return resolver, uow, bot


@pytest.mark.asyncio
async def test_metadata_queries_do_not_load_message_bodies_and_usage_null_is_not_zero(database):
    resolver, uow, bot = await ingress(database)
    admitted = await resolver.pre_admit(bot, _message(message_id="metadata", text="sensitive"))
    await uow.append_inbound(admitted.message, admitted)
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
                runtime_turn_id="offline-turn",
            )
        )
        await session.execute(
            insert(work).values(
                id=str(uuid4()),
                conversation_id=admitted.conversation_id,
                generation=1,
                source_key="offline-work",
                source_json="{}",
                goal="private goal",
                state="completed",
                model_requests=3,
                tool_calls=5,
                created=1,
                updated=2,
            )
        )
    queries = ControlQueryService(ControlQueryAdapter(database))
    statements = []

    def capture(_connection, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        chat = await queries.list_chat_events(
            context("control.chat.metadata.read"),
            PageRequest(),
            conversation_id=ConversationId.parse(admitted.conversation_id),
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert chat.items[0].content is None and chat.items[0].suppression_status == "keeper"
    assert chat.total is None
    assert not any(
        "count(" in statement.lower() and "chat_events" in statement.lower()
        for statement in statements
    )
    ledger_read = next(statement for statement in statements if "FROM chat_events" in statement)
    for column in (
        "chat_events.content",
        "chat_events.segments_json",
        "chat_events.audio_transcript",
    ):
        assert column not in ledger_read
    metadata = context("control.execution.metadata.read")
    usage = await queries.list_model_usage(metadata, PageRequest())
    assert usage.items[0].fields["prompt_tokens"] is None
    assert usage.items[0].fields["turn_id"] == "offline-turn"
    works = await queries.list_work(metadata, PageRequest())
    assert works.items[0].fields["model_requests"] == 3 and "goal" not in works.items[0].fields
    with pytest.raises(ControlQueryError):
        await queries.list_work(metadata, PageRequest(), include_content=True)


@pytest.mark.asyncio
async def test_model_usage_summary_counts_every_call_without_double_counting_cache(database):
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        for created_at, prompt, output, cached, total in (
            (now - timedelta(hours=1), 100, 20, 60, 120),
            (now - timedelta(hours=2), None, None, None, None),
            (now - timedelta(days=2), 10, 5, 0, 15),
        ):
            session.add(
                ModelInvocationModel(
                    task="chat_agent",
                    profile_id="main",
                    provider="fixture",
                    model="offline",
                    success=total is not None,
                    latency_seconds=1,
                    created_at=created_at,
                    prompt_tokens=prompt,
                    completion_tokens=output,
                    cached_prompt_tokens=cached,
                    total_tokens=total,
                )
            )
    queries = ControlQueryService(ControlQueryAdapter(database))
    permission = context("control.execution.metadata.read")
    recent = (await queries.read_model_usage_summary(permission, "24h")).fields
    assert recent["calls"] == 2
    assert recent["input_tokens"] == 100
    assert recent["output_tokens"] == 20
    assert recent["total_tokens"] == 120
    assert recent["cached_input_tokens"] == 60
    assert recent["missing_usage_calls"] == 1
    assert recent["models"][0]["calls"] == 2
    assert (await queries.read_model_usage_summary(permission, "7d")).fields["calls"] == 3
    with pytest.raises(ControlQueryError):
        await queries.read_model_usage_summary(permission, "all")
    with pytest.raises(ControlQueryError):
        await queries.read_model_usage_summary(context("control.chat.metadata.read"), "24h")


@pytest.mark.asyncio
async def test_participation_history_reports_latest_feedback_without_replaying_decisions(database):
    from tests.unit.test_autonomy_repository import _accept, _enable, _event, _scene

    from qq_ai_bot.conversation.autonomy_repository import AutonomyRepository

    scene = await _scene(database)
    repository = AutonomyRepository(database)
    binding = await _enable(repository, scene)
    first = (await _accept(repository, scene, binding, proposal="first")).run
    await repository.record_feedback(
        first.run_id, sequence=1, outcome="running", effect_refs=("receipt:fixture",)
    )
    await repository.record_feedback(first.run_id, sequence=2, outcome="completed")
    second = (
        await _accept(
            repository,
            scene,
            binding,
            proposal="second",
            sources=_event(2),
        )
    ).run
    await repository.record_feedback(second.run_id, sequence=1, outcome="no_reply")
    queries = ControlQueryService(ControlQueryAdapter(database))
    ctx = context("control.execution.metadata.read")
    first_page = await queries.list_participation_runs(
        ctx, PageRequest(limit=1), conversation_id=ConversationId.parse(scene.conversation)
    )
    assert first_page.items[0].resource_id == second.run_id
    assert first_page.items[0].fields["feedback"] == "no_reply"
    second_page = await queries.list_participation_runs(
        ctx,
        PageRequest(limit=1, cursor=first_page.next_cursor),
        conversation_id=ConversationId.parse(scene.conversation),
    )
    assert second_page.items[0].resource_id == first.run_id
    assert second_page.items[0].fields["feedback"] == "completed"
    assert (await repository.get_run(first.run_id)).feedback_sequence == 2
    assert "sources_json" not in json.dumps(dict(second_page.items[0].fields))
    statements = []

    def capture(_, __, statement, *args):
        statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        feedback = await queries.list_participation_feedback(
            ctx, PageRequest(limit=1), run_id=first.run_id, include_content=True
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert feedback.items[0].fields["outcome"] == "running"
    assert "effects" not in feedback.items[0].fields
    assert not any("payload_json" in sql or "sources_json" in sql for sql in statements)
    final = await queries.list_participation_feedback(
        ctx, PageRequest(limit=1, cursor=feedback.next_cursor), run_id=first.run_id
    )
    assert final.items[0].fields["outcome"] == "completed" and final.next_cursor is None
    with pytest.raises(ControlQueryError):
        await queries.list_participation_feedback(
            ctx, PageRequest(cursor=feedback.next_cursor), run_id=second.run_id
        )
    full_ctx = context("control.execution.metadata.read", "control.execution.content.read")
    full = await queries.list_participation_feedback(
        full_ctx, PageRequest(limit=1), run_id=first.run_id, include_content=True
    )
    assert full.items[0].fields["effects"] == ("receipt:fixture",)
    with pytest.raises(ControlQueryError):
        await queries.list_participation_feedback(
            full_ctx, PageRequest(cursor=full.next_cursor), run_id=first.run_id
        )


@pytest.mark.asyncio
async def test_automation_detail_revision_can_update_the_same_owner_without_delivery(
    database, tmp_path
):
    from tests.support.social_identity_cases import social_env
    from tests.unit.test_control_automation_authority import automation_service, group_script

    from qq_ai_bot.control_plane import ControlCommand, ControlCommandService
    from qq_ai_bot.domain.identity import RequestId
    from qq_ai_bot.persistence.control_command import ControlCommandAdapter

    env = await social_env(database, tmp_path)
    env.bot.calls.clear()
    commands = ControlCommandService(
        ControlCommandAdapter(database, automation=automation_service(database))
    )
    ctx = context("control.automation.mutate")
    script = group_script()
    created = await commands.mutate_automation(
        ctx,
        ControlCommand(
            request_id=ctx.request_id,
            expected_revision=0,
            payload={
                "action": "create",
                "spec": {
                    "owner_id": "self",
                    "conversation_id": env.context.conversation_id,
                    "script": script,
                },
            },
        ),
    )
    queries = ControlQueryService(ControlQueryAdapter(database))
    with pytest.raises(ControlQueryError):
        await queries.read_automation(context("control.automation.read"), int(created.resource_id))
    content = context("control.automation.content.read")
    detail = await queries.read_automation(content, int(created.resource_id))
    assert detail.fields["revision"] == created.revision
    assert detail.fields["creator_kind"] == "self"
    assert "authority_snapshot" not in json.dumps(dict(detail.fields), default=str)
    script["name"] = "edited through detail revision"
    ctx = replace(ctx, request_id=RequestId.new())
    changed = await commands.mutate_automation(
        ctx,
        ControlCommand(
            request_id=ctx.request_id,
            expected_revision=detail.fields["revision"],
            payload={"action": "update", "resource_id": created.resource_id, "spec": script},
        ),
    )
    updated = await queries.read_automation(content, int(created.resource_id))
    assert updated.fields["revision"] == changed.revision
    assert updated.fields["name"] == script["name"] and updated.fields["creator_kind"] == "self"
    assert updated.fields["target_space_id"] == env.space and env.bot.calls == []


@pytest.mark.asyncio
async def test_latest_history_cursor_and_event_lookup_never_cross_conversations(database):
    resolver, uow, bot = await ingress(database)
    stamp = datetime.now(UTC) - timedelta(minutes=10)
    ids = []
    for i in range(5):
        message = replace(
            _message(message_id=f"ui-{i}", user_id="1001"), received_at=stamp + timedelta(minutes=i)
        )
        admitted = await resolver.pre_admit(bot, message)
        appended = await uow.append_inbound(admitted.message, admitted)
        ids.append(appended.event.id)
    conversation = ConversationId.parse(admitted.conversation_id)
    other = await resolver.pre_admit(bot, _message(message_id="other", user_id="1002"))
    await uow.append_inbound(other.message, other)
    service = ControlQueryService(ControlQueryAdapter(database))
    ctx = context("control.chat.metadata.read", "control.chat.content.read")
    statements: list[str] = []

    def capture(_connection, _cursor, statement, *_args):
        statements.append(statement.lower())

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        first = await service.list_chat_events(
            ctx,
            PageRequest(limit=2),
            conversation_id=conversation,
            history=ChatHistoryFilter(descending=True),
            include_content=True,
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert [row.event_id for row in first.items] == ids[-2:][::-1]
    assert first.total is None and first.number is None
    assert not any("count(" in statement for statement in statements)
    second = await service.list_chat_events(
        ctx,
        PageRequest(limit=2, cursor=first.next_cursor),
        conversation_id=conversation,
        history=ChatHistoryFilter(descending=True),
    )
    assert [row.event_id for row in second.items] == ids[1:3][::-1]
    anchored = await service.list_chat_events(
        ctx,
        PageRequest(limit=2),
        conversation_id=conversation,
        history=ChatHistoryFilter(descending=True, through_event_id=ids[2]),
    )
    assert [row.event_id for row in anchored.items] == ids[2:0:-1]
    with pytest.raises(ControlQueryError):
        await service.list_chat_events(
            ctx,
            PageRequest(limit=2),
            conversation_id=ConversationId.parse(other.conversation_id),
            history=ChatHistoryFilter(descending=True, through_event_id=ids[2]),
        )
    with pytest.raises(ControlQueryError):
        await service.list_chat_events(
            ctx,
            PageRequest(cursor=first.next_cursor),
            conversation_id=ConversationId.parse(other.conversation_id),
            history=ChatHistoryFilter(descending=True),
        )
    with pytest.raises(ControlQueryError):
        await service.list_chat_events(
            ctx,
            PageRequest(cursor=first.next_cursor),
            conversation_id=conversation,
            history=ChatHistoryFilter(),
        )
    lookup = await service.list_chat_events(
        ctx,
        PageRequest(),
        conversation_id=ConversationId.parse(other.conversation_id),
        history=ChatHistoryFilter(event_id=ids[0]),
    )
    assert not lookup.items
    window = await service.list_chat_events(
        ctx,
        PageRequest(),
        conversation_id=conversation,
        history=ChatHistoryFilter(
            since=stamp + timedelta(minutes=1), until=stamp + timedelta(minutes=3)
        ),
    )
    assert [row.event_id for row in window.items] == ids[1:4]
    local_zone = timezone(timedelta(hours=8))
    local_window = await service.list_chat_events(
        ctx,
        PageRequest(),
        conversation_id=conversation,
        history=ChatHistoryFilter(
            since=(stamp + timedelta(minutes=1)).astimezone(local_zone),
            until=(stamp + timedelta(minutes=3)).astimezone(local_zone),
        ),
    )
    assert [row.event_id for row in local_window.items] == ids[1:4]

    # Numbered browsing uses the original event time, including when ledger IDs
    # and occurrence order differ. The count stays within the selected scope.
    async with database.sessions() as session, session.begin():
        earliest_id = await session.get(ChatEventModel, ids[0])
        assert earliest_id is not None
        earliest_id.occurred_at = stamp + timedelta(minutes=20)
    numbered = await service.list_chat_events(
        ctx,
        PageRequest(limit=2, number=1),
        conversation_id=conversation,
        history=ChatHistoryFilter(descending=True),
    )
    assert numbered.total == 5 and numbered.number == 1
    assert [row.event_id for row in numbered.items] == [ids[0], ids[4]]
    assert numbered.next_cursor is None
    filtered = await service.list_chat_events(
        ctx,
        PageRequest(limit=2, number=2),
        conversation_id=conversation,
        history=ChatHistoryFilter(
            descending=True,
            since=stamp + timedelta(minutes=2),
            until=stamp + timedelta(minutes=4),
        ),
    )
    assert filtered.total == 3 and [row.event_id for row in filtered.items] == [ids[2]]


@pytest.mark.asyncio
async def test_media_download_uses_existing_scope_expiry_and_reset_fences(database, tmp_path):
    resolver, uow, bot = await ingress(database)
    message = replace(
        _message(message_id="media", user_id="1001"),
        segments=({"type": "image", "data": {"file": "fixture-image"}},),
    )
    admitted = await resolver.pre_admit(bot, message)
    appended = await uow.append_inbound(admitted.message, admitted)
    other = await resolver.pre_admit(bot, _message(message_id="other", user_id="1002"))
    await uow.append_inbound(other.message, other)
    source = _Resolver()
    media = ConversationMediaService(
        database, tmp_path / "media", source, ImagePreprocessor(), _Provider()
    )
    service = ControlQueryService(ControlQueryAdapter(database, conversation_media=media))
    ctx = context("control.chat.content.read")
    conversation = ConversationId.parse(admitted.conversation_id)
    result = await service.download_chat_media(ctx, conversation, appended.event.id, 0)
    assert result.content == source.payload and result.media_type == "image/png"
    assert source.calls == 1
    with pytest.raises(ControlQueryError):
        await service.download_chat_media(
            context("control.chat.metadata.read"), conversation, appended.event.id, 0
        )
    with pytest.raises(ControlQueryError):
        await service.download_chat_media(
            ctx, ConversationId.parse(other.conversation_id), appended.event.id, 0
        )
    async with database.sessions() as session, session.begin():
        item = await session.get(ConversationMediaItemModel, (appended.event.id, 0))
        item.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(ControlQueryError):
        await service.download_chat_media(ctx, conversation, appended.event.id, 0)
    await reset_all(database, "webui-test-reset", apply=True)
    with pytest.raises(ControlQueryError):
        await service.download_chat_media(ctx, conversation, appended.event.id, 0)


@pytest.mark.parametrize("raw", [True, -1, 2**63])
def test_history_id_bounds(raw):
    with pytest.raises((TypeError, ValueError)):
        ChatHistoryFilter(event_id=raw)


def test_history_requires_aware_ordered_times():
    with pytest.raises(ValueError):
        ChatHistoryFilter(since=datetime(2026, 9, 27))
    with pytest.raises(ValueError):
        ChatHistoryFilter(
            since=datetime(2026, 9, 28, tzinfo=UTC), until=datetime(2026, 9, 27, tzinfo=UTC)
        )
    with pytest.raises(TypeError):
        ExecutionTraceFilter(descending="yes")
