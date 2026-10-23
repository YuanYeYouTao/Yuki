"""Native ingress exposes source attachment facts without reading file contents."""

import json

import pytest
from sqlalchemy import select
from tests.integration import test_telegram_runtime as native
from tests.integration.test_telegram_runtime import telegram_env  # noqa: F401
from tests.support.service_dependencies import workspace_service

from yuki.persistence.models import ChatEventModel, ConversationMediaItemModel


@pytest.mark.parametrize("mode", ["empty", "caption", "reply"])
@pytest.mark.parametrize("field", ["document", "photo", "video", "sticker"])
async def test_native_telegram_file_index_survives_current_input_composition(
    telegram_env,  # noqa: F811
    database,
    mode,
    field,
):
    env = telegram_env
    env.model._responder = lambda _: "NO_REPLY"
    payload = native.update(text="")
    original = payload["message"]
    original.pop("text")
    media = {
        "file_id": "private-platform-file-id",
        "file_name": "actual-report.csv" if field == "document" else "actual-media.bin",
        "mime_type": "text/csv" if field == "document" else "video/webm",
        "file_size": 23,
        "is_animated": field == "sticker",
        "width": 32,
        "height": 32,
    }
    original[field] = [media] if field == "photo" else media
    if mode == "caption":
        original["caption"] = "先聊一下今天的午饭"
    await env.gateway.receive(payload)
    await native.drain(env)
    if mode == "reply":
        env.model.requests.clear()
        reply = native.update(2, text="这是引用的附件")
        reply["message"]["reply_to_message"] = original
        await env.gateway.receive(reply)
        await native.drain(env)

    assert len(env.model.requests) == 1
    request = env.model.requests[0]
    assert not any(message.images for message in request.messages)
    contents = [message.content or "" for message in request.messages if message.role != "system"]
    material = "\n".join(contents)
    assert media["file_name"] in material
    assert "private-platform-file-id" not in material
    # The original paid input can remain in the private checkpoint unchanged.
    current = next(content for content in reversed(contents) if "[本轮附件索引]\n" in content)
    indices, _ = json.JSONDecoder().raw_decode(current.split("[本轮附件索引]\n", 1)[1])
    assert len(indices) == 1
    async with database.read_session() as reader:
        source = await reader.scalar(select(ChatEventModel).order_by(ChatEventModel.id))
        indexed = await reader.get(ConversationMediaItemModel, (source.id, 0))
    source_segment = next(
        segment for segment in json.loads(source.segments_json) if segment["type"] == indexed.kind
    )
    data = source_segment["data"]
    assert indices[0] == {
        "source": "reply" if mode == "reply" else "current",
        "event_id": source.id,
        "attachment_index": indexed.attachment_index,
        "kind": indexed.kind,
        "filename": media["file_name"],
        "mime_type": data["mime_type"],
        "file_size": 23,
        "is_sticker": field == "sticker",
        "cache_status": "uncached",
        "cache_directory": (
            f"/inputs/media-cache/v1/{source.canonical_conversation_id}/{source.id}/"
        ),
    }
    assert "本轮附件索引" not in source.content
    if mode == "caption":
        assert material.index("今天的午饭") < material.index("[本轮附件索引]")


