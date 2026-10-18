"""Ordinary handoff uses the selected business input and the original writer."""

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from tests.conftest import MemorySender
from tests.support.projection import projection_record
from tests.unit.test_history_dispatch_ownership import _scene

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.initiative_sources import source_revision
from yuki.conversation.ordinary_admission import (
    OrdinaryAdmissionConflict,
    OrdinaryAdmissionDuplicate,
    OrdinaryAdmissionRepository,
)
from yuki.conversation.projection_models import PromptProjectionModel
from yuki.conversation.projections import ProjectionConflict
from yuki.domain.audio import AudioTranscript, serialize_transcripts
from yuki.domain.messages import ChatImage, ChatMessage, ChatRequest, ChatResponse
from yuki.execution_trace.db_models import ExecutionTraceStateModel
from yuki.identity.db_models import PresenceModel
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.capacity import estimate_request_tokens
from yuki.model_runtime.models import ModelExecutionPriority
from yuki.persistence.models import ChatEventModel
from yuki.runtime.origin import TurnOrigin
from yuki.runtime.work_activation import activate_work, current_work_control
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_session import WorkSession
from yuki.runtime.work_source_guard import WorkSourceGuard
from yuki.services.agent_runner import AgentRuntime
from yuki.services.context_boundary import OrdinaryContextInput
from yuki.services.context_compaction import EXECUTION_COMPACT_INSTRUCTION
from yuki.services.turn_coordinator import HistorySourceChangedError
from yuki.services.turn_execution import TurnExecution, _PrimaryDispatch
from yuki.services.turn_transcript import (
    DispatchOrigin,
    TranscriptRequest,
    validating_request,
)


async def _boundary_scene(database, tmp_path):
    provider = FakeLLMProvider(lambda _: ChatResponse("NO_REPLY", 0))
    env, harness, chat, _, message = await _scene(database, tmp_path, provider)
    original_compose = chat.runtime.main_turns.compose
    captured = []

    async def capture(**kwargs):
        captured.append(kwargs)
        return await original_compose(**kwargs)

    chat.runtime.main_turns.compose = capture
    result = await harness.processor.handle(message, MemorySender())
    assert result.reason == "chat"
    candidates = {}
    hook = AsyncMock()
    preparation = AsyncMock()

    async def reader(event_ids):
        await preparation(event_ids)
        return tuple(candidates[key] for key in event_ids if key in candidates)

    # Build a new test composition after the first real dispatch. The captured
    # incremental snapshot belongs to that earlier boundary and must not be
    # replayed as though its original head had never committed.
    kwargs = {
        **captured[0],
        "context": replace(
            captured[0]["context"], history_incremental=False, prepared_fragments=None
        ),
        "ordinary_inputs": reader,
    }
    composition = await original_compose(**kwargs)
    with validating_request(TranscriptRequest(composition.messages, None, ())):
        await composition.commit_projection()
    ordinary = OrdinaryAdmissionRepository(database)
    owner = await ordinary.get(captured[0]["context"].current_event_id)
    assert owner is not None and owner.route == "ordinary"
    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id=message.sender.user_id,
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="ordinary-boundary",
        current_group_id=message.group_id,
        bot_user_id=message.bot_user_id,
        gateway=None,
        runtime_config=await chat._runtime_config.snapshot(),
        allowed_capabilities=frozenset(),
        max_tool_calls=8,
        max_model_requests=8,
        before_model_request=AsyncMock(),
    )

    async def add(*, private=None, allowed=None, sender="10002"):
        appended = await env.service.writer.append(
            scope=message.scope(),
            platform_message_id=f"boundary-{len(candidates)}",
            sender_user_id=sender,
            direction="inbound",
            content="原始的普通补充",
        )
        prepared = ordinary.prepare(appended.event, owner.generation, 999)
        candidate = OrdinaryContextInput(prepared, private, allowed, hook)
        candidates[appended.event.id] = candidate
        return candidate

    return SimpleNamespace(
        env=env,
        harness=harness,
        chat=chat,
        composition=composition,
        ordinary=ordinary,
        owner=owner,
        runtime=runtime,
        candidates=candidates,
        hook=hook,
        preparation=preparation,
        add=add,
    )


async def _read_boundary(scene):
    boundary = await scene.composition.observation_boundary(frozenset())
    assert boundary is not None
    return boundary


