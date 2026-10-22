"""Feishu native events enter the common identity, Processor and Social runtime."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from yuki.domain.conversations import ScopeType
from yuki.domain.messages import (
    AttachmentKind,
    InboundMessage,
    MessageAttachment,
    MessageMention,
    OutboundMessage,
    OutboundSendReceipt,
    SenderIdentity,
)
from yuki.gateway.media import MediaKind, PlatformMediaFile
from yuki.gateway.providers.feishu import FeishuClient, encode_account_id
from yuki.gateway.providers.feishu_social import FeishuSocialOperations
from yuki.gateway.providers.feishu_ws import FeishuWebSocket
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.gateway.social import TransportMedia, TransportMessage, TransportTarget
from yuki.identity.canonical_repository import (
    create_presence,
    create_space_binding,
    find_space_binding,
)
from yuki.identity.receipt_compat import load_claimed_keeper, transport_receipt
from yuki.identity.routing import PresenceRouter
from yuki.persistence.database import Database
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from yuki.services.processor import MessageProcessor

logger = logging.getLogger(__name__)


def normalize_message(event: dict[str, Any], client: FeishuClient) -> InboundMessage | None:
    raw, sender = event.get("message"), event.get("sender")
    if not isinstance(raw, dict) or not isinstance(sender, dict):
        return None
    open_id = (sender.get("sender_id") or {}).get("open_id")
    if not open_id or raw.get("chat_type") not in {"p2p", "group"}:
        return None
    content = raw.get("content") or "{}"
    content = json.loads(content) if isinstance(content, str) else content
    message_id = str(raw["message_id"])
    texts: list[str] = []
    mentions: list[MessageMention] = []
    segments: list[dict[str, Any]] = []
    attachments: list[MessageAttachment] = []
    length = 0

    def text(value: str) -> None:
        nonlocal length
        if value:
            texts.append(value)
            segments.append({"type": "text", "data": {"text": value}})
            length += len(value)

    def mention(identifier: str, name: str, original: str) -> None:
        nonlocal length
        account = encode_account_id(client.app_id, identifier) if identifier != "all" else None
        label = "@" + (name or "未识别成员")
        mentions.append(
            MessageMention(
                "all" if identifier == "all" else "yuki" if account == client.self_id else "member",
                len(segments),
                length,
                length + len(label),
                target_user_id=account,
                display_name=name,
            )
        )
        segments.append(
            {
                "type": "feishu_mention",
                "data": {"open_id": identifier, "name": name, "key": original},
            }
        )
        texts.append(label)
        length += len(label)

    def text_with_mentions(value: str) -> None:
        known = {
            item["key"]: item
            for item in raw.get("mentions") or []
            if item.get("key") and (item.get("id") or {}).get("open_id")
        }
        if not known:
            text(value)
            return
        cursor = 0
        pattern = "|".join(re.escape(key) for key in sorted(known, key=len, reverse=True))
        for match in re.finditer(pattern, value):
            text(value[cursor : match.start()])
            item = known[match[0]]
            mention(item["id"]["open_id"], item.get("name") or "", match[0])
            cursor = match.end()
        text(value[cursor:])

    def media(kind: AttachmentKind, key: str, filename: str = "") -> None:
        locator = json.dumps(
            {
                "message_id": message_id,
                "resource_key": key,
                "type": "image" if kind is AttachmentKind.IMAGE else "file",
            },
            separators=(",", ":"),
        )
        attachment = MessageAttachment(
            kind,
            kind.value,
            segment_index=len(segments),
            file=locator,
            filename=filename or kind.value,
            # Native images are ordinary attachments, never automatic sticker adoption.
            is_sticker=False,
        )
        attachments.append(attachment)
        segments.append(
            {"type": kind.value, "data": {"file": locator, "name": attachment.filename}}
        )

    kind = raw.get("message_type")
    if kind == "text":
        text_with_mentions(str(content.get("text") or ""))
    elif kind == "post":
        post = content
        if "content" not in post:
            post = next((value for value in post.values() if isinstance(value, dict)), {})
        if post.get("title"):
            text(str(post["title"]) + "\n")
        for index, line in enumerate(post.get("content") or []):
            if index:
                text("\n")
            for item in line:
                tag = item.get("tag")
                if tag == "at" and item.get("user_id"):
                    identifier = item["user_id"]
                    native: dict[str, Any] = next(
                        (
                            entry
                            for entry in raw.get("mentions") or []
                            if (entry.get("id") or {}).get("open_id") == identifier
                        ),
                        {},
                    )
                    mention(
                        identifier, item.get("user_name") or native.get("name") or "", identifier
                    )
                elif tag in {"text", "md", "code_block"}:
                    text_with_mentions(str(item.get("text") or ""))
                elif tag == "a":
                    label, href = item.get("text") or "", item.get("href") or ""
                    text(f"{label} ({href})" if label and href and label != href else label or href)
                elif tag == "img" and item.get("image_key"):
                    media(AttachmentKind.IMAGE, item["image_key"])
                elif tag == "media" and item.get("file_key"):
                    media(AttachmentKind.VIDEO, item["file_key"])
                elif tag == "emotion" and item.get("emoji_type"):
                    text("[" + item["emoji_type"] + "]")
    elif kind == "image" and content.get("image_key"):
        media(AttachmentKind.IMAGE, content["image_key"])
    elif kind in {"file", "audio", "media"} and content.get("file_key"):
        media(
            {
                "file": AttachmentKind.FILE,
                "audio": AttachmentKind.AUDIO,
                "media": AttachmentKind.VIDEO,
            }[kind],
            content["file_key"],
            str(content.get("file_name") or ""),
        )
    elif kind == "sticker":
        # Bot resource download doesn't support the native sticker key.
        text("[原生贴纸]")
        segments.append({"type": "feishu_sticker", "data": dict(content)})
    else:
        return None
    body = "".join(texts)
    if not (body.strip() or attachments):
        return None
    if raw.get("thread_id"):
        segments.append(
            {
                "type": "feishu_thread",
                "data": {
                    key: raw[key] for key in ("thread_id", "root_id", "parent_id") if raw.get(key)
                },
            }
        )
    account = encode_account_id(client.app_id, str(open_id))
    return InboundMessage(
        message_id=message_id,
        event_type=f"message:{client.platform}",
        scope_type=ScopeType.PRIVATE if raw["chat_type"] == "p2p" else ScopeType.GROUP,
        sender=SenderIdentity(
            account,
            nickname=str(sender.get("name") or ""),
            nickname_known=bool(sender.get("name")),
            is_bot=sender.get("sender_type") == "app",
        ),
        text=body,
        raw_text=body,
        platform=client.platform,
        bot_user_id=client.self_id,
        group_id=str(raw["chat_id"]) if raw["chat_type"] == "group" else None,
        group_name=str(raw.get("chat_name") or ""),
        # Native omt IDs cannot be passed to the message-reply endpoint. A thread
        # route uses its original root message; the native thread stays above.
        thread_id=str(raw.get("root_id") or message_id) if raw.get("thread_id") else None,
        mentions=tuple(mentions),
        mentions_bot=any(item.kind == "yuki" for item in mentions),
        mentioned_user_ids=tuple(
            dict.fromkeys(
                item.target_user_id
                for item in mentions
                if item.kind == "member" and item.target_user_id is not None
            )
        ),
        is_self_message=account == client.self_id,
        attachments=tuple(attachments),
        segments=tuple(segments),
        reply_to_message_id=raw.get("parent_id") or None,
        occurred_at=datetime.fromtimestamp(int(raw["create_time"]) / 1000, UTC)
        if raw.get("create_time")
        else None,
    )


class FeishuSender:
    def __init__(self, gateway: FeishuGateway, message: InboundMessage) -> None:
        self.gateway, self.message, self.bot = gateway, message, gateway.client

    async def resolve_media_file(
        self, file: str, kind: MediaKind, *, max_download_bytes: int | None
    ) -> PlatformMediaFile:
        return await self.bot.resolve_media_file(file, kind, max_download_bytes=max_download_bytes)

    async def send(self, message: OutboundMessage) -> OutboundSendReceipt:
        if len(message.media) > 1:
            raise ValueError("use_separate_delivery_operations")
        media = message.media[0] if message.media else None
        receipt = await FeishuSocialOperations().send(
            self.bot,
            TransportTarget(
                "space" if self.message.group_id else "person",
                self.message.group_id or self.message.sender.user_id,
                self.message.thread_id,
            ),
            TransportMessage(
                message.text,
                reply_reference=message.reply_to_message_id,
                media=TransportMedia(media.kind.value, media.content, "attachment", media.mime_type)
                if media
                else None,
            ),
        )
        assert receipt.reference is not None
        return OutboundSendReceipt(receipt.reference, transport=self.bot.platform)


class FeishuGateway:
    def __init__(
        self,
        database: Database,
        registry: GatewayConnectionRegistry,
        router: PresenceRouter,
        processor: MessageProcessor,
        writer: ScopedEventLedgerUnitOfWork,
        client: FeishuClient,
        *,
        websocket_factory: Callable[..., FeishuWebSocket] = FeishuWebSocket,
    ) -> None:
        self.database, self.registry, self.router = database, registry, router
        self.processor, self.writer, self.client = processor, writer, client
        self.presence_id = ""
        self._turns: set[asyncio.Task[Any]] = set()
        self._websocket = websocket_factory(
            client,
            self.receive,
            on_connected=self._connected,
            on_disconnected=self._disconnected,
        )

    def _connected(self) -> None:
        self.registry.connect(
            self.client, provider_id=self.client.platform, presence_id=self.presence_id
        )

    def _disconnected(self) -> None:
        self.registry.disconnect(self.client)

    async def start(self) -> None:
        await self.client.get_me()
        async with self.database.immediate_session() as session:
            presence = await create_presence(
                session,
                external_account_id=self.client.self_id,
                platform=self.client.platform,
                now=datetime.now(UTC),
            )
            self.presence_id = presence.id
        await self._websocket.start()

    async def close(self) -> None:
        await self._websocket.close()
        for task in self._turns:
            task.cancel()
        await asyncio.gather(*tuple(self._turns), return_exceptions=True)
        self._disconnected()
        await self.client.close()

    async def health(self) -> dict[str, object]:
        socket = self._websocket
        return {
            "enabled": True,
            "running": socket.running,
            "healthy": socket.connected and socket.last_error_category is None,
            "last_success_at": socket.last_success_at.isoformat()
            if socket.last_success_at
            else None,
            "last_error_category": socket.last_error_category,
            "active_turns": len(self._turns),
        }

    async def receive(self, payload: dict[str, Any]) -> None:
        header = payload.get("header") or {}
        if header.get("event_type") != "im.message.receive_v1":
            return
        if header.get("app_id") and header["app_id"] != self.client.app_id:
            raise PermissionError("feishu_event_app_mismatch")
        message = normalize_message(payload.get("event") or {}, self.client)
        if message is None:
            return
        async with self.database.read_session() as session:
            receipt = await transport_receipt(
                session,
                presence_id=self.presence_id,
                scope=message.scope(),
                event_type=message.event_type,
                platform_message_id=message.message_id,
            )
            original = await load_claimed_keeper(
                session, receipt.canonical_event_id if receipt is not None else None
            )
        if original is not None:
            if original.sender_user_id != message.sender.user_id:
                raise PermissionError("feishu_source_conflict")
            return
        if message.group_id is not None:
            async with self.database.read_session() as session:
                binding = await find_space_binding(
                    session, message.group_id, platform=self.client.platform
                )
            if binding is None:
                if not await FeishuSocialOperations().is_member(
                    self.client, message.group_id, self.client.self_id
                ):
                    raise PermissionError("feishu_membership_unavailable")
                async with self.database.immediate_session() as session:
                    await create_space_binding(
                        session,
                        external_space_id=message.group_id,
                        name=message.group_name,
                        enabled=True,
                        autonomous_enabled=True,
                        require_mention=False,
                        now=datetime.now(UTC),
                        platform=self.client.platform,
                    )
        acknowledged = asyncio.get_running_loop().create_future()
        acknowledged.add_done_callback(
            lambda result: result.exception() if not result.cancelled() else None
        )
        task = asyncio.create_task(
            self.processor.handle(
                message,
                FeishuSender(self, message),
                on_committed=lambda result: (
                    acknowledged.set_result(result) if not acknowledged.done() else None
                ),
            ),
            name=f"{self.client.platform}-message:{message.message_id}",
        )
        self._turns.add(task)

        def finished(completed: asyncio.Task[Any]) -> None:
            self._turns.discard(completed)
            error = asyncio.CancelledError() if completed.cancelled() else completed.exception()
            if not acknowledged.done():
                if error is not None:
                    acknowledged.set_exception(error)
                else:
                    acknowledged.set_result(None)
            elif error is not None and not isinstance(error, asyncio.CancelledError):
                logger.error("feishu_processing_failed category=%s", type(error).__name__)

        task.add_done_callback(finished)
        await asyncio.shield(acknowledged)
