"""Plain OneBot message sender."""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Any, cast

from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment

from qq_ai_bot.domain.messages import AttachmentKind, OutboundMessage, OutboundSendReceipt

logger = logging.getLogger(__name__)


class OneBotSendError(RuntimeError):
    """Sanitized outbound transport failure."""


class OneBotSender:
    """Send plain text, optionally quoting one backend-validated message."""

    def __init__(self, bot: Bot, event: MessageEvent) -> None:
        self._bot = bot
        self._event = event

    @property
    def bot(self) -> Bot:
        return self._bot

    async def send(self, message: OutboundMessage) -> OutboundSendReceipt:
        """Send via the ingress bot; failover only to the same Presence connection."""

        try:
            return await self._deliver(message)
        except OneBotSendError:
            replacement = self._same_presence_bot()
            if replacement is None or replacement is self._bot:
                raise
            self._bot = replacement
            return await self._deliver(message)

    async def _deliver(self, message: OutboundMessage) -> OutboundSendReceipt:
        try:
            if not message.media and message.reply_to_message_id is None:
                if not message.text:
                    raise ValueError("outbound message is empty")
                result = await self._bot.send(
                    event=self._event,
                    message=MessageSegment.text(message.text),
                )
                return parse_onebot_send_receipt(result)
            payload = Message()
            if message.reply_to_message_id:
                if not message.reply_to_message_id.isdigit():
                    raise ValueError("reply target must be a numeric OneBot message ID")
                payload += MessageSegment.reply(int(message.reply_to_message_id))
            if message.text:
                payload += MessageSegment.text(message.text)
            for media in message.media:
                if media.kind is AttachmentKind.IMAGE:
                    content = media.content
                    encoded = base64.b64encode(content).decode("ascii")
                    segment = MessageSegment.image(file=f"base64://{encoded}")
                    if media.emoji_id:
                        segment.data["sub_type"] = 1
                    payload += segment
                elif media.kind is AttachmentKind.AUDIO:
                    if media.local_path is None:
                        raise ValueError("audio media is missing its local file")
                    content = await asyncio.to_thread(Path(media.local_path).read_bytes)
                    encoded = base64.b64encode(content).decode("ascii")
                    payload += MessageSegment.record(file=f"base64://{encoded}")
                    del content, encoded
                else:
                    raise ValueError("unsupported outbound media kind")
            if not payload:
                raise ValueError("outbound message is empty")
            result = await self._bot.send(event=self._event, message=payload)
            return parse_onebot_send_receipt(result)
        except asyncio.CancelledError:
            raise
        except OneBotSendError:
            raise
        except Exception as exc:
            logger.error("onebot_send_failed exception_category=%s", type(exc).__name__)
            raise OneBotSendError("OneBot send failed") from exc

    def _same_presence_bot(self) -> Bot | None:
        from qq_ai_bot.gateway.registry import RegistryClosed, process_registry

        registry = process_registry()
        if registry is None:
            return None
        try:
            current = registry.resolve_by_handle(self._bot)
        except RegistryClosed:
            try:
                current = registry.resolve_account("qq", str(self._bot.self_id))
            except RegistryClosed:
                return None
        presence_id = current.snapshot.presence_id
        if not presence_id:
            return None
        try:
            resolved = registry.resolve_active(presence_id)
        except RegistryClosed:
            return None
        candidate = resolved.bot
        if candidate is self._bot or candidate is None:
            return None
        return cast(Bot, candidate)

    async def call_api(self, action: str, params: dict[str, Any]) -> Any:
        """Call one exact OneBot action through the existing reverse WebSocket."""

        try:
            return await self._bot.call_api(action, **params)
        except Exception as exc:
            logger.error(
                "onebot_api_failed action=%s exception_category=%s",
                action,
                type(exc).__name__,
            )
            raise OneBotSendError("OneBot API call failed") from exc


def parse_onebot_send_receipt(result: object) -> OutboundSendReceipt:
    """Normalize supported OneBot send results into one strict receipt."""

    candidate: object | None = None
    if isinstance(result, (str, int)) and not isinstance(result, bool):
        candidate = result
    elif isinstance(result, dict):
        candidate = result.get("message_id") if "message_id" in result else result.get("id")
    else:
        candidate = getattr(result, "message_id", None)
    if isinstance(candidate, bool) or not isinstance(candidate, (str, int)):
        raise OneBotSendError("OneBot send did not return a message ID")
    normalized = str(candidate).strip()
    if not normalized:
        raise OneBotSendError("OneBot send returned an empty message ID")
    return OutboundSendReceipt(platform_message_id=normalized, transport="onebot")