@asynccontextmanager
async def _guarded(scene):
    async with activate_work(
        WorkRepository(scene.ordinary.database),
        scene.owner.conversation_id,
        scene.owner.generation,
        f"event:{scene.owner.event_id}",
        {"actor_person_id": scene.owner.actor_person_id},
        AsyncMock(),
    ) as control:
        control.session = WorkSession(control, "test-ordinary-boundary")
        control.session.source_guard = WorkSourceGuard(scene.composition.read_version)
        control.session.source_revision = scene.composition.read_version.prompt_source_revision
        assert await control.session.source_guard.check(control)
        yield control


def _dispatch(scene, boundary, *, private=True, public=True, business=True):
    messages = (
        *(message for _, message in boundary.fragments if public),
        *(boundary.current_inputs if private else ()),
    )
    return _PrimaryDispatch(
        runner=scene.chat.runtime.runner,
        runtime=scene.runtime,
        request_index=1,
        execute=AsyncMock(return_value=ChatResponse("", 0)),
        sequence=TranscriptRequest(messages, None, ()),
        boundary=boundary,
        observed_event_ids=set(),
        business_inputs=business,
    )


async def _projection(database):
    async with database.sessions() as reader:
        row = await projection_record(
            reader, (await reader.scalars(select(PromptProjectionModel))).one()
        )
        return row.revision, row.payload_json


@pytest.mark.parametrize("private", [None, ChatMessage("user", "准备所得私有附件正文")])
async def test_selected_ordinary_input_keeps_original_activation_and_private_material(
    database, tmp_path, private
):
    scene = await _boundary_scene(database, tmp_path)
    candidate = await scene.add(private=private)

    async def committed():
        # The feedback reads a separate session, so uncommitted ownership is
        # never mistaken for a successful publication.
        accepted = await scene.ordinary.get(candidate.prepared.event.id)
        assert accepted is not None and accepted.activation_id == scene.owner.activation_id
        assert "原始的普通补充" in (await _projection(database))[1]

    scene.hook.side_effect = committed
    boundary = await _read_boundary(scene)
    before = await _projection(database)
    dispatch = _dispatch(scene, boundary)
    await dispatch.admit()
    await dispatch.admit()
    accepted = await scene.ordinary.get(candidate.prepared.event.id)
    assert accepted == replace(
        candidate.prepared.admission,
        activation_id=scene.owner.activation_id,
        coordinator_version=scene.owner.coordinator_version,
    )
    assert accepted.actor_person_id != scene.owner.actor_person_id
    assert accepted.route == "ordinary" and accepted.work_id is None
    assert scene.runtime.actor_user_id == "10001"
    assert scene.runtime.delegated_authority is None
    assert scene.runtime.before_model_request.await_count == 2
    scene.hook.assert_awaited_once_with()
    after = await _projection(database)
    assert after[0] == before[0] + 1 and "原始的普通补充" in after[1]
    assert "准备所得私有附件正文" not in after[1]
    assert boundary.current_inputs == (() if private is None else (private,))
    if private is None:
        assert len(dispatch.sequence.messages) == len(boundary.fragments)


@pytest.mark.parametrize("missing", ["private", "public"])
async def test_missing_selected_material_does_not_take_ordinary_input(database, tmp_path, missing):
    scene = await _boundary_scene(database, tmp_path)
    image = ChatImage("data:image/png;base64,AA==")
    candidate = await scene.add(private=ChatMessage("user", "私有图片", images=(image,)))
    boundary = await _read_boundary(scene)
    before = await _projection(database)
    dispatch = _dispatch(scene, boundary, private=missing != "private", public=missing != "public")
    await dispatch.admit()
    assert dispatch.selected_boundary is None
    assert await scene.ordinary.get(candidate.prepared.event.id) is None
    assert await _projection(database) == before
    scene.hook.assert_not_awaited()


async def test_compact_publication_does_not_take_input_but_retained_business_input_can(
    database, tmp_path
):
    scene = await _boundary_scene(database, tmp_path)
    candidate = await scene.add(private=ChatMessage("user", "私有附件"))
    boundary = await _read_boundary(scene)
    compact = _dispatch(scene, boundary, private=False, business=False)
    await compact.admit()
    after_compact = await _projection(database)
    assert "原始的普通补充" in after_compact[1]
    assert await scene.ordinary.get(candidate.prepared.event.id) is None
    scene.hook.assert_not_awaited()
    await _dispatch(scene, boundary).admit()
    assert (await scene.ordinary.get(candidate.prepared.event.id)).activation_id == (
        scene.owner.activation_id
    )
    assert await _projection(database) == after_compact
    scene.hook.assert_awaited_once_with()


