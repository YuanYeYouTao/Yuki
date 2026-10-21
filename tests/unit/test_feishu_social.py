from __future__ import annotations

import json

import pytest

from yuki.gateway.providers.feishu import FeishuAPIError
from yuki.gateway.providers.feishu_social import FeishuSocialOperations
from yuki.gateway.social import TransportMedia, TransportMention, TransportMessage, TransportTarget


class Client:
    app_id = "cli_a"
    self_id = "cli_a:ou_bot"

    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    async def call(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return next(self.results)


async def test_private_mentions_decode_only_for_the_same_application():
    client = Client([{"code": 0, "data": {"message_id": "om_sent"}}])
    message = TransportMessage("Hi @A", (TransportMention("cli_a:ou_a", "A", 3, 5),))
    receipt = await FeishuSocialOperations().send(
        client, TransportTarget("person", "cli_a:ou_a"), message
    )
    assert receipt.reference == "om_sent"
    _, path, kwargs = client.calls[0]
    assert path.endswith("/messages")
    assert kwargs["params"] == {"receive_id_type": "open_id"}
    assert kwargs["json"]["receive_id"] == "ou_a"
    assert json.loads(kwargs["json"]["content"])["text"] == 'Hi <at user_id="ou_a">@A</at>'
    with pytest.raises(ValueError, match="app_mismatch"):
        await FeishuSocialOperations().send(
            client, TransportTarget("person", "cli_b:ou_a"), TransportMessage("hello")
        )
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "kind,mime,key", [("image", "image/png", "image_key"), ("file", "text/plain", "file_key")]
)
async def test_media_caption_is_a_separately_receipted_message(kind, mime, key):
    media = TransportMedia(kind, b"bytes", "original.dat", mime, emoji=True)
    message = TransportMessage("caption", reply_reference="om_original", media=media)
    operations = FeishuSocialOperations()
    parts = operations.plan_send(message)
    assert len(parts) == 2
    assert (parts[0].text, parts[0].media, parts[0].reply_reference) == (
        "caption",
        None,
        "om_original",
    )
    assert (parts[1].text, parts[1].media, parts[1].reply_reference) == ("", media, None)
    client = Client(
        [
            {"code": 0, "data": {"message_id": "om_text"}},
            {"code": 0, "data": {key: "resource_uploaded"}},
            {"code": 0, "data": {"message_id": "om_media"}},
        ]
    )
    target = TransportTarget("space", "oc_group")
    receipts = [await operations.send(client, target, part) for part in parts]
    assert [item.reference for item in receipts] == ["om_text", "om_media"]
    assert client.calls[0][1].endswith("/om_original/reply")
    assert client.calls[-1][2]["json"]["msg_type"] == kind
    assert json.loads(client.calls[-1][2]["json"]["content"]) == {key: "resource_uploaded"}
    assert client.calls[1][2]["files"]["image" if kind == "image" else "file"][1] == b"bytes"
    assert all("sticker" not in call[1] for call in client.calls)


async def test_thread_and_quote_are_native_replies_with_one_send():
    client = Client([{"code": 0, "data": {"message_id": "om_sent"}}])
    await FeishuSocialOperations().send(
        client,
        TransportTarget("space", "oc_group", "om_root"),
        TransportMessage("answer", reply_reference="om_question"),
    )
    assert len(client.calls) == 1
    assert client.calls[0][1].endswith("/om_question/reply")
    assert client.calls[0][2]["json"]["reply_in_thread"] is True


def test_large_unicode_text_splits_at_json_bytes_and_keeps_mentions_whole():
    text = "好" * 49970 + "@长名字" + "🙂" * 2000
    start = text.index("@")
    message = TransportMessage(
        text, (TransportMention("cli_a:ou_a", "长名字", start, start + 4),), "om_original"
    )
    parts = FeishuSocialOperations().plan_send(message)
    assert len(parts) > 1
    assert "".join(part.text for part in parts) == text
    assert sum(len(part.mentions) for part in parts) == 1
    assert [part.reply_reference for part in parts] == ["om_original", *[None] * (len(parts) - 1)]
    for part in parts:
        for mention in part.mentions:
            assert part.text[mention.text_start : mention.text_end] == "@长名字"


async def test_missing_message_receipt_is_uncertain_and_does_not_trigger_another_send():
    client = Client([{"code": 0, "data": {}}])
    with pytest.raises(FeishuAPIError) as raised:
        await FeishuSocialOperations().send(
            client, TransportTarget("space", "oc_a"), TransportMessage("hello")
        )
    assert raised.value.uncertain
    assert len(client.calls) == 1


async def test_member_probe_uses_self_token_or_real_member_pages():
    operations = FeishuSocialOperations()
    client = Client(
        [
            {"code": 0, "data": {"is_in_chat": True}},
            {
                "code": 0,
                "data": {
                    "items": [{"member_id": "ou_other"}],
                    "has_more": True,
                    "page_token": "next",
                },
            },
            {"code": 0, "data": {"items": [{"member_id": "ou_a"}], "has_more": False}},
        ]
    )
    assert await operations.is_member(client, "oc_group", client.self_id)
    assert await operations.is_member(client, "oc_group", "cli_a:ou_a")
    assert client.calls[0][1].endswith("/members/is_in_chat")
    assert client.calls[-1][2]["params"]["page_token"] == "next"


async def test_recall_uses_the_original_platform_message_reference():
    client = Client([{"code": 0}])
    await FeishuSocialOperations().recall(client, TransportTarget("space", "oc_group"), "om_sent")
    assert client.calls == [("DELETE", "/open-apis/im/v1/messages/om_sent", {})]
