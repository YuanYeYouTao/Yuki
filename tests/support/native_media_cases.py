"""Native Telegram admission and local original media, without network traffic."""

import asyncio
import gzip
import io
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from sqlalchemy import select
from tests.conftest import build_harness, make_settings

from yuki.conversation.media_service import ConversationMediaService
from yuki.gateway.media import PlatformMediaFile
from yuki.gateway.provider import GatewayProviderCatalog
from yuki.gateway.providers.telegram import TelegramClient, TelegramProvider
from yuki.gateway.providers.telegram_ingress import TelegramGateway
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.identity.canonical_repository import create_presence
from yuki.identity.canonical_uow import CanonicalIngressUnitOfWork
from yuki.identity.ingress import CanonicalIngressResolver
from yuki.identity.routing import PresenceRouter
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import ChatEventModel
from yuki.persistence.repository_helpers import _event_record
from yuki.services.group_recovery import NativeGroupRecovery
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.services.main_agent_contract import MainAgentContract
from yuki.services.media_resolver import MediaResolver
from yuki.social.service import SocialService
from yuki.workspace.short_state import ShortState
from yuki.workspace.store import WorkspaceStore


def telegram_update(identifier=1, *, text="", edit=False):
    message = {
        "message_id": 1 if edit else identifier,
        "date": 1792070000,
        "from": {"id": 42, "first_name": "P", "is_bot": False},
        "chat": {"id": 42, "type": "private"},
        "text": text,
    }
    if edit:
        message["edit_date"] = 1792070010
    return {"update_id": identifier, "edited_message" if edit else "message": message}


def add_photo(message, file="old-photo"):
    message["photo"] = [{"file_id": file, "file_unique_id": file, "width": 8, "height": 8}]


def add_sticker(message, file="old-sticker"):
    message["sticker"] = {
        "file_id": file,
        "is_animated": True,
        "width": 64,
        "height": 64,
        "emoji": "开心",
    }


async def source_record(database, platform_message_id="42:1"):
    async with database.sessions() as reader:
        row = await reader.scalar(
            select(ChatEventModel).where(
                ChatEventModel.platform_message_id == platform_message_id,
                ChatEventModel.direction == "inbound",
            )
        )
        assert row is not None
        return _event_record(row)


async def drain_native_turns(env):
    if env.gateway._turns:
        await asyncio.wait_for(asyncio.gather(*tuple(env.gateway._turns)), 10)


@pytest.fixture
async def native_media_env(database, tmp_path):
    def png(color):
        stream = io.BytesIO()
        Image.new("RGB", (8, 8), color).save(stream, "PNG")
        return stream.getvalue()

    contents = {
        "old-photo": png("red"),
        "new-photo": png("blue"),
        "old-sticker": gzip.compress(
            json.dumps({"v": "5.7", "layers": [], "w": 64, "h": 64}).encode()
        ),
    }
    downloads, wire = [], []

    class MediaGateway:
        async def resolve_media_file(self, file, kind, *, max_download_bytes):
            downloads.append(file)
            return PlatformMediaFile(
                content=contents[file],
                content_type="application/x-tgsticker" if file.endswith("sticker") else "image/png",
            )

    media_gateway = MediaGateway()

    class MediaProvider(TelegramProvider):
        def media(self, handle):
            return media_gateway

    def http(request):
        method = request.url.path.rsplit("/", 1)[-1]
        params = json.loads(request.content)
        wire.append((method, params))
        if method == "getMe":
            result = {"id": 80001, "is_bot": True, "username": "yuki_test"}
        elif method == "getChatMember":
            result = {"status": "administrator", "user": {"id": params["user_id"]}}
        elif method == "sendChatAction":
            result = True
        elif method == "sendMessage":
            result = {"message_id": 1000 + len(wire), "chat": {"id": params["chat_id"]}}
        else:
            raise AssertionError(f"Unexpected native transport call: {method}")
        return httpx.Response(200, json={"ok": True, "result": result})

    model = FakeLLMProvider(lambda request: "NO_REPLY")
    harness = build_harness(database, make_settings(database.url), model)
    registry = GatewayConnectionRegistry(providers=GatewayProviderCatalog((MediaProvider(),)))
    router = PresenceRouter(database, registry)
    harness.processor._canonical_ingress = CanonicalIngressResolver(database, registry, router)
    harness.processor._canonical_uow = CanonicalIngressUnitOfWork(database, router)
    harness.processor._group_recovery = NativeGroupRecovery(
        harness.settings, database, registry, router
    )
    harness.processor._chat._tools.social_service = SocialService(
        database, router, harness.scoped_events
    )
    store = WorkspaceStore(tmp_path / "main-workspace")
    harness.processor._chat.runtime.runner.main_contract = MainAgentContract(
        harness.processor._chat, ShortState(store)
    )
    resolver = MediaResolver()
    media = ConversationMediaService(
        database, tmp_path / "cache", resolver, ImagePreprocessor(), gateway_registry=registry
    )
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as transport:
            client = TelegramClient("offline-test-token", client=transport)
            await client.get_me()
            async with database.immediate_session() as session:
                presence = await create_presence(
                    session,
                    external_account_id=client.self_id,
                    platform="telegram",
                    now=datetime.now(UTC),
                )
            registry.connect(client, provider_id="telegram", presence_id=presence.id)
            gateway = TelegramGateway(
                database, registry, router, harness.processor, harness.scoped_events, client
            )
            gateway.presence_id = presence.id
            try:
                yield SimpleNamespace(
                    gateway=gateway,
                    harness=harness,
                    media=media,
                    media_gateway=media_gateway,
                    contents=contents,
                    downloads=downloads,
                    model=model,
                    wire=wire,
                )
            finally:
                await gateway.close()
    finally:
        await media.close()
        await resolver.close()
