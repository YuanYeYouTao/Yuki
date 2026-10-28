"""Telegram Bot API social capabilities; native IDs stay in this adapter."""

from __future__ import annotations

import html
import re
from dataclasses import replace
from typing import cast

from yuki.domain.conversations import ConversationScope
from yuki.gateway.providers.telegram import TelegramAPIError, TelegramClient
from yuki.gateway.social import (
    TransportMedia,
    TransportMention,
    TransportMessage,
    TransportReceipt,
    TransportTarget,
    split_transport_message,
)


def _media_method(media: TransportMedia) -> tuple[str, str]:
    if media.kind == "file" and not media.emoji:
        return "sendDocument", "document"
    if media.emoji and media.mime_type in {"image/webp", "application/x-tgsticker", "video/webm"}:
        return "sendSticker", "sticker"
    if media.mime_type == "image/gif":
        return "sendAnimation", "animation"
    return ("sendPhoto", "photo") if media.kind == "image" else ("sendDocument", "document")


_NATIVE_ADDRESS = re.compile(r"@[a-zA-Z0-9_]+")


class TelegramSocialOperations:
    def native_reference_namespace(
        self, *, account_id: str, scope: ConversationScope
    ) -> tuple[str | None, ...]:
        return (
            scope.platform,
            scope.scope_type.value,
            scope.group_id or scope.private_peer_user_id,
            scope.thread_id,
        )

    def native_mention(self, text: str, offset: int) -> TransportMention | None:
        match = _NATIVE_ADDRESS.match(text, offset)
        return (
            TransportMention(
                match[0][1:], match[0][1:], offset, offset + len(match[0]), kind="address"
            )
            if match is not None
            else None
        )

    def plan_send(self, message: TransportMessage) -> tuple[TransportMessage, ...]:
        if (
            message.media is not None
            and _media_method(message.media)[0] == "sendSticker"
            and (message.text or message.mentions)
        ):
            # Stickers have no caption. Keep text and sticker as separate receipted effects.
            return (
                *self.plan_send(replace(message, media=None)),
                replace(message, text="", mentions=(), reply_reference=None),
            )
        # Bot API counts visible characters after entity parsing, not HTML bytes.
        # https://core.telegram.org/bots/api#sendmessage and #senddocument
        limit = 1024 if message.media is not None else 4096
        return split_transport_message(message, limit, following_limit=4096)

    async def send(
        self, handle: object, target: TransportTarget, message: TransportMessage
    ) -> TransportReceipt:
        client = cast(TelegramClient, handle)
        params: dict[str, object] = {"chat_id": target.external_id}
        if target.thread_id is not None:
            params["message_thread_id"] = int(target.thread_id)
        if message.reply_reference is not None:
            chat, reference = message.reply_reference.rsplit(":", 1)
            if chat != target.external_id:
                raise ValueError("reply_target_mismatch")
            params["reply_parameters"] = {"message_id": int(reference)}
        pieces: list[str] = []
        cursor = 0
        for mention in message.mentions:
            pieces.append(html.escape(message.text[cursor : mention.text_start]))
            label = html.escape(message.text[mention.text_start : mention.text_end])
            if mention.kind == "all":
                raise ValueError("all_mentions_unavailable")
            pieces.append(
                label
                if mention.kind == "address"
                else f'<a href="tg://user?id={mention.external_id}">{label}</a>'
            )
            cursor = mention.text_end
        pieces.append(html.escape(message.text[cursor:]))
        text = "".join(pieces)
        media = message.media
        if media is None:
            params["parse_mode"] = "HTML"
            params["text"] = text
            result = await client.call("sendMessage", params)
        else:
            method, field = _media_method(media)
            params[field] = "attach://" + field
            if text:
                if method == "sendSticker":
                    raise ValueError("use_separate_delivery_operations")
                params["parse_mode"] = "HTML"
                params["caption"] = text
            result = await client.call(
                method,
                params,
                files={field: (media.filename, media.content, media.mime_type)},
            )
        if not isinstance(result, dict) or type(result.get("message_id")) is not int:
            raise TelegramAPIError("telegram_receipt_missing", uncertain=True)
        return TransportReceipt(f"{target.external_id}:{result['message_id']}")

    async def recall(self, handle: object, target: TransportTarget, reference: str) -> None:
        chat, message = reference.rsplit(":", 1)
        if target.external_id and chat != target.external_id:
            raise ValueError("recall_target_mismatch")
        await cast(TelegramClient, handle).call(
            "deleteMessage", {"chat_id": chat, "message_id": int(message)}
        )

    async def is_member(self, handle: object, space: str, account: str) -> bool:
        result = await cast(TelegramClient, handle).call(
            "getChatMember", {"chat_id": space, "user_id": int(account)}
        )
        return (
            isinstance(result, dict)
            and result.get("status") not in {"left", "kicked"}
            and (result.get("status") != "restricted" or result.get("is_member") is True)
        )