@pytest.mark.parametrize("arrival", ["preparation", "after_selection"])
async def test_later_arrival_is_not_added_to_selected_request_or_skipped_by_watermark(
    database, tmp_path, arrival
):
    scene = await _boundary_scene(database, tmp_path)
    selected = await scene.add(private=ChatMessage("user", "本次准备的 B 材料"))
    later = None

    async def prepare(event_ids):
        nonlocal later
        if arrival == "preparation" and later is None:
            later = await scene.add(private=ChatMessage("user", "后到的 C 材料"))

    scene.preparation.side_effect = prepare
    boundary = await _read_boundary(scene)
    if later is None:
        later = await scene.add(private=ChatMessage("user", "后到的 C 材料"))
    assert selected.prepared.event.id in boundary.event_ids
    assert later.prepared.event.id not in boundary.event_ids
    assert boundary.current_inputs == (selected.message,)
    await _dispatch(scene, boundary).admit()
    assert await scene.ordinary.get(later.prepared.event.id) is None

    following = await scene.composition.observation_boundary(boundary.event_ids)
    assert following is not None
    assert later.prepared.event.id in following.event_ids
    assert selected.prepared.event.id not in following.event_ids
    await _dispatch(scene, following).admit()
    for candidate in (selected, later):
        assert (await scene.ordinary.get(candidate.prepared.event.id)).activation_id == (
            scene.owner.activation_id
        )


@pytest.mark.parametrize("change", ["source", "privacy", "projection", "generation"])
async def test_compact_publication_still_checks_sources_in_business_admission_writer(
    database, tmp_path, change
):
    scene = await _boundary_scene(database, tmp_path)
    candidate = await scene.add(private=ChatMessage("user", "仍完整的私有材料"))
    boundary = await _read_boundary(scene)
    await _dispatch(scene, boundary, private=False, business=False).admit()
    assert boundary.prepare is not None
    prepared = await boundary.prepare(True, boundary.ordinary_inputs)
    prepared.stage()
    # Change the real source after the last preparation read, before the
    # admission writer. Compact has already published the public fragments.
    async with database.immediate_session() as writer:
        if change == "source":
            original = await writer.get(ChatEventModel, scene.owner.event_id)
            original.content = "原 A 来源在最终读取后修订"
        elif change == "privacy":
            privacy = await writer.get(ExecutionTraceStateModel, 1)
            if privacy is None:
                writer.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
            else:
                privacy.privacy_generation += 1
        elif change == "projection":
            projection = await writer.get(PromptProjectionModel, scene.owner.conversation_id)
            projection.revision += 1
        else:
            conversation = await writer.get(CanonicalConversationModel, scene.owner.conversation_id)
            conversation.generation += 1
    after_change = await _projection(database)
    try:
        with pytest.raises(ProjectionConflict):
            async with database.immediate_session() as writer:
                await prepared.publication(writer)
    finally:
        prepared.rollback()
    assert await scene.ordinary.get(candidate.prepared.event.id) is None
    assert await _projection(database) == after_change
    scene.hook.assert_not_awaited()


async def test_execution_compact_selects_only_candidates_with_retained_private_material(
    database, tmp_path
):
    scene = await _boundary_scene(database, tmp_path)
    covered = await scene.add(private=ChatMessage("user", "C 的较早私有材料。" * 800))
    retained = await scene.add(private=ChatMessage("user", "D 的完整私有材料。" * 80))
    runner = scene.chat.runtime.runner
    runtime = replace(scene.runtime, observation_boundary=scene.composition.observation_boundary)
    execution = TurnExecution(runner, scene.composition.messages, runtime, None)
    await execution.begin(1)
    boundary = execution.state.boundary
    assert boundary is not None
    request = ChatRequest(messages=execution.state.transcript.request().messages)
    fitting_prefix = (
        *request.messages[:-1],
        ChatMessage("user", EXECUTION_COMPACT_INSTRUCTION),
    )
    budget = estimate_request_tokens(
        runner._capacity_request(replace(request, messages=fitting_prefix))
    )
    assert estimate_request_tokens(runner._capacity_request(request)) > budget
    compacted = await execution.compact_current(
        1, request, ModelExecutionPriority.FOREGROUND, budget
    )
    # Use the actual capacity planner and compact adoption: public C/D survive,
    # while only D's private input remains outside the covered prefix.
    assert all(message in compacted.messages for _, message in boundary.fragments)
    assert covered.message not in compacted.messages
    assert retained.message in compacted.messages
    after_compact = await _projection(database)
    assert await scene.ordinary.get(covered.prepared.event.id) is None
    assert await scene.ordinary.get(retained.prepared.event.id) is None
    dispatch = replace(_dispatch(scene, boundary), sequence=execution.state.transcript.request())
    await dispatch.admit()
    assert await scene.ordinary.get(covered.prepared.event.id) is None
    assert (await scene.ordinary.get(retained.prepared.event.id)).activation_id == (
        scene.owner.activation_id
    )
    assert await _projection(database) == after_compact
    scene.hook.assert_awaited_once_with()


