"""Platform operations below canonical ownership and durable effect receipts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TransportTarget:
    kind: str
    external_id: str
    thread_id: str | None = None


@dataclass(frozen=True, slots=True)
class TransportMention:
    external_id: str
    display_name: str


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


@dataclass(frozen=True, slots=True)
class TransportReceipt:
    reference: str | None


class PlatformSocialOperations(Protocol):
    # QQ uploads have a separate caption effect; App and Telegram deliver it atomically.
    file_caption_separate: bool

    def plan_send(self, message: TransportMessage) -> tuple[TransportMessage, ...]:
        """Pure protocol layout. Each returned item needs its own Social receipt."""
        ...

    async def send(
        self, handle: object, target: TransportTarget, message: TransportMessage
    ) -> TransportReceipt: ...

    async def recall(self, handle: object, target: TransportTarget, reference: str) -> None: ...

    async def is_member(self, handle: object, space: str, account: str) -> bool: ...
