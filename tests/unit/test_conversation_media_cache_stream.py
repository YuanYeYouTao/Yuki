"""Cold cache admission hashes bounded chunks without loading attachment blobs."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from tests.unit.test_canonical_ingress import _Bot, _message, _stack
from tests.unit.test_conversation_media import _png

from qq_ai_bot.conversation import media_service
from qq_ai_bot.conversation.media_service import ConversationMediaError, ConversationMediaService
from qq_ai_bot.identity.canonical_repository import ensure_presence
from qq_ai_bot.persistence.models import ConversationMediaItemModel
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
from qq_ai_bot.vision.models import DownloadedMedia


async def _incoming_attachment(database, kind):
    registry, resolver, uow = await _stack(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    message = replace(
        _message(message_id="cold-attachment", user_id="1001"),
        segments=({"type": kind, "data": {"file": "opaque", "url": "https://example.test/f"}},),
    )
    admitted = await resolver.pre_admit(bot, message)
    assert admitted is not None and admitted.conversation_id
    appended = await uow.append_inbound(admitted.message, admitted)
    return admitted.conversation_id, appended.event.id


class _Downloader:
    def __init__(self, payload):
        self.payload, self.calls = payload, 0

    async def resolve(self, _reference, _gateway):
        self.calls += 1
        return DownloadedMedia(
            content=self.payload,
            content_type="image/png",
            content_hash=hashlib.sha256(self.payload).hexdigest(),
            byte_size=len(self.payload),
        )

    async def download_attachment(self, _reference, destination, *, max_download_bytes):
        self.calls += 1
        # Deliberately ignore the network cap: cache admission must still enforce it.
        destination.write_bytes(self.payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "payload", "extension"),
    [
        ("image", _png() + b"x" * 150_000, ".png"),
        ("video", b"\x00\x00\x00\x18ftypmp42" + b"v" * 150_000, ".mp4"),
        ("file", b"document" * 20_000, ".bin"),
    ],
    ids=["image", "video", "file"],
)
async def test_cold_cache_never_reads_whole_file_and_preserves_source_authority(
    database, tmp_path, monkeypatch, kind, payload, extension
):
    conversation_id, event_id = await _incoming_attachment(database, kind)
    downloader = _Downloader(payload)
    service = ConversationMediaService(
        database, tmp_path / "media", downloader, ImagePreprocessor()
    )

    def reject_unbounded_read(_path):
        raise AssertionError("cache admission must not call Path.read_bytes")

    monkeypatch.setattr(Path, "read_bytes", reject_unbounded_read)
    _, path = await service.authorized_path(
        event_id=event_id,
        attachment_index=0,
        conversation_id=conversation_id,
        generation=1,
        gateway=None,
    )
    digest = hashlib.sha256(payload).hexdigest()
    assert path.name == f"0-{digest}{extension}"
    with path.open("rb") as stream:
        assert stream.read() == payload
    async with database.sessions() as session:
        stored = await session.get(ConversationMediaItemModel, (event_id, 0))
        assert stored is not None and stored.cache_status == "cached"
        assert stored.content_sha256 == digest and stored.cache_name == path.name
    await service.authorized_path(
        event_id=event_id,
        attachment_index=0,
        conversation_id=conversation_id,
        generation=1,
        gateway=None,
    )
    assert downloader.calls == 1
    with pytest.raises(ConversationMediaError, match="attachment_scope_denied"):
        await service.authorized_path(
            event_id=event_id,
            attachment_index=0,
            conversation_id=conversation_id,
            generation=2,
            gateway=None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"", b"x" * 101], ids=["empty", "oversize"])
async def test_cold_cache_rejects_empty_or_oversize_download_without_publication(
    database, tmp_path, monkeypatch, payload
):
    conversation_id, event_id = await _incoming_attachment(database, "file")
    monkeypatch.setattr(media_service, "_MAX_FILE", 100)
    service = ConversationMediaService(
        database, tmp_path / "media", _Downloader(payload), ImagePreprocessor()
    )
    with pytest.raises(ConversationMediaError, match="attachment_invalid"):
        await service.authorized_path(
            event_id=event_id,
            attachment_index=0,
            conversation_id=conversation_id,
            generation=1,
            gateway=None,
        )
    assert not list(service.root.rglob("*.part"))
    assert not list(service.root.rglob("*.bin"))
    async with database.sessions() as session:
        stored = await session.get(ConversationMediaItemModel, (event_id, 0))
        assert stored is not None and stored.cache_status != "cached"
        assert stored.content_sha256 is None


def test_cache_hash_uses_bounded_reads_and_rechecks_file_before_publication(tmp_path, monkeypatch):
    path = tmp_path / "download.part"
    payload = b"z" * 150_000
    path.write_bytes(payload)
    original_fdopen = media_service.os.fdopen
    reads = []

    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            assert 0 < size <= 64 * 1024
            reads.append(size)
            return self.stream.read(size)

    monkeypatch.setattr(
        media_service.os, "fdopen", lambda *args: BoundedReader(original_fdopen(*args))
    )
    facts = media_service._cache_file_facts(path, 200_000)
    assert len(reads) >= 3
    assert facts.head == payload[:12]
    assert facts.byte_size == len(payload)
    assert facts.digest == hashlib.sha256(payload).hexdigest()
    path.write_bytes(b"changed")
    with pytest.raises(ConversationMediaError, match="attachment_changed"):
        media_service._publish_cache_file(path, tmp_path / "final.bin", facts)
    assert not (tmp_path / "final.bin").exists()
