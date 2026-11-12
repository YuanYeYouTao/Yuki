"""The desktop sends picture stickers from any catalog, and reads them as stickers."""

import gzip
import json
from uuid import uuid4

from tests.support.sticker_delivery import DirectoryBridge, settle
from tests.unit.test_app_messages import admin as admin
from tests.unit.test_app_messages import app_env as app_env
from tests.unit.test_app_messages import drain
from tests.unit.test_client_api import bearer, pair, person_and_code
from tests.unit.test_sticker_library import picture, seed_cached_sticker

from yuki.emoji.delivery import EmojiDeliveryService
from yuki.emoji.library import EmojiLibrary
from yuki.gateway.providers import builtin_provider_catalog
from yuki.gateway.providers.app import AppHandle
from yuki.persistence.event_view import attachment_facts
from yuki.persistence.models import ChatEventModel
from yuki.social.service import SocialContext


async def test_desktop_sticker_comes_from_any_catalog_and_reads_as_a_sticker(
    app_env, database, tmp_path
):
    env = app_env
    _, code = await person_and_code(env.client)
    device = await pair(env.client, code)
    response = await env.client.post(
        "/client/v1/messages",
        headers=bearer(device["token"]),
        json={"request_id": str(uuid4()), "text": "发个表情吧"},
    )
    assert response.status_code == 200
    await drain(env)
    sent = response.json()["data"]
    async with database.read_session() as reader:
        event = await reader.get(ChatEventModel, sent["event_id"])
    context = SocialContext(
        str(uuid4()),
        "desktop-sticker",
        sent["conversation_id"],
        person_refs={"current_speaker": event.author_person_id},
        trigger_event_id=event.id,
        reply_presence_id=event.ingress_presence_id,
    )
    workspace = env.social.workspace_service
    workspace.sandbox = DirectoryBridge(tmp_path / "terminal", env.store)
    library = EmojiLibrary(workspace)
    collected = await seed_cached_sticker(
        library, picture("green"), platform="qq", tags=("开心",), is_sticker=True
    )
    # A better-matching animation file is no picture for the desktop.
    await seed_cached_sticker(
        library,
        gzip.compress(json.dumps({"v": "5", "layers": [], "w": 64, "h": 64}).encode()),
        platform="telegram",
        tags=("开心", "高兴"),
        is_sticker=True,
    )
    delivery = EmojiDeliveryService(env.social, library)
    env.social.emoji_delivery = delivery
    await delivery.start()
    try:
        arguments = {"text": "给你一个", "sticker": {"goal": "开心", "emotion": "高兴"}}
        assert (await env.social.execute("send", arguments, context))["status"] == "succeeded"
        illustrated = await settle(delivery, context)
        intent = await delivery.intents.find(context.turn_id, context.call_id)
    finally:
        await delivery.close()
    # 1: the empty desktop catalog does not stop it; the QQ picture is sent.
    assert illustrated["status"] == "succeeded"
    assert intent.source["platform"] == "app"
    assert intent.selected["platform"] == "qq" and intent.selected["id"] == collected.id
    events = await env.client.get(
        f"/client/v1/conversations/{sent['conversation_id']}/events",
        headers=bearer(device["token"]),
    )
    attachments = [
        attachment for item in events.json()["data"]["items"] for attachment in item["attachments"]
    ]
    # 5: the desktop reads it as a sticker.
    assert [attachment.get("sticker") for attachment in attachments] == [True]
    assert attachments[0]["mime_type"] == "image/png"


def test_only_the_desktop_shows_every_catalog_and_plain_pictures_are_no_stickers():
    # 4: an ACP editor shows attachments as file links, not pictures.
    catalog = builtin_provider_catalog()
    capabilities = {
        platform: catalog.describe_connection(
            AppHandle(platform), provider_id=platform
        ).capabilities
        for platform in ("app", "acp")
    }
    assert "shared_stickers" in capabilities["app"]
    assert "shared_stickers" not in capabilities["acp"]
    # 5: an ordinary picture is not a sticker.
    assert "sticker" not in attachment_facts({"mime_type": "image/png", "size": 3})
    assert attachment_facts({"mime_type": "image/gif", "is_sticker": True})["sticker"] is True
