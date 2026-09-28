"""Model image capability changes take effect for the next media input."""

import asyncio
import base64
import io
from dataclasses import replace
from types import MethodType

import pytest
from PIL import Image

from qq_ai_bot.admin.models import VisionRuntimeConfig
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    AttachmentKind,
    ChatImage,
    InboundMessage,
    MessageAttachment,
    SenderIdentity,
)
from qq_ai_bot.services.attachment_inputs import AttachmentInputService
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
from qq_ai_bot.services.media_resolver import MediaResolver
from qq_ai_bot.services.vision_service import VisionProcessingError


@pytest.mark.asyncio
async def test_ingress_pins_media_capability_until_agent_handoff():
    from qq_ai_bot.model_runtime.executor import TaskModelExecutor
    from qq_ai_bot.model_runtime.models import (
        ModelCapability,
        ModelProfile,
        ModelProtocol,
        ModelRoute,
        ModelTask,
    )
    from qq_ai_bot.model_runtime.pool import ModelClientPool
    from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
    from qq_ai_bot.model_runtime.routes import ModelRouter
    from qq_ai_bot.services.chat import ChatService
    from qq_ai_bot.services.processor import MessageProcessor, ProcessResult

    def catalog(name: str, *, image_input: bool) -> ModelProfileCatalog:
        capabilities = {ModelCapability.REASONING, ModelCapability.TOOLS}
        if image_input:
            capabilities.add(ModelCapability.IMAGE_INPUT)
        profile = ModelProfile(
            id=name,
            provider="fake",
            protocol=ModelProtocol.RESPONSES,
            model=name,
            timeout_seconds=1,
            max_retries=0,
            default_temperature=0,
            default_max_output_tokens=100,
            capabilities=frozenset(capabilities),
        )
        return ModelProfileCatalog(
            profiles={name: profile},
            routes={task: ModelRoute(task=task, profile_id=name) for task in ModelTask},
        )

    executor = TaskModelExecutor(
        router=ModelRouter(catalog("image-model", image_input=True)),
        pool=ModelClientPool(),
    )
    service = AttachmentInputService(
        MediaResolver(),
        ImagePreprocessor(),
        concurrency=1,
        pending_limit=2,
        timeout=2,
        max_bytes=100_000,
        images_enabled=lambda: (
            ModelCapability.IMAGE_INPUT in executor.capabilities(ModelTask.CHAT_AGENT)
        ),
    )
    stream = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(stream, format="PNG")
    message = _message(
        MessageAttachment(
            AttachmentKind.IMAGE,
            "image",
            file="base64://" + base64.b64encode(stream.getvalue()).decode(),
        )
    )
    chat = object.__new__(ChatService)
    chat._models = executor
    processor = object.__new__(MessageProcessor)
    processor._chat = chat
    processor._group_recovery = None
    processor._canonical_ingress = None
    entered = asyncio.Event()
    resume = asyncio.Event()
    observed: list[tuple[str, bool]] = []

    async def admitted(self, inbound, sender, profile_resolver=None, admitted=None):
        entered.set()
        await resume.wait()
        prepared = await service.prepare(inbound, _runtime(), None)
        observed.append((executor.model_name(ModelTask.CHAT_AGENT), bool(prepared.images)))
        return ProcessResult(True, reason="chat")

    processor._handle_admitted = MethodType(admitted, processor)
    try:
        turn = asyncio.create_task(processor.handle(message, object()))
        await asyncio.wait_for(entered.wait(), timeout=2)
        executor.apply_catalog(catalog("text-model", image_input=False), ModelClientPool())
        resume.set()
        assert (await asyncio.wait_for(turn, timeout=2)).reason == "chat"
        assert observed == [("image-model", True)]
        assert executor.model_name(ModelTask.CHAT_AGENT) == "text-model"
        assert not service.images_enabled
    finally:
        resume.set()
        await service._resolver.close()


def _runtime() -> VisionRuntimeConfig:
    return VisionRuntimeConfig(
        max_images_per_turn=4,
        max_frames_per_turn=4,
        gif_max_frames=1,
        thinking_enabled=False,
        thinking_budget=0,
        low_confidence_retry_threshold=0,
        per_user_requests_per_minute=20,
        per_group_requests_per_minute=20,
        analysis_retention_days=1,
    )


def _message(attachment: MessageAttachment) -> InboundMessage:
    return InboundMessage(
        message_id="dynamic-capability",
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity("person"),
        text="看看附件",
        bot_user_id="bot",
        attachments=(attachment,),
    )


@pytest.mark.asyncio
async def test_image_and_video_follow_current_model_capability(monkeypatch):
    enabled = False
    resolver = MediaResolver()
    service = AttachmentInputService(
        resolver,
        ImagePreprocessor(),
        concurrency=1,
        pending_limit=2,
        timeout=2,
        max_bytes=100_000,
        images_enabled=lambda: enabled,
    )
    stream = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(stream, format="PNG")
    image = _message(
        MessageAttachment(
            AttachmentKind.IMAGE,
            "image",
            file="base64://" + base64.b64encode(stream.getvalue()).decode(),
        )
    )
    video = _message(
        MessageAttachment(
            AttachmentKind.VIDEO,
            "video",
            file="base64://" + base64.b64encode(b"\x00\x00\x00\x18ftypisom").decode(),
        )
    )
    disabled_image = await service.prepare(image, _runtime(), None)
    assert not disabled_image.images
    assert "不支持图片" in disabled_image.documents
    with pytest.raises(VisionProcessingError) as denied_video:
        await service.prepare(video, _runtime(), None)
    assert denied_video.value.code == "image_capability_unavailable"

    async def sample_frame(*_args, **_kwargs):
        return (ChatImage(data_url="data:image/jpeg;base64,AA==", video_timestamp_seconds=0),)

    monkeypatch.setattr("qq_ai_bot.services.attachment_inputs.sample_video", sample_frame)
    enabled = True
    assert service.images_enabled
    prepared_image = await service.prepare(
        replace(image, message_id="enabled-image"), _runtime(), None
    )
    assert len(prepared_image.images) == 1
    prepared_video = await service.prepare(
        replace(video, message_id="enabled-video"), _runtime(), None
    )
    assert len(prepared_video.images) == 1
    assert prepared_video.images[0].video_timestamp_seconds == 0

    enabled = False
    assert not service.images_enabled
    with pytest.raises(VisionProcessingError) as denied_again:
        await service.prepare(replace(video, message_id="disabled-video"), _runtime(), None)
    assert denied_again.value.code == "image_capability_unavailable"
    await resolver.close()
