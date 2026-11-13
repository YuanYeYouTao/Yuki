"""QQ official Bot WebSocket gateway into the common identity, Processor and Social.

Only the WebSocket gateway is used, so no public callback address is needed. The
session follows the platform protocol as SuInk/Diana implements it: Hello, IDENTIFY or
RESUME, heartbeats with the last sequence, and a read deadline against half-open links.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

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
from yuki.gateway.providers.qq_official import (
    QQ_OFFICIAL_PROVIDER_ID,
    QQOfficialAPIError,
    QQOfficialClient,
    QQOfficialConnection,
)
from yuki.gateway.providers.qq_official_social import send_message
from yuki.gateway.providers.qq_official_store import is_outbound, message_for_ref, record_message
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.gateway.social import TransportMedia, TransportMessage, TransportTarget
from yuki.identity.canonical_repository import (
    create_presence,
    create_space_binding,
    find_space_binding,
)
from yuki.identity.receipt_compat import load_claimed_keeper, transport_receipt
from yuki.identity.routing import PresenceRouter
from yuki.persistence.database import Database, _complete
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from yuki.services.processor import MessageProcessor

logger = logging.getLogger(__name__)
# websockets DEBUG would write full event frames; keep only warnings from the protocol.
_protocol_logger = logging.getLogger(__name__ + ".protocol")
_protocol_logger.setLevel(logging.WARNING)

INTENT_GROUP_MEMBERS = 1 << 24
INTENT_GROUP_AND_C2C = 1 << 25
INTENT_INTERACTION = 1 << 26
# Close codes: 4004 invalid token; 4013/4014 intents invalid or not permitted.
_INVALID_TOKEN = 4004
_INTENT_REJECTED = frozenset({4013, 4014})
_MESSAGE_EVENTS = frozenset(
    {"C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}
)
_QUOTE = 103
_MENTION_TOKEN = re.compile(r"<@!?([A-Za-z0-9_-]{1,64})>")
_FACE = re.compile(r"<faceType=\d+[^>]*>")
_FACE_EXT = re.compile(r'ext="([^"]*)"')


def _face_text(content: str) -> str:
    """Face tags carry their name in a base64 JSON ``ext``; never show the raw tag."""

    def name(tag: re.Match[str]) -> str:
        ext = _FACE_EXT.search(tag[0])
        try:
            label = json.loads(base64.b64decode(ext[1]))["text"] if ext else ""
        except (ValueError, KeyError, TypeError):
            label = ""
        return f"[表情:{label}]" if label else "[表情]"

    return _FACE.sub(name, content)


def _attachments(
    items: list[dict[str, Any]], *, source: str, first_index: int
) -> tuple[list[MessageAttachment], list[dict[str, Any]]]:
    attachments: list[MessageAttachment] = []
    segments: list[dict[str, Any]] = []
    for item in items:
        url = str(item.get("url") or "")
        if not url:
            continue
        if "://" not in url:  # Some pushes omit the scheme of an https download URL.
            url = "https://" + url.removeprefix("//")
        content_type = str(item.get("content_type") or "").lower()
        kind = (
            AttachmentKind.IMAGE
            if content_type.startswith("image")
            else AttachmentKind.VIDEO
            if content_type.startswith("video")
            else AttachmentKind.AUDIO
            if content_type == "voice" or content_type.startswith("audio")
            else AttachmentKind.FILE
        )
        attachment = MessageAttachment(
            kind,
            kind.value,
            segment_index=first_index + len(segments),
            source=source,
            file=url,
            file_size=item.get("size") if isinstance(item.get("size"), int) else None,
            filename=str(item.get("filename") or kind.value),
            mime_type=content_type if "/" in content_type else None,
            width=item.get("width") if isinstance(item.get("width"), int) else None,
            height=item.get("height") if isinstance(item.get("height"), int) else None,
        )
        attachments.append(attachment)
        segments.append(
            {
                "type": kind.value,
                "data": {
                    "file": url,
                    "name": attachment.filename,
                    "file_size": attachment.file_size,
                },
            }
        )
    return attachments, segments


def _scene(data: dict[str, Any], key: str) -> str | None:
    """``message_scene.ext`` holds ``key=value`` strings; values may contain ``=``."""
    for item in (data.get("message_scene") or {}).get("ext") or []:
        if isinstance(item, str) and item.startswith(key + "="):
            return item[len(key) + 1 :] or None
    return None


def normalize_message(
    event_type: str, data: dict[str, Any], *, self_id: str
) -> InboundMessage | None:
    """Project one message dispatch; the quote's REFIDX_ is resolved by the gateway."""
    if event_type not in _MESSAGE_EVENTS:
        return None
    author = data.get("author") or {}
    private = event_type == "C2C_MESSAGE_CREATE"
    # member_openid (group) and user_openid (C2C) are one value inside one Bot.
    sender = author.get("user_openid" if private else "member_openid") or author.get("id")
    group_id = None if private else data.get("group_openid") or data.get("group_id")
    if not sender or (not private and not group_id):
        return None
    content = str(data.get("content") or "")
    if event_type == "GROUP_AT_MESSAGE_CREATE":
        content = content.strip()
    content = _face_text(content)
    native = [item for item in data.get("mentions") or [] if isinstance(item, dict)]
    texts: list[str] = []
    segments: list[dict[str, Any]] = []
    mentions: list[MessageMention] = []
    length = 0

    def text(value: str) -> None:
        nonlocal length
        if value:
            texts.append(value)
            segments.append({"type": "text", "data": {"text": value}})
            length += len(value)

    cursor = 0
    for token in _MENTION_TOKEN.finditer(content):
        entry = next(
            (
                item
                for item in native
                if token[1]
                in {item.get(key) for key in ("id", "openid", "member_openid", "user_openid")}
            ),
            None,
        )
        if entry is None:
            continue
        text(content[cursor : token.start()])
        is_yuki = entry.get("is_you") is True
        target = (
            self_id
            if is_yuki
            else entry.get("member_openid")
            or entry.get("user_openid")
            or entry.get("openid")
            or token[1]
        )
        name = str(entry.get("username") or entry.get("nick") or "")
        mention = MessageMention(
            "yuki" if is_yuki else "member",
            len(segments),
            length,
            length,
            target_user_id=target,
            display_name=name,
        )
        mention = replace(mention, text_end=length + len(mention.text))
        mentions.append(mention)
        segments.append(
            {"type": "qq_official_mention", "data": {"id": token[1], "is_you": is_yuki}}
        )
        texts.append(mention.text)
        length = mention.text_end
        cursor = token.end()
    text(content[cursor:])
    body = "".join(texts)
    attachments, media_segments = _attachments(
        data.get("attachments") or [], source="current", first_index=len(segments)
    )
    segments.extend(media_segments)
    reply_text = None
    reply_attachments: list[MessageAttachment] = []
    reply_segments: list[dict[str, Any]] = []
    if data.get("message_type") == _QUOTE:
        # The quoted body and media arrive inside the event itself.
        elements = [item for item in data.get("msg_elements") or [] if isinstance(item, dict)]
        quoted = [_face_text(str(item.get("content") or "")).strip() for item in elements]
        reply_text = "\n".join(part for part in quoted if part)
        if reply_text:
            reply_segments.append({"type": "text", "data": {"text": reply_text}})
        for element in elements:
            found, found_segments = _attachments(
                element.get("attachments") or [], source="reply", first_index=len(reply_segments)
            )
            reply_attachments.extend(found)
            reply_segments.extend(found_segments)
    if not (body.strip() or attachments or reply_text or reply_attachments):
        return None
    is_bot = author.get("bot") is True
    timestamp = data.get("timestamp")
    message_id = str(data["id"])
    return InboundMessage(
        ingress_key=message_id,
        native_message_id=message_id,
        event_type=f"message:{QQ_OFFICIAL_PROVIDER_ID}",
        platform=QQ_OFFICIAL_PROVIDER_ID,
        bot_user_id=self_id,
        scope_type=ScopeType.PRIVATE if private else ScopeType.GROUP,
        group_id=group_id,
        sender=SenderIdentity(
            str(sender),
            nickname=str(author.get("username") or ""),
            nickname_known=bool(author.get("username")),
            is_bot=is_bot,
        ),
        text=body,
        raw_text=body,
        # A group @ event is itself the call; full-mode messages say so per mention.
        mentions_bot=not is_bot
        and (
            event_type == "GROUP_AT_MESSAGE_CREATE"
            or any(item.get("is_you") is True for item in native)
        ),
        mentions=tuple(mentions),
        mentioned_user_ids=tuple(
            dict.fromkeys(
                item.target_user_id
                for item in mentions
                if item.kind == "member" and item.target_user_id is not None
            )
        ),
        attachments=tuple(attachments),
        segments=tuple(segments),
        reply_text=reply_text,
        reply_attachments=tuple(reply_attachments),
        reply_segments=tuple(reply_segments),
        occurred_at=datetime.fromisoformat(timestamp).astimezone(UTC)
        if isinstance(timestamp, str) and timestamp
        else None,
        received_at=datetime.now(UTC),
    )


