"""Absent human input is silent; real input carriers keep their normal route."""

import base64
import io
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image
from sqlalchemy import func, select
from tests.conftest import MemorySender, build_harness, make_settings

from yuki.domain.conversations import ScopeType
from yuki.domain.messages import (
    AttachmentKind,
    InboundMessage,
    MessageAttachment,
    SenderIdentity,
)
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import ChatEventModel
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import inputs
from yuki.services.attachment_inputs import AttachmentInputService
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.services.media_resolver import MediaResolver


def message(**changes):
    return replace(
        InboundMessage(
            message_id="empty-input",
            event_type="message:test",
            scope_type=ScopeType.PRIVATE,
            sender=SenderIdentity("1001"),
            bot_user_id="8000",
            text="",
        ),
        **changes,
    )


@pytest.mark.parametrize("platform", ["qq", "telegram"])
@pytest.mark.parametrize("text", ["", " \n\t"])
async def test_absent_input_never_enters_runtime_or_records_a_chat(
    database, monkeypatch, platform, text
):
    provider = FakeLLMProvider(lambda _: "NO_REPLY")
    harness = build_harness(database, make_settings(database.url), provider)
    processor = harness.processor
    ingress = AsyncMock(wraps=processor._canonical_ingress.pre_admit)
    pin = Mock(wraps=processor._chat.pin_model_runtime)
    notify = AsyncMock(wraps=processor._turn_coordinator.notify_message)
    stage = AsyncMock(wraps=processor._chat.stage_work_input)
    monkeypatch.setattr(processor._canonical_ingress, "pre_admit", ingress)
    monkeypatch.setattr(processor._chat, "pin_model_runtime", pin)
    monkeypatch.setattr(processor._turn_coordinator, "notify_message", notify)
    monkeypatch.setattr(processor._chat, "stage_work_input", stage)
    committed = Mock()
    sender = MemorySender()

    result = await processor.handle(
        message(
            platform=platform,
            text=text,
            raw_text="platform service metadata",
            segments=({"type": "yuki_context", "data": {"text": "runtime metadata"}},),
        ),
        sender,
        on_committed=committed,
    )

    assert result.handled and result.reason == "empty" and result.sent_messages == 0
    assert not provider.requests and not sender.messages and sender.calls == 0
    ingress.assert_not_awaited()
    pin.assert_not_called()
    notify.assert_not_awaited()
    stage.assert_not_awaited()
    committed.assert_not_called()
    async with database.sessions() as reader:
        assert await reader.scalar(select(func.count()).select_from(ChatEventModel)) == 0
        assert await reader.scalar(select(func.count()).select_from(inputs)) == 0


@pytest.mark.parametrize("unresolved_reply", [False, True])
async def test_absent_qq_input_does_not_enqueue_or_interrupt_original_work(
    database, unresolved_reply
):
    provider = FakeLLMProvider(lambda _: "NO_REPLY")
    harness = build_harness(database, make_settings(database.url), provider)
    processor = harness.processor
    assert (await processor.handle(message(text="继续原工作"), MemorySender())).reason == "chat"
    async with database.sessions() as reader:
        original = (await reader.scalars(select(ChatEventModel))).one()
    repository = WorkRepository(database)
    lease = await repository.acquire(original.canonical_conversation_id, 1)
    assert lease is not None

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(
        repository,
        lease,
        "original-blank-input-work",
        {"actor_person_id": original.author_person_id, "principal_kind": "person"},
        validate,
    )
    await control.execute("task_control", {"action": "accept", "goal": "继续原工作"}, "accept")
    before = await repository.get(control.current["id"])
    sender = MemorySender()
    try:
        with processor._chat.runtime.bindings.bind(
            f"conversation:{original.canonical_conversation_id}", control
        ):
            result = await processor.handle(
                message(
                    message_id="blank-during-work",
                    reply_to_message_id="unknown-original" if unresolved_reply else None,
                ),
                sender,
            )
        assert result.reason == "empty" and sender.calls == 0
        assert len(provider.requests) == 1
        assert await repository.get(control.current["id"]) == before
        async with database.sessions() as reader:
            staged = list((await reader.execute(select(inputs))).mappings())
            if unresolved_reply:
                assert len(staged) == 1
                assert staged[0]["state"] == "cancelled" and not staged[0]["ready"]
                assert staged[0]["payload_json"] == "{}"
            else:
                assert not staged
            assert await reader.scalar(select(func.count()).select_from(ChatEventModel)) == (
                2 if unresolved_reply else 1
            )
    finally:
        await repository.release(lease)