@pytest.mark.parametrize("prefetch_first", [False, True])
async def test_unknown_reply_is_observed_with_current_event_without_inventing_history(
    telegram_env,  # noqa: F811
    database,
    tmp_path,
    monkeypatch,
    prefetch_first,
):
    import asyncio

    import httpx

    from yuki.conversation.media_service import ConversationMediaService
    from yuki.services.image_preprocessor import ImagePreprocessor
    from yuki.services.media_resolver import MediaResolver
    from yuki.workspace.store import WorkspaceStore

    env = telegram_env
    env.model._responder = lambda _: "NO_REPLY"
    original_http = env.gateway.client._client
    downloads = []
    originals = {
        "current-native-id": b"CURRENT original file",
        "observed-native-id": b"OBSERVED reply original file",
        "new-observed-id": b"NEW observed reply original file",
    }

    async def transport(request):
        if request.url.path.endswith("/getFile"):
            file_id = json.loads(request.content)["file_id"]
            return httpx.Response(200, json={"ok": True, "result": {"file_path": file_id}})
        if "/file/" in request.url.path:
            file_id = request.url.path.rsplit("/", 1)[-1]
            downloads.append(file_id)
            return httpx.Response(200, content=originals[file_id])
        return await original_http.send(request)

    resolver = MediaResolver()
    service = ConversationMediaService(
        database,
        tmp_path / "cache",
        resolver,
        ImagePreprocessor(),
        gateway_registry=env.gateway.registry,
    )
    workspace = workspace_service(WorkspaceStore(tmp_path / "artifacts"))
    workspace.conversation_media = service
    env.harness.processor._conversation_media = service
    release = asyncio.Event()
    cache_event = service._cache_event

    async def delayed_prefetch(*args):
        if not prefetch_first:
            await release.wait()
        return await cache_event(*args)

    monkeypatch.setattr(service, "_cache_event", delayed_prefetch)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as media_http:
        monkeypatch.setattr(env.gateway.client, "_client", media_http)
        try:
            payload = native.update(text="查看引用里这份文件")
            parent = payload["message"]
            parent["document"] = {"file_id": "current-native-id", "file_name": "current.txt"}
            reply = native.update(999, text="这段原文此前没有入账")["message"]
            reply["document"] = {"file_id": "observed-native-id", "file_name": "observed.txt"}
            parent["reply_to_message"] = reply
            await env.gateway.receive(payload)
            await native.drain(env)
            async with database.read_session() as reader:
                events = list(await reader.scalars(select(ChatEventModel)))
                sources = list(
                    await reader.scalars(
                        select(ConversationMediaItemModel).order_by(
                            ConversationMediaItemModel.attachment_index
                        )
                    )
                )
            assert len(events) == 1 and events[0].reply_to_event_id is None
            assert len(sources) == 2 and [item.attachment_index for item in sources] == [0, 1]
            assert all(item.source_event_id == events[0].id for item in sources)
            assert sources[0].source_snapshot_json is None
            snapshot = json.loads(sources[1].source_snapshot_json)
            assert snapshot["type"] == "file" and snapshot["data"]["file"] == "observed-native-id"
            assert "此前没有入账" not in sources[1].source_snapshot_json
            material = "\n".join(
                message.content or "" for message in env.model.requests[0].messages
            )
            current = material.rsplit("[本轮附件索引]\n", 1)[1]
            indices, _ = json.JSONDecoder().raw_decode(current)
            assert indices[1]["event_id"] == events[0].id
            assert indices[1]["origin"] == "reply_observed" and indices[1]["attachment_index"] == 1
            assert "observed-native-id" not in material
            assert not any(message.images for message in env.model.requests[0].messages)
            if prefetch_first:
                await asyncio.gather(*tuple(service._pending))
            else:
                assert not downloads  # Main reached its first request while prefetch was blocked.
            result = await workspace.execute(
                "inspect_conversation_attachment",
                {"event_id": events[0].id, "attachment_index": 1, "question": "读取引用文件"},
                request_id="explicit-observed-reply",
            )
            assert "OBSERVED reply original file" in result["result"]["text"]
            release.set()
            await asyncio.gather(*tuple(service._pending))
            again = await workspace.execute(
                "inspect_conversation_attachment",
                {"event_id": events[0].id, "attachment_index": 1, "question": "再次读取引用文件"},
                request_id="explicit-observed-reply-again",
            )
            assert again["sha256"] == result["sha256"]
            assert downloads.count("observed-native-id") == 1
            await env.gateway.receive(payload)
            await native.drain(env)
            assert len(env.model.requests) == 1
            # An authenticated edit removes current slots and replaces the
            # observed source without manufacturing its unknown original event.
            edited = native.update(3, text=parent["text"], edit=True)
            edited["edited_message"]["reply_to_message"] = {
                **reply,
                "document": {"file_id": "new-observed-id", "file_name": "new.txt"},
            }
            await env.gateway.receive(edited)
            async with database.read_session() as reader:
                events = list(await reader.scalars(select(ChatEventModel)))
                sources = list(await reader.scalars(select(ConversationMediaItemModel)))
            assert len(events) == 1 and events[0].reply_to_event_id is None
            assert len(sources) == 1 and sources[0].attachment_index == 0
            assert json.loads(sources[0].source_snapshot_json)["data"]["file"] == "new-observed-id"
            assert sources[0].cache_name is None and sources[0].cache_status == "uncached"
            selected = await workspace.execute(
                "inspect_conversation_attachment",
                {"event_id": events[0].id, "attachment_index": 0, "question": "读取新的引用原件"},
                request_id="explicit-edited-observed-reply",
            )
            assert "NEW observed reply original file" in selected["result"]["text"]
            assert downloads.count("new-observed-id") == 1 and len(env.model.requests) == 1
        finally:
            release.set()
            await service.close()
            await resolver.close()


def test_original_audio_and_observed_reply_keep_real_attachment_slots():
    from yuki.conversation.media_index import prepare_event_media

    current = (
        {"type": "face", "data": {"id": "1"}},
        {"type": "record", "data": {"file": "voice"}},
        {"type": "card", "data": {"id": "card"}},
        {"type": "image", "data": {"file": "photo"}},
        {"type": "yuki_context", "data": {"platform": "qq"}},
    )
    reply = ({"type": "file", "data": {"file": "observed"}},)
    rows = prepare_event_media(
        event_id=7,
        conversation_id="real-conversation",
        generation=1,
        segments=current,
        reply_segments=reply,
    )
    assert [(row.attachment_index, row.kind) for row in rows] == [
        (0, "record"),
        (2, "image"),
        (3, "file"),
    ]
    assert rows[0].source_snapshot_json is None and rows[1].source_snapshot_json is None
    assert json.loads(rows[2].source_snapshot_json) == reply[0]
