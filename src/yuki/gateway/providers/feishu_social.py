"""Feishu/Lark message dispatch under the shared Social receipt contract."""

from __future__ import annotations

import html
import json
from dataclasses import replace
from typing import Any, cast
from urllib.parse import quote

from yuki.gateway.providers.feishu import FeishuAPIError, FeishuClient, decode_account_id
from yuki.gateway.social import (
    TransportMention,
    TransportMessage,
    TransportReceipt,
    TransportTarget,
)

# Native text requests are limited to 150 KB, including JSON escaping.
_TEXT_REQUEST_BYTES = 150_000


def _text_content(message: TransportMessage, app_id: str | None = None) -> str:
    pieces: list[str] = []
    cursor = 0
    for mention in message.mentions:
        pieces.append(message.text[cursor : mention.text_start])
        if mention.kind == "all":
            account = "all"
        else:
            account = (
                decode_account_id(app_id, mention.external_id)
                if app_id is not None
                else mention.external_id.rsplit(":", 1)[-1]
            )
        label = html.escape(message.text[mention.text_start : mention.text_end])
        pieces.append(f'<at user_id="{html.escape(account, quote=True)}">{label}</at>')
        cursor = mention.text_end
    pieces.append(message.text[cursor:])
    return json.dumps({"text": "".join(pieces)}, ensure_ascii=False, separators=(",", ":"))


def _text_request_size(message: TransportMessage) -> int:
    return len(
        json.dumps(
            {"msg_type": "text", "content": _text_content(message)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _slice(message: TransportMessage, start: int, end: int) -> TransportMessage:
    return replace(
        message,
        text=message.text[start:end],
        mentions=tuple(
            replace(item, text_start=item.text_start - start, text_end=item.text_end - start)
            for item in message.mentions
            if start <= item.text_start < end
        ),
        reply_reference=message.reply_reference if start == 0 else None,
    )


def _text_parts(message: TransportMessage) -> tuple[TransportMessage, ...]:
    if _text_request_size(message) <= _TEXT_REQUEST_BYTES:
        return (message,)
    parts: list[TransportMessage] = []
    start = 0
    while start < len(message.text):
        low, high = start + 1, len(message.text)
        while low <= high:
            end = (low + high) // 2
            if _text_request_size(_slice(message, start, end)) <= _TEXT_REQUEST_BYTES:
                low = end + 1
            else:
                high = end - 1
        end = high
        for mention in message.mentions:
            if mention.text_start < end < mention.text_end:
                end = mention.text_start
                break
        if end <= start:
            raise ValueError("mention_exceeds_platform_limit")
        parts.append(_slice(message, start, end))
        start = end
    return tuple(parts)


class FeishuSocialOperations:
    def native_mention(self, text: str, offset: int) -> TransportMention | None:
        # Display names alone do not provide an Open ID. Shared local resolution owns them.
        return None

    def plan_send(self, message: TransportMessage) -> tuple[TransportMessage, ...]:
        if message.media is not None and (message.text or message.mentions):
            return (
                *_text_parts(replace(message, media=None)),
                replace(message, text="", mentions=(), reply_reference=None),
            )
        return (message,) if message.media is not None else _text_parts(message)

    async def send(
        self, handle: object, target: TransportTarget, message: TransportMessage
    ) -> TransportReceipt:
        client = cast(FeishuClient, handle)
        private = target.kind == "person"
        receive_id = (
            decode_account_id(client.app_id, target.external_id) if private else target.external_id
        )
        body: dict[str, Any]
        if message.media is None:
            body = {"msg_type": "text", "content": _text_content(message, client.app_id)}
        else:
            if message.text or message.mentions:
                raise ValueError("use_separate_delivery_operations")
            media = message.media
            image = media.kind == "image"
            upload = await client.call(
                "POST",
                "/open-apis/im/v1/images" if image else "/open-apis/im/v1/files",
                json=(
                    {"image_type": "message"}
                    if image
                    else {"file_type": "stream", "file_name": media.filename}
                ),
                files={
                    "image" if image else "file": (
                        media.filename,
                        media.content,
                        media.mime_type,
                    )
                },
            )
            data = upload.get("data")
            key_name = "image_key" if image else "file_key"
            if (
                not isinstance(data, dict)
                or not isinstance(data.get(key_name), str)
                or not data[key_name]
            ):
                raise FeishuAPIError("feishu_upload_receipt_missing")
            body = {
                "msg_type": "image" if image else "file",
                "content": json.dumps({key_name: data[key_name]}, separators=(",", ":")),
            }

        if message.reply_reference is not None or target.thread_id is not None:
            parent = message.reply_reference or target.thread_id
            assert parent is not None
            body["reply_in_thread"] = target.thread_id is not None
            result = await client.call(
                "POST", f"/open-apis/im/v1/messages/{quote(parent, safe='')}/reply", json=body
            )
        else:
            body["receive_id"] = receive_id
            result = await client.call(
                "POST",
                "/open-apis/im/v1/messages",
                params={"receive_id_type": "open_id" if private else "chat_id"},
                json=body,
            )
        data = result.get("data")
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("message_id"), str)
            or not data["message_id"]
        ):
            raise FeishuAPIError("feishu_receipt_missing", uncertain=True)
        return TransportReceipt(data["message_id"])

    async def recall(self, handle: object, target: TransportTarget, reference: str) -> None:
        await cast(FeishuClient, handle).call(
            "DELETE", f"/open-apis/im/v1/messages/{quote(reference, safe='')}"
        )

    async def is_member(self, handle: object, space: str, account: str) -> bool:
        client = cast(FeishuClient, handle)
        path = f"/open-apis/im/v1/chats/{quote(space, safe='')}/members"
        if account == client.self_id:
            payload = await client.call("GET", path + "/is_in_chat")
            data = payload.get("data")
            if not isinstance(data, dict) or type(data.get("is_in_chat")) is not bool:
                raise FeishuAPIError("feishu_invalid_member_response")
            return cast(bool, data["is_in_chat"])
        open_id = decode_account_id(client.app_id, account)
        params: dict[str, Any] = {"member_id_type": "open_id", "page_size": 100}
        while True:
            payload = await client.call("GET", path, params=params)
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise FeishuAPIError("feishu_invalid_member_response")
            if any(item.get("member_id") == open_id for item in data["items"]):
                return True
            if not data.get("has_more"):
                return False
            params["page_token"] = data["page_token"]
