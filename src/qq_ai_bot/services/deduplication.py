"""Durable event idempotency helpers."""

from __future__ import annotations

import hashlib

from qq_ai_bot.domain.messages import InboundMessage


def build_event_key(message: InboundMessage, conversation_key: str) -> str:
    """Hash event type, message id, and conversation identity into a stable key."""

    material = f"{message.event_type}\x1f{message.message_id}\x1f{conversation_key}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
