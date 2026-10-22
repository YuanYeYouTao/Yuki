"""The original Telegram body streams once, including concurrent explicit readers."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import update
from tests.integration.test_telegram_attachment_tools import attachment_tools as attachment_tools
from tests.support.workspace_snapshots import artifact_ids

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.media_service import ConversationMediaError
from yuki.persistence.models import ConversationMediaItemModel


class OriginalBody(httpx.AsyncByteStream):
    def __init__(self, head, size, *, pause=False):
        self.head, self.size = head, size
        self.pause = pause
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        yield self.head
        self.entered.set()
        if self.pause:
            await self.release.wait()
        remaining = self.size - len(self.head)
        while remaining:
            chunk = b"x" * min(65536, remaining)
            remaining -= len(chunk)
            yield chunk

    async def aclose(self):
        self.closed = True


def serve_original(case, monkeypatch, body):
    requests = []

    def respond(request):
        requests.append(request.url.path.rsplit("/", 1)[-1])
        if request.url.path.endswith("/getFile"):
            assert json.loads(request.content) == {"file_id": "original-bot-a-file"}
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": "original", "file_size": body.size}}
            )
        return httpx.Response(200, stream=body)

    monkeypatch.setattr(case.original_client._client, "_transport", httpx.MockTransport(respond))
    return requests


async def test_prefetch_and_immediate_inspect_save_share_the_actual_stream(
    attachment_tools, monkeypatch
):
    case = attachment_tools
    body = OriginalBody(case.original_bytes, 11 * 1024 * 1024, pause=True)
    requests = serve_original(case, monkeypatch, body)
    original_cache = case.media._cache
    callers, readers_entered = 0, asyncio.Event()

    async def cache(*args, **kwargs):
        nonlocal callers
        callers += 1
        if callers == 3:
            readers_entered.set()
        return await original_cache(*args, **kwargs)

    monkeypatch.setattr(case.media, "_cache", cache)
    prefetch = asyncio.create_task(case.media._cache_event(case.source.id, None))
    await asyncio.wait_for(body.entered.wait(), 5)
    inspect = asyncio.create_task(
        case.workspace.execute(
            "inspect_conversation_attachment",
            {"event_id": case.source.id, "attachment_index": 0, "question": "查看选定原件"},
            runtime=case.runtime,
        )
    )
    save = asyncio.create_task(
        case.workspace.execute(
            "save_conversation_attachment_to_workspace",
            {"event_id": case.source.id, "attachment_index": 0, "destination": "saved.png"},
            runtime=case.runtime,
            request_id="original-stream-save",
        )
    )
    try:
        await asyncio.wait_for(readers_entered.wait(), 5)
        assert not inspect.done() and not save.done()
        body.release.set()
        _, inspected, saved = await asyncio.gather(prefetch, inspect, save)
        assert inspected.images and saved["version"] == inspected["sha256"]
        assert saved["size"] == body.size and artifact_ids(case.store) == []
        assert requests == ["getFile", "original"] and body.closed
        item, path = await case.media.authorized_path(event_id=case.source.id, attachment_index=0)
        assert path.stat().st_size == body.size
        first_expiry = item.expires_at
        again, _ = await case.media.authorized_path(event_id=case.source.id, attachment_index=0)
        assert again.expires_at == first_expiry
    finally:
        body.release.set()
        await asyncio.gather(prefetch, inspect, save, return_exceptions=True)


@pytest.mark.parametrize(
    "attachment_tools",
    [{"kind": "file", "data": {"name": "large.bin", "file_size": 201 * 1024 * 1024}}],
    indirect=True,
)
async def test_large_original_streams_past_old_cap_without_rebuilding_context(
    attachment_tools, monkeypatch
):
    case = attachment_tools
    body = OriginalBody(b"original file\n", 201 * 1024 * 1024)
    requests = serve_original(case, monkeypatch, body)
    async with case.media.database.immediate_session() as writer:
        await writer.execute(
            update(CanonicalConversationModel)
            .where(CanonicalConversationModel.id == case.source.canonical_conversation_id)
            .values(generation=9, starts_after_event_id=case.source.id)
        )
    await case.media._cache_event(case.source.id, None)
    item, path = await case.media.authorized_path(event_id=case.source.id, attachment_index=0)
    assert path.stat().st_size == body.size and requests == ["getFile", "original"]
    with path.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == item.content_sha256
    # Expiration is owned physical cleanup; a still-present original remains readable.
    async with case.media.database.immediate_session() as writer:
        await writer.execute(
            update(ConversationMediaItemModel)
            .where(ConversationMediaItemModel.source_event_id == case.source.id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert (await case.media.authorized_path(event_id=case.source.id, attachment_index=0))[
        1
    ] == path
    await case.media.cleanup()
    async with asyncio.timeout(5):
        while path.exists():
            await case.media.cleanup()
    with pytest.raises(ConversationMediaError, match="attachment_expired"):
        await case.media.authorized_path(event_id=case.source.id, attachment_index=0)
    assert requests == ["getFile", "original"] and body.closed