@pytest.mark.parametrize("carrier", ["image", "audio", "file", "reply_text", "reply_image"])
async def test_empty_body_with_real_input_still_reaches_main(database, carrier):
    provider = FakeLLMProvider(lambda _: "NO_REPLY")
    harness = build_harness(database, make_settings(database.url), provider)
    processor = harness.processor
    incoming = message()
    resolver = MediaResolver()
    processor._native_images = AttachmentInputService(
        resolver,
        ImagePreprocessor(),
        concurrency=1,
        pending_limit=8,
        timeout=30,
        max_bytes=1024 * 1024,
    )
    if carrier in {"image", "reply_image"}:
        image = io.BytesIO()
        Image.new("RGB", (8, 8), "blue").save(image, "PNG")
        attachment = MessageAttachment(
            AttachmentKind.IMAGE,
            "photo",
            source="reply" if carrier == "reply_image" else "current",
            file="base64://" + base64.b64encode(image.getvalue()).decode(),
        )
        incoming = replace(
            incoming,
            **{"reply_attachments" if carrier == "reply_image" else "attachments": (attachment,)},
        )
    elif carrier == "audio":
        from yuki.asr.service import AudioInput
        from yuki.domain.audio import AudioTranscript, serialize_transcripts

        processor._asr = SimpleNamespace(
            prepare=AsyncMock(
                return_value=AudioInput(
                    transcript=serialize_transcripts((AudioTranscript("current", 0, "语音内容"),))
                )
            )
        )
        incoming = replace(
            incoming, attachments=(MessageAttachment(AttachmentKind.AUDIO, "voice", file="voice"),)
        )
    elif carrier == "file":
        incoming = replace(
            incoming,
            attachments=(
                MessageAttachment(AttachmentKind.FILE, "notes.txt", filename="notes.txt"),
            ),
        )
    else:
        incoming = replace(incoming, reply_text="引用的真实内容")
    sender = MemorySender()
    try:
        result = await processor.handle(incoming, sender)
        assert result.reason == "chat" and len(provider.requests) == 1
        assert sender.calls == 0
        if carrier in {"image", "reply_image"}:
            assert any(item.images for item in provider.requests[0].messages)
        elif carrier == "audio":
            assert "语音内容" in str(provider.requests[0].messages)
    finally:
        await resolver.close()


async def test_mention_only_greeting_and_explicit_command_keep_their_routes(database):
    provider = FakeLLMProvider(lambda _: "NO_REPLY")
    harness = build_harness(database, make_settings(database.url), provider)
    incoming = message(scope_type=ScopeType.GROUP, group_id="2001", mentions_bot=True)
    assert (await harness.processor.handle(incoming, MemorySender())).reason == "chat"
    assert len(provider.requests) == 1
    assert "只 @ 了你" in str(provider.requests[0].messages)
    sender = MemorySender()
    result = await harness.processor.handle(message(message_id="help", text="/ai help"), sender)
    assert result.reason == "command_help" and sender.calls == 1
    assert len(provider.requests) == 1


@pytest.mark.parametrize("internal", [False, True])
async def test_reference_without_body_reaches_canonical_resolution(database, internal):
    provider = FakeLLMProvider(lambda _: "NO_REPLY")
    harness = build_harness(database, make_settings(database.url), provider)
    processor = harness.processor
    await processor.handle(message(text="原消息的真实内容"), MemorySender())
    async with database.sessions() as reader:
        original = (await reader.scalars(select(ChatEventModel))).one()
    incoming = message(
        message_id="reference-only",
        **(
            {"reply_to_event_id": original.id}
            if internal
            else {"reply_to_message_id": "empty-input"}
        ),
    )
    committed = Mock()
    sender = MemorySender()
    result = await processor.handle(incoming, sender, on_committed=committed)
    assert result.reason == "chat" and len(provider.requests) == 2 and sender.calls == 0
    committed.assert_called_once()
    assert committed.call_args.args[0].event.reply_to_event_id == original.id
    current = provider.requests[-1].messages[-1].content
    assert f"回复:#{original.id}" in current
    assert "原消息的真实内容" in current
