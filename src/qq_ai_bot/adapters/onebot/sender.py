"""Plain OneBot message sender."""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment

from qq_ai_bot.domain.messages import AttachmentKind, OutboundMessage, OutboundSendReceipt

logger = logging.getLogger(__name__)


class OneBotSendError(RuntimeError):
    """Sanitized outbound transport failure."""


class OneBotRouteSender:
    """Deliver a persisted message on one already verified Presence connection.

    The application owns source checks, effect gates and durable receipts. This
    adapter only translates the message; it never selects a replacement route.
    """

    def __init__(self, bot: object, *, group: bool, target_id: str) -> None:
        self._route_bot = bot
        self._group = group
        self._target_id = target_id

    async def send(self, message: OutboundMessage) -> OutboundSendReceipt:
        call_api = getattr(self._route_bot, "call_api", None)
        if not callable(call_api):
            raise ValueError("work_gateway_unavailable")
        payload: list[dict[str, Any]] = []
        if message.reply_to_message_id:
            payload.append({"type": "reply", "data": {"id": message.reply_to_message_id}})
        if message.text:
            payload.append({"type": "text", "data": {"text": message.text}})
        for media in message.media:
            if media.kind is not AttachmentKind.IMAGE:
                raise ValueError("unsupported_persisted_delivery_media")
            payload.append(
                {
                    "type": "image",
                    "data": {"file": "base64://" + base64.b64encode(media.content).decode("ascii")},
                }
            )
        response = await call_api(
            "send_group_msg" if self._group else "send_private_msg",
            **{
                "group_id" if self._group else "user_id": int(self._target_id),
                "message": payload,
            },
        )
        return parse_onebot_send_receipt(response)


class OneBotSender:
    """Send plain text, optionally quoting one backend-validated message."""

    def __init__(self, bot: Bot, event: MessageEvent) -> None:
        self._bot = bot
        self._event = event

    @property
    def bot(self) -> Bot:
        return self._bot

    async def send(self, message: OutboundMessage) -> OutboundSendReceipt:
        """Send through the original ingress connection."""

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
            if message.reply_to_message_id is not None:
                from qq_ai_bot.adapters.onebot.message_id import parse_message_id

                payload += MessageSegment.reply(parse_message_id(message.reply_to_message_id))
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
