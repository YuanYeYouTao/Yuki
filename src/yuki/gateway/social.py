"""Platform operations below canonical ownership and durable effect receipts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class TransportTarget:
    kind: str
    external_id: str
    thread_id: str | None = None


@dataclass(frozen=True, slots=True)
class TransportMention:
    external_id: str
    display_name: str
    text_start: int
    text_end: int
    account_id: str | None = None
    assignment_id: str | None = None
    person_id: str | None = None
    presence_id: str | None = None
    kind: str = "member"


@dataclass(frozen=True, slots=True)
class TransportMedia:
    kind: str
    content: bytes
    filename: str
    mime_type: str
    emoji: bool = False


@dataclass(frozen=True, slots=True)
class TransportMessage:
    text: str = ""
    mentions: tuple[TransportMention, ...] = ()
    reply_reference: str | None = None
    media: TransportMedia | None = None


def transport_payload(message: TransportMessage) -> dict[str, Any]:
    """Persist actual layout and targets; immutable media bytes stay in their artifact."""
    return {
        "text": message.text,
        "mentions": [asdict(item) for item in message.mentions],
        "reply_reference": message.reply_reference,
        "media": message.media is not None,
    }


def prepared_transport(
    payload: dict[str, Any], media: TransportMedia | None = None
) -> TransportMessage:
    return TransportMessage(
        payload["text"],
        tuple(TransportMention(**item) for item in payload["mentions"]),
        payload["reply_reference"],
        media if payload["media"] else None,
    )


def split_transport_message(
    message: TransportMessage, limit: int, *, following_limit: int | None = None
) -> tuple[TransportMessage, ...]:
    """Slice actual text once, keeping each native mention in its physical part."""
    if len(message.text) <= limit:
        return (message,)
    parts: list[TransportMessage] = []
    start = mention_index = 0
    while start < len(message.text):
        end = min(start + (following_limit or limit) if parts else start + limit, len(message.text))
        next_index = mention_index
        while next_index < len(message.mentions):
            mention = message.mentions[next_index]
            if mention.text_start >= end:
                break
            if mention.text_end > end:
                end = mention.text_start
                if end == start:
                    raise ValueError("mention_exceeds_platform_limit")
                break
            next_index += 1
        parts.append(
            replace(
                message,
                text=message.text[start:end],
                mentions=tuple(
                    replace(
                        item, text_start=item.text_start - start, text_end=item.text_end - start
                    )
                    for item in message.mentions[mention_index:next_index]
                ),
                reply_reference=message.reply_reference if not parts else None,
                media=message.media if not parts else None,
            )
        )
        start, mention_index = end, next_index
    return tuple(parts)


@dataclass(frozen=True, slots=True)
class TransportReceipt:
    reference: str | None
    remote_confirmed: bool = True


class TransportEffectError(Exception):
    """A send adapter's own boundary between preparation and a message call."""

    def __init__(self, category: str, *, uncertain: bool) -> None:
        super().__init__(category)
        self.category = category
        self.uncertain = uncertain


class PlatformSocialOperations(Protocol):
    def native_mention(self, text: str, offset: int) -> TransportMention | None:
        """Recognize protocol addresses after local names have been resolved."""
        ...

    def plan_send(self, message: TransportMessage) -> tuple[TransportMessage, ...]:
        """Pure protocol layout. Each returned item needs its own Social receipt."""
        ...

    async def send(
        self, handle: object, target: TransportTarget, message: TransportMessage
    ) -> TransportReceipt: ...

    async def recall(self, handle: object, target: TransportTarget, reference: str) -> None: ...

    async def is_member(self, handle: object, space: str, account: str) -> bool: ...
