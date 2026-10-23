"""Shared files and archived tool pixels cross the real model admission boundary."""

from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace

import pytest
from sqlalchemy import update as sql_update
from tests.integration.test_telegram_attachment_tools import attachment_tools as attachment_tools
from tests.integration.test_telegram_event_edits import _edit, _path, _photo, _source
from tests.integration.test_telegram_event_edits import media_reader as media_reader
from tests.integration.test_telegram_runtime import drain, update
from tests.integration.test_telegram_runtime import telegram_env as telegram_env
from tests.support.event_seed import seed_scoped_event
from tests.support.model_executor import InjectedModelExecutor
from tests.support.service_dependencies import workspace_service

from yuki.domain.conversations import ConversationScope
from yuki.domain.messages import ChatMessage, ChatRequest
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import ConversationMediaItemModel
from yuki.persistence.people_repository import PeopleRepository
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_session import WorkSession
from yuki.services.agent_runner import AgentRunner
from yuki.services.main_agent_backend import MainAgentBackend
from yuki.services.turn_execution import _PrimaryDispatch
from yuki.services.turn_transcript import TurnTranscript
from yuki.workspace.store import WorkspaceError, WorkspaceStore


async def dispatch(workspace, tool_runtime, transcript, conversation_id, provider, control=None):
    service = SimpleNamespace(
        _tools=SimpleNamespace(workspace_service=workspace),
        _settings=SimpleNamespace(vision_max_prepared_bytes=6_291_456),
    )
    backend = MainAgentBackend(service, tool_runtime)
    runner = AgentRunner(model_executor=InjectedModelExecutor(provider), concurrency=None)
    sequence = transcript.request()
    images = tuple(image for message in transcript.portable_entries() for image in message.images)
    runtime = SimpleNamespace(
        runtime_config=SimpleNamespace(vision=SimpleNamespace(max_frames_per_turn=4)),
        canonical_conversation_id=conversation_id,
        before_model_request=None,
        work_control=control,
        auxiliary_requests=[],
    )
    return await _PrimaryDispatch(
        runner=runner,
        runtime=runtime,
        request_index=len(provider.requests),
        execute=partial(provider.complete, ChatRequest(model="fake", messages=sequence.messages)),
        sequence=sequence,
        boundary=None,
        observed_event_ids=set(),
        tools=backend,
        selected_media=images,
    ).complete()


async def selected(case):
    return await case.workspace.execute(
        "inspect_conversation_attachment",
        {"event_id": case.source.id, "attachment_index": 0, "question": "read this image"},
        runtime=case.runtime,
        request_id="select-original",
    )


async def test_cross_conversation_attachment_reaches_main_model(attachment_tools, database):
    case = attachment_tools
    other = (
        await seed_scoped_event(
            ScopedEventLedgerUnitOfWork(database),
            scope=ConversationScope.group(
                case.current_client.self_id,
                "-10042",
                platform="telegram",
                presence_id=case.runtime.presence_id,
                thread_id="other-topic",
            ),
            sender_user_id="43",
            platform_message_id="-10042:other-topic-input",
            direction="inbound",
            content="read the shared image from the other topic",
        )
    ).event
    assert other.canonical_conversation_id != case.source.canonical_conversation_id
    result = await selected(case)
    transcript = TurnTranscript(
        (ChatMessage("user", "selected shared file", images=result.images),)
    )
    provider = FakeLLMProvider(lambda _: "actual shared pixels received")
    await dispatch(
        case.workspace, case.runtime, transcript, other.canonical_conversation_id, provider
    )
    assert provider.requests[0].messages[0].images == result.images


async def test_archived_work_pixels_survive_raw_cache_expiry(attachment_tools, database):
    case = attachment_tools
    result = await selected(case)
    _item, original = await case.media.authorized_path(event_id=case.source.id, attachment_index=0)
    repository = WorkRepository(database)
    lease = await repository.acquire(case.source.canonical_conversation_id, 1)

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(
        repository, lease, "selected-image-work", {"trigger_event_id": case.source.id}, validate
    )
    control.current = await repository.accept(
        lease, source_key=control.source_key, source=control.source, goal="continue from this image"
    )
    try:
        first = WorkSession(control, "shared-image-contract")
        control.session = first
        transcript = await first.restore(
            TurnTranscript((ChatMessage("user", "inspect and follow up"),))
        )
        transcript.append(
            ChatMessage("user", "[Host 工具媒体观察：call_id=inspect]", images=result.images)
        )
        provider = FakeLLMProvider(lambda _: "the archived image is visible")
        response = await dispatch(
            case.workspace, case.runtime, transcript, lease.conversation_id, provider, control
        )
        transcript.append(ChatMessage("assistant", response.content))
        await first.save("paired")
        async with database.immediate_session() as writer:
            await writer.execute(
                sql_update(ConversationMediaItemModel)
                .where(ConversationMediaItemModel.source_event_id == case.source.id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        await case.media.cleanup()
        assert not original.exists()
        resumed = WorkSession(control, "shared-image-contract")
        control.session = resumed
        restored = await resumed.restore(
            TurnTranscript((ChatMessage("user", "continue this work"),))
        )
        archived = tuple(
            image for message in restored.portable_entries() for image in message.images
        )
        assert archived == result.images
        await dispatch(
            case.workspace, case.runtime, restored, lease.conversation_id, provider, control
        )
        assert len(provider.requests) == 2
        assert any(message.images == archived for message in provider.requests[-1].messages)
        with pytest.raises(WorkspaceError, match="attachment_expired"):
            await selected(case)
        assert len([path for _method, path in case.wire if path.endswith("original.png")]) == 1
    finally:
        await repository.release(lease)


async def test_real_person_forget_still_revokes_selected_pixels(attachment_tools, database):
    case = attachment_tools
    result = await selected(case)
    assert await PeopleRepository(database).delete_person_id(case.source.author_person_id)
    transcript = TurnTranscript((ChatMessage("user", "selected image", images=result.images),))
    provider = FakeLLMProvider(lambda _: "must not be called")
    with pytest.raises(WorkspaceError, match="attachment_not_found"):
        await dispatch(
            case.workspace,
            case.runtime,
            transcript,
            case.source.canonical_conversation_id,
            provider,
        )
    assert not provider.requests


async def test_real_native_edit_still_revokes_old_selected_pixels(
    telegram_env, database, media_reader, tmp_path
):
    env, reader = telegram_env, media_reader
    incoming = update(text="")
    _photo(incoming["message"])
    await env.gateway.receive(incoming)
    await drain(env)
    source = await _source(database)
    item, path = await _path(reader, source)
    selected = await reader.service.inspect(item, path, "read original")
    edited = _edit(2)
    _photo(edited["edited_message"], "new-photo")
    await env.gateway.receive(edited)
    await _path(reader, source)
    workspace = workspace_service(WorkspaceStore(tmp_path / "artifacts"))
    workspace.conversation_media = reader.service
    provider = FakeLLMProvider(lambda _: "must not be called")
    transcript = TurnTranscript((ChatMessage("user", "old pixels", images=selected.images),))
    with pytest.raises(WorkspaceError, match="attachment_source_changed"):
        await dispatch(workspace, None, transcript, source.canonical_conversation_id, provider)
    assert not provider.requests