class QQOfficialSender:
    def __init__(self, gateway: QQOfficialGateway, message: InboundMessage) -> None:
        self.gateway, self.message = gateway, message
        self.bot = gateway.connection

    async def send(self, message: OutboundMessage) -> OutboundSendReceipt:
        if len(message.media) > 1:
            raise ValueError("use_separate_delivery_operations")
        media = message.media[0] if message.media else None
        receipt = await send_message(
            self.bot,
            TransportTarget(
                "space" if self.message.group_id else "person",
                self.message.group_id or self.message.sender.user_id,
            ),
            TransportMessage(
                message.text,
                reply_reference=message.reply_to_message_id,
                media=TransportMedia(media.kind.value, media.content, "attachment", media.mime_type)
                if media is not None
                else None,
            ),
            reply_to=self.message.native_message_id,
        )
        assert receipt.reference is not None
        return OutboundSendReceipt(receipt.reference, transport=QQ_OFFICIAL_PROVIDER_ID)


class QQOfficialGateway:
    def __init__(
        self,
        database: Database,
        registry: GatewayConnectionRegistry,
        router: PresenceRouter,
        processor: MessageProcessor,
        writer: ScopedEventLedgerUnitOfWork,
        client: QQOfficialClient,
        *,
        connector: Callable[..., Any] = connect,
        retry_delay: float = 1.0,
    ) -> None:
        self.database, self.registry, self.router = database, registry, router
        self.processor, self.writer, self.client = processor, writer, client
        self.connection = QQOfficialConnection(client, database)
        self.connector, self.retry_delay = connector, retry_delay
        self._task: asyncio.Task[None] | None = None
        self._turns: set[asyncio.Task[Any]] = set()
        self._session_id = ""
        self._sequence = 0
        self._intents = INTENT_GROUP_AND_C2C | INTENT_GROUP_MEMBERS | INTENT_INTERACTION
        self._connected = False
        self._last_success_at: datetime | None = None
        self._last_error_category: str | None = None

    @property
    def presence_id(self) -> str:
        return self.connection.presence_id

    async def preflight(self) -> None:
        await self.client.gateway_url()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="qq-official-gateway")

    async def close(self) -> None:
        async def shutdown() -> BaseException | None:
            failure: BaseException | None = None
            try:
                if self._task is not None:
                    self._task.cancel()
                    await asyncio.gather(self._task, return_exceptions=True)
            finally:
                try:
                    turns = tuple(self._turns)
                    for task in turns:
                        task.cancel()
                    await asyncio.gather(*turns, return_exceptions=True)
                    self._disconnected()
                    await self.client.close()
                except BaseException as cleanup:
                    failure = cleanup
            return failure

        # Shutdown owns the admitted turns through settlement before the database closes.
        failure, cancellation = await _complete(shutdown())
        if cancellation is not None:
            raise cancellation from failure
        if failure is not None:
            raise failure

    async def health(self) -> dict[str, object]:
        running = self._task is not None and not self._task.done()
        return {
            "enabled": True,
            "running": running,
            "healthy": running and self._connected and self._last_error_category is None,
            "last_success_at": self._last_success_at.isoformat()
            if self._last_success_at is not None
            else None,
            "last_error_category": self._last_error_category,
            "active_turns": len(self._turns),
        }

    def _disconnected(self) -> None:
        if self._connected:
            self._connected = False
            self.registry.disconnect(self.connection)

    async def _run(self) -> None:
        delay = self.retry_delay
        while True:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                category = (
                    f"close_{exc.rcvd.code}"
                    if isinstance(exc, ConnectionClosed) and exc.rcvd is not None
                    else exc.code
                    if isinstance(exc, QQOfficialAPIError)
                    else type(exc).__name__
                )
                self._last_error_category = category
                logger.warning("qq_official_session_ended category=%s", category)
            if self._connected:
                delay = self.retry_delay
            self._disconnected()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _session(self) -> None:
        url = await self.client.gateway_url()
        token = await self.client.access_token()
        async with self.connector(url, max_size=None, logger=_protocol_logger) as socket:
            hello = json.loads(await socket.recv())
            interval = hello["d"]["heartbeat_interval"] / 1000
            if self._session_id:
                opening = {
                    "op": 6,
                    "d": {
                        "token": f"QQBot {token}",
                        "session_id": self._session_id,
                        "seq": self._sequence,
                    },
                }
            else:
                opening = {
                    "op": 2,
                    "d": {
                        "token": f"QQBot {token}",
                        "intents": self._intents,
                        "shard": [0, 1],
                        "properties": {},
                    },
                }
            await socket.send(json.dumps(opening))
            heartbeat = asyncio.create_task(self._heartbeat(socket, interval))
            try:
                while True:
                    # Every heartbeat is acknowledged; two silent intervals mean the
                    # link is half-open even if writes still succeed.
                    frame = json.loads(await asyncio.wait_for(socket.recv(), 2 * interval))
                    self._last_success_at = datetime.now(UTC)
                    if frame.get("s"):
                        self._sequence = frame["s"]
                    if frame["op"] == 0:
                        await self._dispatch(frame.get("t"), frame.get("d") or {})
                    elif frame["op"] == 9:
                        self._session_id, self._sequence = "", 0
                        return
                    elif frame["op"] == 7:
                        return
            except ConnectionClosed as closed:
                code = closed.rcvd.code if closed.rcvd is not None else None
                if code == _INVALID_TOKEN:
                    self.client.invalidate_token(token)
                elif code in _INTENT_REJECTED:
                    # Drop the optional intents in turn; messages always stay.
                    self._intents &= ~(
                        INTENT_GROUP_MEMBERS
                        if self._intents & INTENT_GROUP_MEMBERS
                        else INTENT_INTERACTION
                    )
                raise
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat(self, socket: Any, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            await socket.send(json.dumps({"op": 1, "d": self._sequence or None}))

    async def _dispatch(self, event_type: str | None, data: dict[str, Any]) -> None:
        if event_type == "READY":
            self._session_id = data["session_id"]
            self.client.self_id = str(data["user"]["id"])
            await self._announce()
        elif event_type == "RESUMED":
            await self._announce()
        elif event_type in _MESSAGE_EVENTS:
            try:
                await self.receive(event_type, data)
            except Exception as exc:
                # A WebSocket event has no redelivery ACK; keep the session serving.
                logger.error("qq_official_admission_failed category=%s", type(exc).__name__)

    async def _announce(self) -> None:
        if not self.connection.presence_id:
            async with self.database.immediate_session() as session:
                presence = await create_presence(
                    session,
                    external_account_id=self.client.self_id,
                    platform=QQ_OFFICIAL_PROVIDER_ID,
                    now=datetime.now(UTC),
                )
                presence_id = presence.id
            self.connection.presence_id = presence_id
        self.registry.connect(
            self.connection,
            provider_id=QQ_OFFICIAL_PROVIDER_ID,
            presence_id=self.connection.presence_id,
        )
        self._connected = True
        self._last_error_category = None

    async def receive(self, event_type: str, data: dict[str, Any]) -> None:
        presence_id = self.connection.presence_id
        message_id = str(data["id"])
        # Full-message mode pushes Yuki's own messages back; their delivery records
        # already hold them as outbound events.
        if await is_outbound(self.database, presence_id, message_id):
            return
        message = normalize_message(event_type, data, self_id=self.client.self_id)
        if message is None:
            return
        await record_message(
            self.database,
            presence_id=presence_id,
            message_id=message_id,
            chat_kind="c2c" if message.group_id is None else "group",
            chat_id=message.group_id or message.sender.user_id,
            direction="inbound",
            ref_idx=_scene(data, "msg_idx"),
        )
        quoted = _scene(data, "ref_msg_idx") if data.get("message_type") == _QUOTE else None
        if quoted is not None:
            message = replace(
                message,
                reply_to_message_id=await message_for_ref(self.database, presence_id, quoted),
            )
        async with self.database.read_session() as session:
            receipt = await transport_receipt(
                session,
                presence_id=presence_id,
                scope=message.scope(),
                event_type=message.event_type,
                ingress_key=message.ingress_key,
            )
            original = await load_claimed_keeper(
                session, receipt.canonical_event_id if receipt is not None else None
            )
            binding = (
                await find_space_binding(
                    session, message.group_id, platform=QQ_OFFICIAL_PROVIDER_ID
                )
                if message.group_id is not None
                else None
            )
        if original is not None:
            if original.sender_user_id != message.sender.user_id:
                raise PermissionError("qq_official_source_conflict")
            return
        if message.group_id is not None and binding is None:
            # A group event from the platform gateway is itself proof of membership.
            async with self.database.immediate_session() as session:
                await create_space_binding(
                    session,
                    external_space_id=message.group_id,
                    name="",
                    enabled=True,
                    autonomous_enabled=True,
                    require_mention=False,
                    now=datetime.now(UTC),
                    platform=QQ_OFFICIAL_PROVIDER_ID,
                )
        acknowledged = asyncio.get_running_loop().create_future()
        acknowledged.add_done_callback(
            lambda result: result.exception() if not result.cancelled() else None
        )
        task = asyncio.create_task(
            self.processor.handle(
                message,
                QQOfficialSender(self, message),
                on_committed=lambda result: (
                    acknowledged.set_result(result) if not acknowledged.done() else None
                ),
            ),
            name=f"qq-official-message:{message_id}",
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
                logger.error("qq_official_processing_failed category=%s", type(error).__name__)

        task.add_done_callback(finished)
        await asyncio.shield(acknowledged)
