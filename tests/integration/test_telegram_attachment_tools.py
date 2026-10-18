"""Real ToolRuntime and SQLite media tools resolve the original Telegram bot.

Telegram HTTP and environment checkout are local substitutes; no real Bot API,
Docker runtime, model, or platform message is used.
"""

import base64
import hashlib
import io
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from yuki.conversation.media_service import ConversationMediaService
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.gateway.providers import builtin_provider_catalog
from yuki.gateway.providers.telegram import TelegramClient
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.identity.canonical_repository import (
    create_person_binding,
    create_presence,
    create_space_binding,
)
from yuki.persistence.models import ChatEventModel
from yuki.persistence.repositories import EventLedgerRepository
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from yuki.services.agent_tools import ToolRuntime
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.services.media_resolver import MediaResolver
from yuki.workspace.service import WorkspaceService
from yuki.workspace.store import WorkspaceError, WorkspaceStore


@pytest.fixture
async def attachment_tools(database, tmp_path, request):
    image = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(image, "PNG")
    picture = image.getvalue()
    options = getattr(request, "param", {})
    original_bytes = options.get("content", picture)
    media_content = {"original": original_bytes}
    wire = []

    def respond(request):
        wire.append((request.method, request.url.path))
        if request.url.path.endswith("/getFile"):
            file_id = json.loads(request.content)["file_id"]
            assert file_id in {"original-bot-a-file", "actual-sticker-preview"}
            # Official file_id namespace is per bot. A different bot must not be tried.
            if request.url.path != "/botfixture-a/getFile":
                return httpx.Response(400, json={"ok": False, "error_code": 400})
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "file_path": "photos/preview.png"
                        if file_id == "actual-sticker-preview"
                        else "photos/original.png"
                    },
                },
            )
        if request.url.path == "/file/botfixture-a/photos/preview.png":
            return httpx.Response(200, content=picture, headers={"content-type": "image/png"})
        assert request.url.path == "/file/botfixture-a/photos/original.png"
        return httpx.Response(
            200, content=media_content["original"], headers={"content-type": "image/png"}
        )

    registry = GatewayConnectionRegistry(providers=builtin_provider_catalog())
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_a,
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_b,
    ):
        original_client = TelegramClient("fixture-a", client=http_a)
        current_client = TelegramClient("fixture-b", client=http_b)
        original_client.self_id, current_client.self_id = "80001", "80002"
        async with database.immediate_session() as writer:
            await create_space_binding(
                writer,
                external_space_id="-10042",
                name="Group",
                enabled=True,
                autonomous_enabled=True,
                require_mention=False,
                platform="telegram",
                now=datetime.now(UTC),
            )
            for account in ("42", "43"):
                await create_person_binding(
                    writer,
                    external_account_id=account,
                    display_name=account,
                    platform="telegram",
                    now=datetime.now(UTC),
                )
            original_presence = await create_presence(
                writer,
                external_account_id=original_client.self_id,
                platform="telegram",
                now=datetime.now(UTC),
            )
            current_presence = await create_presence(
                writer,
                external_account_id=current_client.self_id,
                platform="telegram",
                now=datetime.now(UTC),
            )
            original_presence_id, current_presence_id = original_presence.id, current_presence.id
        registry.connect(original_client, provider_id="telegram", presence_id=original_presence_id)
        registry.connect(current_client, provider_id="telegram", presence_id=current_presence_id)
        events = ScopedEventLedgerUnitOfWork(database)
        source = (
            await events.append(
                scope=ConversationScope.group(
                    original_client.self_id,
                    "-10042",
                    platform="telegram",
                    presence_id=original_presence_id,
                    thread_id="10",
                ),
                sender_user_id="42",
                platform_message_id="-10042:1",
                direction="inbound",
                content="原接收账号的图片",
                segments=(
                    {
                        "type": options.get("kind", "image"),
                        "data": {"file": "original-bot-a-file", **options.get("data", {})},
                    },
                ),
            )
        ).event
        current = (
            await events.append(
                scope=ConversationScope.group(
                    current_client.self_id,
                    "-10042",
                    platform="telegram",
                    presence_id=current_presence_id,
                    thread_id="10",
                ),
                sender_user_id="43",
                platform_message_id="-10042:2",
                direction="inbound",
                content="请看之前的图片",
            )
        ).event
        assert source.canonical_conversation_id == current.canonical_conversation_id
        assert source.ingress_presence_id != current.ingress_presence_id
        runtime = ToolRuntime(
            inbound=InboundMessage(
                message_id=current.platform_message_id,
                event_type="message:telegram",
                platform="telegram",
                bot_user_id=current_client.self_id,
                scope_type=ScopeType.GROUP,
                group_id="-10042",
                thread_id="10",
                sender=SenderIdentity("43"),
                text=current.content,
                source_event_id=current.id,
                conversation_id=current.canonical_conversation_id,
                presence_id=current.ingress_presence_id,
                person_id=current.author_person_id,
            )
        )
        assert not hasattr(runtime, "gateway")
        # An unrelated default must never receive this source's opaque file_id.
        resolver = MediaResolver(gateway=current_client)
        media = ConversationMediaService(
            database,
            tmp_path / "cache",
            resolver,
            ImagePreprocessor(),
            gateway_registry=registry,
        )
        store = WorkspaceStore(tmp_path / "artifacts")
        workspace = WorkspaceService(store)
        workspace.conversation_media = media
        checkouts = []

        async def checkout(method, args, *, request_id):
            assert method == "workspace_checkout"
            metadata, content = store.read_bytes(args["artifact_id"])
            path = tmp_path / "environment" / metadata["name"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            checkouts.append(path)
            return {"path": "/workspace/" + metadata["name"], "version": metadata["sha256"]}

        workspace.sandbox = SimpleNamespace(execute=checkout)
        try:
            yield SimpleNamespace(
                workspace=workspace,
                media=media,
                store=store,
                registry=registry,
                original_client=original_client,
                source=source,
                runtime=runtime,
                wire=wire,
                original_bytes=original_bytes,
                media_content=media_content,
                checkouts=checkouts,
            )
        finally:
            await media.close()
            await resolver.close()


@pytest.mark.parametrize(
    "tool", ["inspect_conversation_attachment", "save_conversation_attachment_to_workspace"]
)
@pytest.mark.parametrize(
    "cached,original_offline", [(False, False), (True, False), (True, True), (False, True)]
)
async def test_attachment_tools_use_original_presence_and_cached_bytes(
    attachment_tools, tool, cached, original_offline
):
    case = attachment_tools
    if cached:
        await case.media.authorized_path(
            event_id=case.source.id,
            attachment_index=0,
            conversation_id=case.source.canonical_conversation_id,
            generation=None,
        )
        case.wire.clear()
    if original_offline:
        case.registry.disconnect(case.original_client)
    arguments = {
        "event_id": case.source.id,
        "attachment_index": 0,
        "question": "查看原图",
        "destination": "original.png",
    }
    if original_offline and not cached:
        with pytest.raises(WorkspaceError, match="resource_unavailable"):
            await case.workspace.execute(tool, arguments, runtime=case.runtime, request_id=tool)
        assert case.wire == case.checkouts == []
        return
    result = await case.workspace.execute(tool, arguments, runtime=case.runtime, request_id=tool)
    assert case.wire == (
        []
        if cached
        else [
            ("POST", "/botfixture-a/getFile"),
            ("GET", "/file/botfixture-a/photos/original.png"),
        ]
    )
    assert "original-bot-a-file" not in str(result)
    if tool == "inspect_conversation_attachment":
        assert result["event_id"] == case.source.id
        assert result["status"] == "prepared_for_main_agent"
        assert result.images[0].source_event_id == case.source.id
        assert result["sha256"] == hashlib.sha256(case.original_bytes).hexdigest()
    else:
        assert result["file_imported"] is True
        assert case.checkouts[0].read_bytes() == case.original_bytes
        assert case.store.read_bytes(result["artifact_id"])[1] == case.original_bytes


async def test_native_file_selection_uses_resolvers_actual_locator_priority(
    attachment_tools, database
):
    case = attachment_tools
    async with database.immediate_session() as writer:
        event = await writer.get(ChatEventModel, case.source.id)
        segments = json.loads(event.segments_json)
        segments[0]["data"]["url"] = "data:image/ignored-url-field"
        event.segments_json = json.dumps(segments)
    result = await case.workspace.execute(
        "inspect_conversation_attachment",
        {"event_id": case.source.id, "attachment_index": 0, "question": "查看原图"},
        runtime=case.runtime,
        request_id="original-native-priority",
    )
    assert result.images and case.wire == [
        ("POST", "/botfixture-a/getFile"),
        ("GET", "/file/botfixture-a/photos/original.png"),
    ]


@pytest.mark.parametrize("native", [False, True])
async def test_attachment_bridge_keeps_qq_adapter_and_inline_without_connection(
    database, tmp_path, native
):
    image = io.BytesIO()
    Image.new("RGB", (2, 2), "blue").save(image, "PNG")
    content = image.getvalue()
    encoded = base64.b64encode(content).decode()
    source, _ = await EventLedgerRepository(database).append(
        bot_user_id="8000",
        platform_message_id="qq-original-image",
        scope_type=ScopeType.GROUP,
        group_id="2001",
        sender_user_id="1001",
        direction="inbound",
        content="原图",
        segments=(
            {"type": "image", "data": {"file": "qq-original" if native else "base64://" + encoded}},
        ),
    )
    registry = GatewayConnectionRegistry(providers=builtin_provider_catalog())
    raw = SimpleNamespace(self_id="8000", call_api=AsyncMock(return_value={"base64": encoded}))
    if native:
        registry.connect(raw, provider_id="snowluma", presence_id=source.ingress_presence_id)
    resolver = MediaResolver()
    media = ConversationMediaService(
        database, tmp_path / "cache", resolver, ImagePreprocessor(), gateway_registry=registry
    )
    workspace = WorkspaceService(WorkspaceStore(tmp_path / "artifacts"))
    workspace.conversation_media = media
    try:
        result = await workspace.execute(
            "inspect_conversation_attachment",
            {"event_id": source.id, "attachment_index": 0, "question": "查看原图"},
            runtime=ToolRuntime(inbound=None, conversation_id=source.canonical_conversation_id),
        )
        assert result.images and result["sha256"] == hashlib.sha256(content).hexdigest()
        if native:
            raw.call_api.assert_awaited_once_with("get_image", file="qq-original")
        else:
            raw.call_api.assert_not_awaited()
            assert not registry.has_any_active()
    finally:
        await media.close()
        await resolver.close()
