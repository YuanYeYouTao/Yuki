"""Native Telegram normalization and long polling into the existing Processor."""

from __future__ import annotations

import asyncio
import logging
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
from yuki.gateway.providers.telegram import TelegramClient
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.gateway.social import TransportMedia, TransportMessage, TransportTarget
from yuki.identity.canonical_repository import (
    create_presence,
    create_space_binding,
    find_space_binding,
)
from yuki.identity.receipt_compat import load_claimed_keeper, transport_receipt
from yuki.identity.routing import PresenceRouter
from yuki.identity.space_metadata import observe_space_name
from yuki.persistence.database import Database
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from yuki.services.processor import MessageProcessor

logger = logging.getLogger(__name__)


def _text_mentions(
    raw: dict[str, Any], client: TelegramClient
) -> tuple[str, tuple[MessageMention, ...], list[dict[str, Any]]]:
    text = str(raw.get("text") or raw.get("caption") or "")
    entities = raw.get("entities") or raw.get("caption_entities") or []
    entities = sorted(
        (item for item in entities if item.get("type") in {"mention", "text_mention"}),
        key=lambda item: item["offset"],
    )
    if not entities:
        return text, (), [{"type": "text", "data": {"text": text}}] if text else []
    boundaries = {
        point
        for entity in entities
        for point in (entity["offset"], entity["offset"] + entity["length"])
    }
    offsets = {}
    units = 0
    for index, character in enumerate(text):
        if units in boundaries:
            offsets[units] = index
        units += 2 if ord(character) > 0xFFFF else 1
    if units in boundaries:
        offsets[units] = len(text)
    mentions: list[MessageMention] = []
    segments: list[dict[str, Any]] = []
    cursor = 0
    for entity in entities:
        kind = entity.get("type")
        if kind not in {"mention", "text_mention"}:
            continue
        start_units, end_units = entity["offset"], entity["offset"] + entity["length"]
        start, end = offsets[start_units], offsets[end_units]
        if start > cursor:
            segments.append({"type": "text", "data": {"text": text[cursor:start]}})
        token = text[start:end]
        user = entity.get("user", {})
        target = str(user["id"]) if kind == "text_mention" else None
        is_yuki = target == client.self_id or (
            kind == "mention" and token.casefold() == ("@" + client.username).casefold()
        )
        if is_yuki:
            target = client.self_id
        name = " ".join(
            part for part in (user.get("first_name"), user.get("last_name")) if part
        ) or token.removeprefix("@")
        mentions.append(
            MessageMention(
                kind="yuki" if is_yuki else "member",
                segment_index=len(segments),
                text_start=start,
                text_end=end,
                target_user_id=target,
                display_name=name,
            )
        )
        segments.append(
            {
                "type": "telegram_mention",
                "data": {
                    "text": token,
                    "target_user_id": target,
                    "entity_type": kind,
                    "display_name": name,
                    "native_username": user.get("username", ""),
                },
            }
        )
        cursor = end
    if cursor < len(text):
        segments.append({"type": "text", "data": {"text": text[cursor:]}})
    return text, tuple(mentions), segments


