"""Only actual Feishu content becomes chat; image receipt doesn't adopt stickers."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from tests.integration.test_feishu_runtime import drain, update
from tests.integration.test_feishu_runtime import feishu_env as feishu_env
from tests.support.service_dependencies import workspace_service

from yuki.domain.messages import AttachmentKind, OutboundMessage
from yuki.emoji.library import EmojiLibrary
from yuki.gateway.providers.feishu_ingress import FeishuSender, normalize_message
from yuki.persistence.models import ChatEventModel
from yuki.workspace.store import WorkspaceStore


def client(app_id="cli_test", platform="feishu"):
    return SimpleNamespace(app_id=app_id, platform=platform, self_id=f"{app_id}:ou_yuki")


@pytest.mark.parametrize(
    "event_type",
    [
        "im.message.message_read_v1",
        "im.message.recalled_v1",
        "card.action.trigger",
        "im.chat.member.user.added_v1",
    ],
)
async def test_service_events_do_not_create_chat_or_purchase_model(
    feishu_env, database, event_type
):
    env = feishu_env
    incoming = update()
    incoming["header"]["event_type"] = event_type
    await env.gateway.receive(incoming)
    await drain(env)
    assert not env.model.requests
    async with database.read_session() as session:
        assert await session.scalar(select(func.count()).select_from(ChatEventModel)) == 0


@pytest.mark.parametrize(
    ("message_type", "content", "kind"),
    [
        ("image", {"image_key": "img_original"}, AttachmentKind.IMAGE),
        ("file", {"file_key": "file_original", "file_name": "notes.txt"}, AttachmentKind.FILE),
        ("audio", {"file_key": "file_voice"}, AttachmentKind.AUDIO),
        ("media", {"file_key": "file_video"}, AttachmentKind.VIDEO),
    ],
)
def test_media_uses_original_message_resource_locator(message_type, content, kind):
    incoming = update(message_type=message_type)
    incoming["event"]["message"]["content"] = json.dumps(content)
    message = normalize_message(incoming["event"], client())
    assert message is not None and not message.text
    attachment = message.attachments[0]
    assert attachment.kind is kind and not attachment.is_sticker
    assert json.loads(attachment.file) == {
        "message_id": "om_1",
        "resource_key": content.get("image_key") or content["file_key"],
        "type": "image" if kind is AttachmentKind.IMAGE else "file",
    }


@pytest.mark.parametrize("message_type", ["image", "sticker"])
async def test_inbound_image_and_native_sticker_do_not_start_library_collection(
    tmp_path, message_type
):
    incoming = update(message_type=message_type)
    incoming["event"]["message"]["content"] = json.dumps(
        {"image_key": "img_original"} if message_type == "image" else {"file_key": "sticker_key"}
    )
    message = normalize_message(incoming["event"], client())
    library = EmojiLibrary(workspace_service(WorkspaceStore(tmp_path / "workspace")))
    library.collect_message = AsyncMock(side_effect=AssertionError("No automatic collection"))
    assert message is not None
    library.submit(message, source_event_id=1)
    assert not library._tasks
    library.collect_message.assert_not_called()
    if message_type == "sticker":
        assert not message.attachments
        assert message.segments[-1] == {
            "type": "feishu_sticker",
            "data": {"file_key": "sticker_key"},
        }
    await library.close()


def test_native_mentions_preserve_repetition_and_literal_at():
    incoming = update(text="literal @Yuki: @_user_1 plus @_user_1!")
    incoming["event"]["message"]["mentions"] = [
        {"key": "@_user_1", "id": {"open_id": "ou_yuki"}, "name": "雪"},
    ]
    message = normalize_message(incoming["event"], client())
    assert message.text == "literal @Yuki: @雪 plus @雪!"
    assert message.mentions_bot and len(message.mentions) == 2
    assert [message.text[item.text_start : item.text_end] for item in message.mentions] == [
        "@雪",
        "@雪",
    ]
    assert all(item.target_user_id == "cli_test:ou_yuki" for item in message.mentions)


def test_post_keeps_native_image_link_and_mention_without_fetching():
    incoming = update(message_type="post")
    incoming["event"]["message"]["content"] = json.dumps(
        {
            "zh_cn": {
                "title": "Title",
                "content": [
                    [
                        {"tag": "text", "text": "Hello "},
                        {"tag": "at", "user_id": "ou_person", "user_name": "Person"},
                        {"tag": "a", "text": "Docs", "href": "https://example.org/docs"},
                        {"tag": "img", "image_key": "img_post"},
                    ]
                ],
            }
        }
    )
    message = normalize_message(incoming["event"], client())
    assert message.text == "Title\nHello @PersonDocs (https://example.org/docs)"
    assert message.mentions[0].target_user_id == "cli_test:ou_person"
    assert json.loads(message.attachments[0].file)["resource_key"] == "img_post"


def test_empty_reference_and_unknown_native_content_are_not_replayed_chat():
    incoming = update(text="")
    incoming["event"]["message"]["parent_id"] = "om_old"
    assert normalize_message(incoming["event"], client()) is None
    incoming["event"]["message"]["message_type"] = "system"
    incoming["event"]["message"]["content"] = '{"text":"Not user speech"}'
    assert normalize_message(incoming["event"], client()) is None


def test_thread_uses_original_root_message_and_keeps_native_provenance():
    incoming = update(thread="omt_real")
    incoming["event"]["message"].update(root_id="om_root", parent_id="om_parent")
    message = normalize_message(incoming["event"], client())
    assert message.thread_id == "om_root" and message.reply_to_message_id == "om_parent"
    assert message.segments[-1] == {
        "type": "feishu_thread",
        "data": {"thread_id": "omt_real", "root_id": "om_root", "parent_id": "om_parent"},
    }
    del incoming["event"]["message"]["thread_id"]
    assert normalize_message(incoming["event"], client()).thread_id is None


def test_open_ids_are_separate_for_each_app_and_feishu_lark_namespace():
    event = update()["event"]
    first = normalize_message(event, client("cli_first"))
    second = normalize_message(event, client("cli_second", "lark"))
    assert first.sender.user_id == "cli_first:ou_person"
    assert second.sender.user_id == "cli_second:ou_person"
    assert first.platform == "feishu" and second.platform == "lark"


async def test_callback_from_other_app_cannot_admit_chat(feishu_env):
    incoming = update()
    incoming["header"]["app_id"] = "cli_other"
    with pytest.raises(PermissionError, match="feishu_event_app_mismatch"):
        await feishu_env.gateway.receive(incoming)
    assert not feishu_env.model.requests


@pytest.mark.parametrize("reply", [None, "om_selected"])
async def test_control_reply_uses_actual_outbound_reference_not_incoming_parent(reply):
    incoming = update()
    incoming["event"]["message"]["parent_id"] = "om_old"
    native_client = client()
    native_client.call = AsyncMock(return_value={"code": 0, "data": {"message_id": "om_sent"}})
    message = normalize_message(incoming["event"], native_client)
    sender = FeishuSender(SimpleNamespace(client=native_client), message)
    await sender.send(OutboundMessage("control result", reply_to_message_id=reply))
    expected = (
        "/open-apis/im/v1/messages/om_selected/reply" if reply else "/open-apis/im/v1/messages"
    )
    assert native_client.call.call_args.args == ("POST", expected)