@pytest.mark.parametrize("change", ["policy", "content", "presence", "generation", "competing"])
async def test_input_conflict_rolls_back_projection_and_does_not_notify(database, tmp_path, change):
    scene = await _boundary_scene(database, tmp_path)
    allowed = AsyncMock(return_value=change != "policy")
    candidate = await scene.add(allowed=allowed)
    boundary = await _read_boundary(scene)
    before = await _projection(database)
    if change == "competing":
        assert await scene.ordinary.commit(candidate.prepared)
    elif change != "policy":
        async with database.immediate_session() as writer:
            if change == "content":
                source = await writer.get(ChatEventModel, candidate.prepared.event.id)
                source.content = "真正修订后的来源"
            elif change == "presence":
                presence = await writer.get(PresenceModel, candidate.prepared.admission.presence_id)
                presence.enabled = False
            else:
                conversation = await writer.get(
                    CanonicalConversationModel, scene.owner.conversation_id
                )
                conversation.generation += 1
        before = await _projection(database)
    with pytest.raises(HistorySourceChangedError):
        await _dispatch(scene, boundary).admit()
    assert await _projection(database) == before
    existing = await scene.ordinary.get(candidate.prepared.event.id)
    assert (existing is not None) is (change == "competing")
    if existing is not None:
        assert existing.activation_id == candidate.prepared.admission.activation_id
    scene.hook.assert_not_awaited()


async def test_same_activation_replay_reads_original_and_other_activation_cannot_overwrite(
    database, tmp_path
):
    scene = await _boundary_scene(database, tmp_path)
    candidate = await scene.add()
    async with database.immediate_session() as writer:
        assert await scene.ordinary.commit_to_activation(
            candidate.prepared, scene.owner, session=writer
        )
    accepted = await scene.ordinary.get(candidate.prepared.event.id)
    async with database.immediate_session() as writer:
        assert not await scene.ordinary.commit_to_activation(
            candidate.prepared, scene.owner, session=writer
        )
    with pytest.raises(OrdinaryAdmissionDuplicate):
        async with database.immediate_session() as writer:
            await scene.ordinary.commit_to_activation(
                candidate.prepared,
                replace(scene.owner, activation_id="another-original-activation"),
                session=writer,
            )
    assert await scene.ordinary.get(candidate.prepared.event.id) == accepted


async def test_ordinary_source_checks_exact_segments_beyond_existing_revision(database, tmp_path):
    scene = await _boundary_scene(database, tmp_path)
    candidate = await scene.add(private=ChatMessage("user", "旧附件准备所得材料"))
    async with database.immediate_session() as writer:
        source = await writer.get(ChatEventModel, candidate.prepared.event.id)
        source.segments_json = json.dumps([{"type": "file", "data": {"file": "replacement.txt"}}])
    refreshed = await scene.harness.ledger.get_event(candidate.prepared.event.id)
    assert source_revision(refreshed) == source_revision(candidate.prepared.event)
    assert refreshed.segments != candidate.prepared.event.segments
    with pytest.raises(OrdinaryAdmissionConflict, match="ordinary_source_changed"):
        async with database.immediate_session() as writer:
            await scene.ordinary.commit_to_activation(
                candidate.prepared, scene.owner, session=writer
            )
    assert await scene.ordinary.get(candidate.prepared.event.id) is None