def _message_content(
    raw: dict[str, Any], client: TelegramClient, *, source: str = "current"
) -> tuple[str, tuple[MessageMention, ...], list[dict[str, Any]], tuple[MessageAttachment, ...]]:
    text, mentions, segments = _text_mentions(raw, client)
    attachments: list[MessageAttachment] = []
    media_types = (
        ("photo", AttachmentKind.IMAGE),
        ("animation", AttachmentKind.VIDEO),
        ("video", AttachmentKind.VIDEO),
        ("voice", AttachmentKind.AUDIO),
        ("audio", AttachmentKind.AUDIO),
        ("document", AttachmentKind.FILE),
        ("sticker", AttachmentKind.IMAGE),
    )
    for field, kind in media_types:
        media = raw.get(field)
        if not media or (field == "document" and raw.get("animation")):
            continue
        if field == "photo":
            media = media[-1]
        sticker_format = None
        mime_type = media.get("mime_type")
        if field == "sticker":
            if media.get("is_video"):
                kind, sticker_format, mime_type = AttachmentKind.VIDEO, "video", "video/webm"
            elif media.get("is_animated"):
                kind, sticker_format, mime_type = (
                    AttachmentKind.FILE,
                    "animated",
                    "application/x-tgsticker",
                )
            else:
                sticker_format, mime_type = "static", "image/webp"
        extension = {"static": "webp", "animated": "tgs", "video": "webm"}.get(sticker_format or "")
        filename = str(media.get("file_name") or (f"sticker.{extension}" if extension else field))
        thumbnail = media.get("thumbnail") if sticker_format == "animated" else None
        preview = (
            MessageAttachment(
                AttachmentKind.IMAGE,
                "sticker_preview",
                source=source,
                file=str(thumbnail["file_id"]),
                file_size=thumbnail.get("file_size"),
                width=thumbnail.get("width"),
                height=thumbnail.get("height"),
            )
            if isinstance(thumbnail, dict) and thumbnail.get("file_id")
            else None
        )
        attachment = MessageAttachment(
            kind,
            field,
            segment_index=len(segments),
            source=source,
            file=str(media["file_id"]),
            file_size=media.get("file_size"),
            filename=filename,
            summary=media.get("emoji"),
            is_sticker=field == "sticker",
            sticker_format=sticker_format,
            mime_type=mime_type,
            width=media.get("width"),
            height=media.get("height"),
            preview=preview,
        )
        attachments.append(attachment)
        segments.append(
            {
                "type": kind.value,
                "data": {
                    "file": attachment.file,
                    "name": filename,
                    "file_size": attachment.file_size,
                    "mime_type": mime_type,
                    "is_sticker": attachment.is_sticker,
                    "sticker_format": sticker_format,
                    "summary": attachment.summary,
                    "width": attachment.width,
                    "height": attachment.height,
                    **(
                        {
                            "preview": {
                                "file": preview.file,
                                "file_size": preview.file_size,
                                "width": preview.width,
                                "height": preview.height,
                            }
                        }
                        if preview is not None
                        else {}
                    ),
                },
            }
        )
    return text, mentions, segments, tuple(attachments)


def normalize_message(raw: dict[str, Any], client: TelegramClient) -> InboundMessage | None:
    chat = raw["chat"]
    if chat["type"] not in {"private", "group", "supergroup"} or not isinstance(
        raw.get("from"), dict
    ):
        return None
    author = raw["from"]
    text, mentions, segments, attachments = _message_content(raw, client)
    mentions_bot = any(mention.kind == "yuki" for mention in mentions)
    # A reply relation alone is not a new chat body; native service updates have none.
    if not (text.strip() or attachments):
        return None
    reply = raw.get("reply_to_message")
    reply_author = reply.get("from") if isinstance(reply, dict) else None
    reply_text, reply_mentions, reply_segments, reply_attachments = (
        _message_content(reply, client, source="reply")
        if isinstance(reply, dict)
        else (None, (), [], ())
    )
    chat_id = str(chat["id"])
    return InboundMessage(
        message_id=f"{chat_id}:{raw['message_id']}",
        event_type="message:telegram",
        platform="telegram",
        bot_user_id=client.self_id,
        scope_type=ScopeType.PRIVATE if chat["type"] == "private" else ScopeType.GROUP,
        group_id=None if chat["type"] == "private" else chat_id,
        group_name=chat.get("title", "") if chat["type"] != "private" else "",
        thread_id=str(raw["message_thread_id"])
        if raw.get("message_thread_id") is not None
        else None,
        sender=SenderIdentity(
            str(author["id"]),
            nickname=" ".join(
                part for part in (author.get("first_name"), author.get("last_name")) if part
            ),
            is_bot=bool(author.get("is_bot")),
            nickname_known=True,
            native_username=author.get("username", ""),
        ),
        text=text,
        raw_text=text,
        mentions_bot=mentions_bot,
        mentioned_user_ids=tuple(
            dict.fromkeys(
                mention.target_user_id
                for mention in mentions
                if mention.kind == "member" and mention.target_user_id is not None
            )
        ),
        mentions=mentions,
        reply_mentions=reply_mentions,
        attachments=tuple(attachments),
        segments=tuple(segments),
        reply_segments=tuple(reply_segments),
        reply_attachments=reply_attachments,
        reply_to_message_id=f"{chat_id}:{reply['message_id']}" if isinstance(reply, dict) else None,
        reply_sender_user_id=str(reply_author["id"]) if isinstance(reply_author, dict) else None,
        reply_text=reply_text,
        occurred_at=datetime.fromtimestamp(raw["date"], UTC)
        if raw.get("date") is not None
        else None,
        received_at=datetime.now(UTC),
    )


