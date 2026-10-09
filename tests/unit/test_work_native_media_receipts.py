"""Selected tool pixels survive the accepted/paired crash boundary privately."""

import json
from dataclasses import replace
from hashlib import sha256

import pytest
from sqlalchemy import select, update
from tests.support.work_session import WorkSession, invoke_tool
from tests.unit.test_semantic_participation_host import _event_and_route
from tests.unit.test_work_effect_results import owned_session
from tests.unit.test_work_protocol_continuity import _control
from tests.unit.test_work_source_guard import _guard

from qq_ai_bot.admin.models import WorkStorageRuntimeConfig
from qq_ai_bot.capabilities.media import MediaResultText, result_images
from qq_ai_bot.capabilities.results import ToolExecutionResult
from qq_ai_bot.domain.messages import ChatImage, ChatMessage, ToolCall, ToolFunction
from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.effect_outcomes import current_result_capture
from qq_ai_bot.runtime.protocol_schema import refs
from qq_ai_bot.runtime.work_journal import JournalUnavailable
from qq_ai_bot.runtime.work_repository import WorkConflict
from qq_ai_bot.runtime.work_schema_v1 import effects
from qq_ai_bot.services.turn_transcript import TurnTranscript, validating_request


def _image(control, identity):
    return ChatImage(
        f"data:image/png;base64,{identity}",
        source="history",
        conversation_id=control.lease.conversation_id,
        generation=control.lease.generation,
        source_event_id=101,
        attachment_index=0,
        content_hash=sha256(identity.encode()).hexdigest(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_phase", ["response", "paired"])
async def test_root_business_resume_keeps_pending_selected_pixels_without_old_tool_replay(
    database, tmp_path, crash_phase
):
    control, session, _store = await owned_session(database, tmp_path)
    old_inbound = ChatImage("data:image/png;base64,retired-current", source="current")
    session.transcript.append(
        ChatMessage("user", "retired conversation text", images=(old_inbound,))
    )
    call = ToolCall("selected", ToolFunction("inspect_conversation_attachment", "{}"))
    session.transcript.append(ChatMessage("assistant", tool_calls=(call,)))
    await session.save("response", (call,))
    image = _image(control, "root-original-pixels")
    executed = 0

    async def invoke():
        nonlocal executed
        executed += 1
        return MediaResultText('{"ok":true}', (image,))

    receipt = await invoke_tool(session, call, invoke, side_effecting=False)
    if crash_phase == "paired":
        session.transcript.append_result(call.id, receipt)
        session.transcript.append_tool_media(((call.id, receipt),))
        await session.save("paired")
    initial = TurnTranscript((ChatMessage("user", "continue original task"),))
    restored = await WorkSession(control, "result-test").restore(initial)
    assert [image for message in restored.request().messages for image in message.images] == [image]
    # A business resume is a legal new input chain: no old assistant call is replayed.
    assert not any(message.tool_calls for message in restored.request().messages)
    assert not any(
        "retired conversation text" in (message.content or "")
        for message in restored.request().messages
    )
    assert executed == 1
    assert control.tools_started == 1


@pytest.mark.asyncio
async def test_accepted_media_receipts_restore_before_paired_without_read_or_reexecute(
    database, tmp_path
):
    control = await _control(database, tmp_path, worker=True)
    session = WorkSession(control, "result-test")
    await session.restore(TurnTranscript((ChatMessage("user", "read selected images"),)))
    calls = tuple(
        ToolCall(identity, ToolFunction("inspect_conversation_attachment", "{}"))
        for identity in ("one", "two")
    )
    session.transcript.append(ChatMessage("assistant", tool_calls=calls))
    await session.save("response", calls)
    originals = []
    invoked = []
    for call in calls:
        image = _image(control, call.id)
        originals.append(image)

        async def invoke(image=image, identity=call.id):
            invoked.append(identity)
            return MediaResultText('{"ok":true,"data":{"mode":"image"}}', (image,))

        await invoke_tool(session, call, invoke, side_effecting=False)
    # Simulated crash: the last persisted journal is still response/pending.
    resumed = WorkSession(control, "result-test")
    restored = await resumed.restore(TurnTranscript(()))
    messages = restored.request().messages
    assert [message.tool_call_id for message in messages if message.role == "tool"] == [
        "one",
        "two",
    ]
    assert [image for message in messages for image in message.images] == originals
    first_image = next(index for index, message in enumerate(messages) if message.images)
    assert all(message.role != "tool" for message in messages[first_image:])

    async def forbidden():
        pytest.fail("Accepted tool preparation must not execute or reread its source")

    replay = await invoke_tool(resumed, calls[0], forbidden, side_effecting=False)
    assert result_images(replay) == (originals[0],)
    assert invoked == ["one", "two"]
    assert control.tools_started == 2
    async with database.sessions() as reader:
        receipts = list(await reader.scalars(select(effects.c.receipt_json)))
        assert receipts and all("data:image/" not in receipt for receipt in receipts)
        assert all("media_result_ref" in receipt for receipt in receipts)
        assert len(list(await reader.scalars(select(refs.c.sha256)))) >= 4
    await resumed.save("paired")
    again = await WorkSession(control, "result-test").restore(TurnTranscript(()))
    assert [image for message in again.request().messages for image in message.images] == originals


@pytest.mark.asyncio
async def test_missing_private_pixels_fail_closed_without_rereading(database, tmp_path):
    control, session, _store = await owned_session(database, tmp_path)
    image = _image(control, "original")
    call = ToolCall("image", ToolFunction("workspace_inspect", "{}"))

    async def invoke():
        return MediaResultText('{"ok":true}', (image,))

    await invoke_tool(session, call, invoke, side_effecting=False)
    digest = sha256(image.data_url.encode()).hexdigest()
    session.journal.objects._path(digest).unlink()
    with pytest.raises(JournalUnavailable, match="work_effect_media_missing"):
        await session.journal.effect_result(session.call_key(call.id))


@pytest.mark.asyncio
async def test_erasure_during_prepare_cannot_publish_late_image_receipt(database, tmp_path):
    control, session, _store = await owned_session(database, tmp_path)
    call = ToolCall("erased", ToolFunction("inspect_conversation_attachment", "{}"))

    async def invoke():
        async with database.sessions() as writer, writer.begin():
            state = await writer.get(ExecutionTraceStateModel, 1)
            if state is None:
                writer.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
            else:
                state.privacy_generation += 1
        return MediaResultText('{"ok":true}', (_image(control, "private"),))

    with pytest.raises(WorkConflict, match="work_effect_media_source_changed"):
        await invoke_tool(session, call, invoke, side_effecting=False)
    async with database.sessions() as reader:
        assert await reader.scalar(select(effects.c.state)) == "prepared"
        assert not list(await reader.scalars(select(refs.c.sha256)))

    # An unknown receipt is not permission to run the old tool again.
    async def forbidden():
        pytest.fail("Erased preparation must not reexecute")

    replay = await invoke_tool(session, call, forbidden, side_effecting=False)
    assert json.loads(replay)["error"] == "execution_outcome_unknown"
    assert not result_images(replay)


@pytest.mark.asyncio
async def test_erasure_fences_already_owned_media_on_a_later_receipt(database, tmp_path):
    control, session, _store = await owned_session(database, tmp_path)
    image = _image(control, "same-original")
    first = ToolCall("first", ToolFunction("workspace_inspect", "{}"))

    async def initial():
        return MediaResultText('{"ok":true}', (image,))

    await invoke_tool(session, first, initial, side_effecting=False)
    second = ToolCall("second", ToolFunction("workspace_inspect", "{}"))

    async def erased():
        async with database.sessions() as writer, writer.begin():
            writer.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
        return MediaResultText('{"ok":true}', (image,))

    with pytest.raises(WorkConflict, match="work_effect_media_source_changed"):
        await invoke_tool(session, second, erased, side_effecting=False)
    async with database.sessions() as reader:
        assert (
            await reader.scalar(
                select(effects.c.state).where(effects.c.effect_key == session.call_key("second"))
            )
            == "prepared"
        )


@pytest.mark.asyncio
async def test_typed_media_survives_text_projection_failure(database, tmp_path):
    control, session, _store = await owned_session(database, tmp_path)
    image = _image(control, "prepared-before-text-failed")
    call = ToolCall("projection", ToolFunction("workspace_inspect", "{}"))

    async def invoke():
        capture = current_result_capture.get()
        capture.outcome = ToolExecutionResult(
            ok=True, images=(image,), tool_name="workspace_inspect"
        )
        raise ValueError("text projection failed")

    with pytest.raises(ValueError, match="text projection failed"):
        await invoke_tool(session, call, invoke, side_effecting=False)
    recovered = await session.journal.effect_result(session.call_key(call.id))
    assert result_images(recovered) == (image,)
    assert json.loads(recovered)["result_unavailable"]


@pytest.mark.asyncio
async def test_protocol_capacity_failure_does_not_accept_incomplete_pixels(
    database, tmp_path, monkeypatch
):
    control, session, _store = await owned_session(database, tmp_path)
    call = ToolCall("capacity", ToolFunction("workspace_inspect", "{}"))

    async def keep_test_policy():
        pass

    monkeypatch.setattr(session.journal.objects, "refresh_policy", keep_test_policy)
    session.journal.objects.policy = WorkStorageRuntimeConfig(object_max_bytes=128)

    async def invoke():
        return MediaResultText('{"ok":true}', (_image(control, "x" * 512),))

    with pytest.raises(ValueError, match="work_protocol_object_capacity"):
        await invoke_tool(session, call, invoke, side_effecting=False)
    async with database.sessions() as reader:
        assert await reader.scalar(select(effects.c.state)) == "prepared"
        assert not list(await reader.scalars(select(refs.c.sha256)))


@pytest.mark.asyncio
async def test_receipt_writer_publishes_only_prepared_metadata(database, tmp_path, monkeypatch):
    control, session, _store = await owned_session(database, tmp_path)
    key = session.call_key("writer")
    await control.repository.prepare_effect(control.lease, control.current["id"], key, "tool")
    entering_receipt_writer = False
    published_files = []
    original_publish = session.journal.objects._publish
    original_record = control.repository.record_effect

    def publish(digest, content):
        assert not entering_receipt_writer, "pixels must be written before the receipt writer"
        published_files.append(digest)
        return original_publish(digest, content)

    async def record(*args, **kwargs):
        nonlocal entering_receipt_writer
        entering_receipt_writer = True
        assert len(published_files) == 2  # original pixels plus their small source manifest
        assert kwargs["prepared_protocol"]
        return await original_record(*args, **kwargs)

    monkeypatch.setattr(session.journal.objects, "_publish", publish)
    monkeypatch.setattr(control.repository, "record_effect", record)
    await session.journal.record_effect(
        key, "accepted", {"result": MediaResultText('{"ok":true}', (_image(control, "pixels"),))}
    )
    async with database.sessions() as reader:
        assert await reader.scalar(select(effects.c.state)) == "accepted"
        assert set(await reader.scalars(select(refs.c.sha256))) == set(published_files)


@pytest.mark.asyncio
async def test_dispatch_selected_history_image_adds_source_dependency(database):
    ledger = EventLedgerRepository(database)
    initial = await _event_and_route(database, ledger)
    selected = await _event_and_route(database, ledger, content="selected attachment source")
    guard, control = await _guard(database, initial)
    image = ChatImage(
        "data:image/png;base64,pixels",
        source="history",
        conversation_id=control.lease.conversation_id,
        generation=control.lease.generation,
        source_event_id=selected.id,
        attachment_index=0,
    )
    transcript = TurnTranscript((ChatMessage("user", images=(image,)),))
    with validating_request(transcript.request()):
        assert await guard.check(control)
    assert selected.id in guard.additional_events
    saved = guard.snapshot()
    async with database.sessions() as writer, writer.begin():
        await writer.execute(
            update(ChatEventModel).where(ChatEventModel.id == selected.id).values(content="erased")
        )
    assert not await type(guard).restore(saved).check(control)
    foreign = replace(image, conversation_id="other-conversation")
    with validating_request(TurnTranscript((ChatMessage("user", images=(foreign,)),)).request()):
        assert not await guard.check(control)