async def test_original_preparation_revision_is_rendered_and_published(database, tmp_path):
    scene = await _boundary_scene(database, tmp_path)
    candidate = await scene.add()

    async def prepare(event_ids):
        assert event_ids == (candidate.prepared.event.id,)
        assert await scene.harness.ledger.set_audio_transcript(
            candidate.prepared.event.id,
            serialize_transcripts((AudioTranscript("current", 0, "原语音已经准备完成"),)),
            generation=scene.owner.generation,
        )
        refreshed = await scene.harness.ledger.get_event(candidate.prepared.event.id)
        scene.candidates[refreshed.id] = replace(
            candidate,
            prepared=scene.ordinary.prepare(refreshed, scene.owner.generation, 999),
        )

    scene.preparation.side_effect = prepare
    before = await _projection(database)
    async with _guarded(scene):
        boundary = await _read_boundary(scene)
        assert "原语音已经准备完成" in "\n".join(m.content for _, m in boundary.fragments)
        await _dispatch(scene, boundary).admit()
    accepted = await scene.ordinary.get(candidate.prepared.event.id)
    assert (
        accepted.source_revision
        == scene.candidates[accepted.event_id].prepared.admission.source_revision
    )
    assert "原语音已经准备完成" in (await _projection(database))[1]
    original_items = json.loads(before[1])
    assert json.loads((await _projection(database))[1])[: len(original_items)] == original_items


@pytest.mark.parametrize("change", ["content", "segments"])
async def test_prepared_source_that_changed_before_selection_remains_unadmitted(
    database, tmp_path, change
):
    scene = await _boundary_scene(database, tmp_path)
    candidate = await scene.add(private=ChatMessage("user", "已经过时的私有附件"))

    async def revise(event_ids):
        async with database.immediate_session() as writer:
            source = await writer.get(ChatEventModel, candidate.prepared.event.id)
            if change == "content":
                source.content = "准备期间另行修订的新内容"
            else:
                source.segments_json = json.dumps(
                    [{"type": "file", "data": {"file": "replacement.txt"}}]
                )

    scene.preparation.side_effect = revise
    async with _guarded(scene):
        boundary = await _read_boundary(scene)
        assert boundary.current_inputs == ()
        await _dispatch(scene, boundary).admit()
    assert await scene.ordinary.get(candidate.prepared.event.id) is None
    if change == "content":
        assert "准备期间另行修订的新内容" in (await _projection(database))[1]
    assert "已经过时的私有附件" not in (await _projection(database))[1]
    scene.hook.assert_not_awaited()


async def test_enriching_new_input_cannot_bless_a_changed_original_prefix(database, tmp_path):
    scene = await _boundary_scene(database, tmp_path)
    candidate = await scene.add()
    async with _guarded(scene):
        async with database.immediate_session() as writer:
            original = await writer.get(ChatEventModel, scene.owner.event_id)
            original.content = "原执行来源已经改变"
        boundary = await _read_boundary(scene)
        before = await _projection(database)
        with pytest.raises(HistorySourceChangedError):
            await _dispatch(scene, boundary).admit()
    assert await scene.ordinary.get(candidate.prepared.event.id) is None
    assert await _projection(database) == before
    scene.hook.assert_not_awaited()


@pytest.mark.parametrize("origin", list(DispatchOrigin))
async def test_admitted_input_stays_in_ordinary_source_guard_after_publication(
    database, tmp_path, origin
):
    scene = await _boundary_scene(database, tmp_path)
    candidate = await scene.add()
    boundary = await _read_boundary(scene)
    await _dispatch(scene, boundary).admit()
    accepted = await scene.ordinary.get(candidate.prepared.event.id)
    before = await _projection(database)
    sequence = TranscriptRequest((ChatMessage("user", "原执行私有续接"),), None, (), origin=origin)
    with validating_request(sequence):
        await scene.composition.commit_projection()
    assert await _projection(database) == before
    async with database.immediate_session() as writer:
        source = await writer.get(ChatEventModel, candidate.prepared.event.id)
        source.content = "接纳之后来源确实发生修订"
    invalidated = await _projection(database)
    with validating_request(sequence), pytest.raises(HistorySourceChangedError):
        await scene.composition.commit_projection()
    assert await scene.ordinary.get(candidate.prepared.event.id) == accepted
    assert await _projection(database) == invalidated


async def test_work_accepted_after_freezing_ordinary_payload_preserves_original_opportunity(
    database, tmp_path
):
    scene = await _boundary_scene(database, tmp_path)
    candidate = await scene.add()
    boundary = await _read_boundary(scene)
    before = await _projection(database)
    token = current_work_control.set(SimpleNamespace(current={"id": "original-work"}))
    try:
        with pytest.raises(HistorySourceChangedError):
            await _dispatch(scene, boundary).admit()
    finally:
        current_work_control.reset(token)
    assert await scene.ordinary.get(candidate.prepared.event.id) is None
    assert await _projection(database) == before
    scene.hook.assert_not_awaited()