class TelegramSender:
    def __init__(self, gateway: TelegramGateway, message: InboundMessage) -> None:
        self.gateway, self.message = gateway, message
        self.bot = gateway.client

    async def resolve_media_file(
        self, file: str, kind: MediaKind, *, max_download_bytes: int | None
    ) -> PlatformMediaFile:
        return await self.bot.resolve_media_file(file, kind, max_download_bytes=max_download_bytes)

    async def send(self, message: OutboundMessage) -> OutboundSendReceipt:
        from yuki.gateway.providers.telegram_social import TelegramSocialOperations

        if len(message.media) > 1:
            raise ValueError("use_separate_delivery_operations")
        media = message.media[0] if message.media else None
        receipt = await TelegramSocialOperations().send(
            self.bot,
            TransportTarget(
                "space" if self.message.group_id else "person",
                self.message.group_id or self.message.sender.user_id,
                self.message.thread_id,
            ),
            TransportMessage(
                message.text,
                reply_reference=message.reply_to_message_id,
                media=TransportMedia(
                    media.kind.value,
                    media.content,
                    "attachment",
                    media.mime_type,
                    bool(media.emoji_id),
                )
                if media is not None
                else None,
            ),
        )
        assert receipt.reference is not None
        return OutboundSendReceipt(receipt.reference, transport="telegram")


