"""The media object belongs to an internal event and one live generation."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from PIL import Image
from tests.unit.test_canonical_ingress import _Bot, _message, _stack

from qq_ai_bot.conversation.media_service import ConversationMediaError, ConversationMediaService
from qq_ai_bot.identity.canonical_repository import ensure_presence
from qq_ai_bot.operations.reset_conversations import reset_all
from qq_ai_bot.persistence.models import ConversationMediaItemModel
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
from qq_ai_bot.vision.models import DownloadedMedia, VisualObservation
from qq_ai_bot.workspace.service import WorkspaceService
from qq_ai_bot.workspace.store import WorkspaceStore


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, "PNG")
    return output.getvalue()


class _Resolver:
    def __init__(self) -> None:
        self.payload = _png()
        self.calls = 0

    async def resolve(self, _reference, _gateway):
        self.calls += 1
        return DownloadedMedia(
            content=self.payload,
            content_type="image/png",
            content_hash=hashlib.sha256(self.payload).hexdigest(),
            byte_size=len(self.payload),
        )

    async def download_attachment(self, _reference, destination, *, max_download_bytes):
        assert len(self.payload) <= max_download_bytes
        destination.write_bytes(self.payload)


class _Provider:
    async def analyze(self, inputs, question):
        assert inputs and question
        return VisualObservation(items=(), overall_description="红色方块")


@pytest.mark.asyncio
async def test_media_index_cache_scope_expiry_and_reset(database, tmp_path):
    registry, resolver, uow = await _stack(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)

    message = replace(
        _message(message_id="media-1", user_id="1001"),
        text="看看这个 [图片附件0]",
        segments=(
            {"type": "text", "data": {"text": "看看这个"}},
            {"type": "image", "data": {"file": "opaque-image-id"}},
        ),
    )
    admitted = await resolver.pre_admit(bot, message)
    assert admitted is not None and admitted.conversation_id
    appended = await uow.append_inbound(admitted.message, admitted)
    async with database.sessions() as session:
        item = await session.get(ConversationMediaItemModel, (appended.event.id, 0))
        assert item is not None
        assert item.segment_index == 1
        assert item.conversation_id == admitted.conversation_id

    other = await resolver.pre_admit(bot, _message(message_id="other-1", user_id="1002"))
    assert other is not None and other.conversation_id
    await uow.append_inbound(other.message, other)

    source = _Resolver()
    service = ConversationMediaService(
        database, tmp_path / "media", source, ImagePreprocessor(), _Provider()
    )
    item, path = await service.authorized_path(
        event_id=appended.event.id,
        attachment_index=0,
        conversation_id=admitted.conversation_id,
        generation=1,
        gateway=None,
    )
    assert path.read_bytes() == source.payload
    assert path.parent.parent.name == admitted.conversation_id
    observation = await service.inspect(item, path, "这是什么颜色？")
    assert observation["observation"]["overall_description"] == "红色方块"
    assert observation["event_id"] == appended.event.id
    assert source.calls == 1

    workspace = WorkspaceService(WorkspaceStore(tmp_path / "workspace"))
    workspace.conversation_media = service
    runtime = ToolRuntime(inbound=admitted.message, gateway=None, allow_generic_onebot=False)
    assert runtime.conversation_id is None
    assert runtime.effective_conversation_id == admitted.conversation_id
    inspected = await workspace.execute(
        "inspect_conversation_attachment",
        {"event_id": appended.event.id, "attachment_index": 0, "question": "这是什么颜色？"},
        runtime=runtime,
    )
    assert inspected["event_id"] == appended.event.id
    assert inspected["observation"]["overall_description"] == "红色方块"
    assert source.calls == 1

    with pytest.raises(ConversationMediaError, match="attachment_scope_denied"):
        await service.authorized_path(
            event_id=appended.event.id,
            attachment_index=0,
            conversation_id=other.conversation_id,
            generation=1,
            gateway=None,
        )

    preview = await reset_all(database, "media-cutover-test", apply=False)
    assert preview["total"] >= 2
    first = await reset_all(database, "media-cutover-test", apply=True)
    second = await reset_all(database, "media-cutover-test", apply=True)
    assert first["newly_reset"] == first["total"]
    assert second["newly_reset"] == 0
    with pytest.raises(ConversationMediaError, match="attachment_scope_denied"):
        await service.authorized_path(
            event_id=appended.event.id,
            attachment_index=0,
            conversation_id=admitted.conversation_id,
            generation=2,
            gateway=None,
        )

    async with database.sessions() as session, session.begin():
        stored = await session.get(ConversationMediaItemModel, (appended.event.id, 0))
        assert stored is not None
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await service.cleanup()
    assert not path.exists()
    async with database.sessions() as session:
        stored = await session.get(ConversationMediaItemModel, (appended.event.id, 0))
        assert stored is not None and stored.cache_status == "expired"
    assert await PeopleRepository(database).delete_person("1001") is True
    async with database.sessions() as session:
        assert await session.get(ConversationMediaItemModel, (appended.event.id, 0)) is None


@pytest.mark.asyncio
async def test_file_with_image_bytes_uses_visual_reader(database, tmp_path):
    registry, resolver, uow = await _stack(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    message = replace(
        _message(message_id="file-image-1", user_id="1001"),
        text="这份文件 [文件附件0：图.png]",
        segments=({"type": "file", "data": {"name": "图.png", "url": "https://example.test/f"}},),
    )
    admitted = await resolver.pre_admit(bot, message)
    assert admitted is not None
    appended = await uow.append_inbound(admitted.message, admitted)
    service = ConversationMediaService(
        database, tmp_path / "media", _Resolver(), ImagePreprocessor(), _Provider()
    )
    item, path = await service.authorized_path(
        event_id=appended.event.id,
        attachment_index=0,
        conversation_id=admitted.conversation_id,
        generation=1,
        gateway=None,
    )
    assert path.suffix == ".bin"
    result = await service.inspect(item, path, "图里是什么？")
    assert result["mode"] == "image"
    assert result["observation"]["overall_description"] == "红色方块"


@pytest.mark.asyncio
async def test_forgetting_person_removes_index_and_cached_bytes(database, tmp_path):
    registry, resolver, uow = await _stack(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    message = replace(
        _message(message_id="forget-media-1", user_id="1001"),
        segments=({"type": "image", "data": {"file": "opaque-image-id"}},),
    )
    admitted = await resolver.pre_admit(bot, message)
    assert admitted is not None
    appended = await uow.append_inbound(admitted.message, admitted)
    service = ConversationMediaService(
        database, tmp_path / "media", _Resolver(), ImagePreprocessor(), _Provider()
    )
    _, path = await service.authorized_path(
        event_id=appended.event.id,
        attachment_index=0,
        conversation_id=admitted.conversation_id,
        generation=1,
        gateway=None,
    )
    assert path.is_file()
    assert await PeopleRepository(database).delete_person("1001") is True
    await service.cleanup()
    assert not path.exists()
    async with database.sessions() as session:
        assert await session.get(ConversationMediaItemModel, (appended.event.id, 0)) is None