class TelegramGateway:
    def __init__(
        self,
        database: Database,
        registry: GatewayConnectionRegistry,
        router: PresenceRouter,
        processor: MessageProcessor,
        writer: ScopedEventLedgerUnitOfWork,
        client: TelegramClient,
        *,
        poll_interval: float = 1,
    ) -> None:
        self.database, self.registry, self.router = database, registry, router
        self.processor, self.writer, self.client = processor, writer, client
        self.poll_interval = poll_interval
        self._poll: asyncio.Task[None] | None = None
        self._turns: set[asyncio.Task[Any]] = set()
        self.presence_id = ""
        self._last_success_at: datetime | None = None
        self._last_error_category: str | None = None

    async def start(self) -> None:
        await self.client.get_me()
        self._last_success_at = datetime.now(UTC)
        self._last_error_category = None
        async with self.database.immediate_session() as session:
            presence = await create_presence(
                session,
                external_account_id=self.client.self_id,
                now=datetime.now(UTC),
                platform="telegram",
            )
            self.presence_id = presence.id
        self.registry.connect(self.client, provider_id="telegram", presence_id=self.presence_id)
        self._poll = asyncio.create_task(self._run(), name="telegram-ingress")

    async def close(self) -> None:
        if self._poll is not None:
            self._poll.cancel()
            await asyncio.gather(self._poll, return_exceptions=True)
        for task in self._turns:
            task.cancel()
        await asyncio.gather(*tuple(self._turns), return_exceptions=True)
        self.registry.disconnect(self.client)
        await self.client.close()

    async def health(self) -> dict[str, object]:
        running = self._poll is not None and not self._poll.done()
        return {
            "enabled": True,
            "running": running,
            "healthy": running and self._last_error_category is None,
            "last_success_at": self._last_success_at.isoformat()
            if self._last_success_at is not None
            else None,
            "last_error_category": self._last_error_category,
            "active_turns": len(self._turns),
        }

    async def _run(self) -> None:
        offset = None
        while True:
            try:
                params = {"timeout": 30, "allowed_updates": ["message", "edited_message"]}
                if offset is not None:
                    params["offset"] = offset
                updates = await self.client.call("getUpdates", params)
                self._last_success_at = datetime.now(UTC)
                self._last_error_category = None
                for update in updates:
                    await self.receive(update)
                    # Only the next successful request confirms this update.
                    # Original event receipts handle restart/redelivery.
                    offset = update["update_id"] + 1
            except Exception as exc:
                self._last_error_category = type(exc).__name__
                logger.error("telegram_poll_failed category=%s", type(exc).__name__)
                await asyncio.sleep(self.poll_interval)

    async def receive(self, update: dict[str, Any]) -> None:
        raw = update.get("message") or update.get("edited_message")
        if not isinstance(raw, dict):
            return
        migration_to = raw.get("migrate_to_chat_id")
        migration_from = raw.get("migrate_from_chat_id")
        if migration_to is not None or migration_from is not None:
            previous = str(raw["chat"]["id"] if migration_to is not None else migration_from)
            current = str(migration_to if migration_to is not None else raw["chat"]["id"])
            async with self.database.immediate_session() as session:
                binding = await find_space_binding(session, previous, platform="telegram")
                current_binding = await find_space_binding(session, current, platform="telegram")
                if binding is not None:
                    if current_binding is not None and current_binding.id != binding.id:
                        raise ValueError("telegram_space_migration_conflict")
                    binding.external_space_id = current
                    binding.revision += 1
                    binding.updated_at = datetime.now(UTC)
            return
        if "new_chat_title" in raw and "edited_message" not in update:
            async with self.database.sessions() as session:
                binding = await find_space_binding(
                    session, str(raw["chat"]["id"]), platform="telegram"
                )
            if binding is not None:
                await observe_space_name(
                    self.database,
                    presence_id=self.presence_id,
                    space_binding_id=binding.id,
                    name=raw["new_chat_title"],
                )
            return
        message = normalize_message(raw, self.client)
        if message is None:
            return
        async with self.database.sessions() as session:
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
        if "edited_message" in update:
            if original is not None:
                await self.writer.update_platform_message(
                    original.id,
                    message,
                    presence_id=self.presence_id,
                    edited_at=datetime.fromtimestamp(raw["edit_date"], UTC),
                    revision=update["update_id"],
                )
            return
        if original is not None:
            if original.sender_user_id != message.sender.user_id:
                raise PermissionError("telegram_source_conflict")
            return
        if message.group_id is not None:
            async with self.database.sessions() as session:
                binding = await find_space_binding(session, message.group_id, platform="telegram")
            if binding is None:
                from yuki.gateway.providers.telegram_social import TelegramSocialOperations

                if not await TelegramSocialOperations().is_member(
                    self.client, message.group_id, self.client.self_id
                ):
                    raise PermissionError("telegram_membership_unavailable")
                async with self.database.immediate_session() as session:
                    await create_space_binding(
                        session,
                        external_space_id=message.group_id,
                        name=str(raw["chat"].get("title") or ""),
                        enabled=True,
                        autonomous_enabled=True,
                        require_mention=False,
                        now=datetime.now(UTC),
                        platform="telegram",
                    )
        acknowledged = asyncio.get_running_loop().create_future()
        acknowledged.add_done_callback(
            lambda result: result.exception() if not result.cancelled() else None
        )
        task = asyncio.create_task(
            self.processor.handle(
                message,
                TelegramSender(self, message),
                on_committed=lambda result: (
                    acknowledged.set_result(result) if not acknowledged.done() else None
                ),
            ),
            name=f"telegram-message:{update['update_id']}",
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
                logger.error("telegram_processing_failed category=%s", type(error).__name__)

        task.add_done_callback(finished)
        await asyncio.shield(acknowledged)
