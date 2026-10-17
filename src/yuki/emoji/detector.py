"""Deterministic OneBot-metadata candidate detection."""

from __future__ import annotations

from yuki.domain.messages import AttachmentKind, MessageAttachment
from yuki.emoji.models import EmojiCollectionMode

_EMOJI_SUBTYPES = frozenset(
    {
        "1",
        "2",
        "emoji",
        "market_face",
        "marketface",
        "sticker",
        "meme",
    }
)
_EMOJI_HINTS = ("表情", "贴纸", "sticker", "emoji", "marketface", "market_face")


class EmojiCandidateDetector:
    """Classify only whether media deserves asynchronous inspection."""

    def is_candidate(
        self,
        attachment: MessageAttachment,
        mode: EmojiCollectionMode,
    ) -> bool:
        if attachment.kind is not AttachmentKind.IMAGE:
            return False
        if mode is EmojiCollectionMode.ALL_IMAGES:
            return True
        explicit = bool(
            attachment.emoji_id
            or attachment.emoji_package_id
            or (attachment.sub_type or "").casefold() in _EMOJI_SUBTYPES
        )
        if mode is EmojiCollectionMode.METADATA_ONLY:
            return explicit
        label = " ".join(
            value.casefold()
            for value in (attachment.label, attachment.summary or "", attachment.sub_type or "")
            if value
        )
        return explicit or any(hint in label for hint in _EMOJI_HINTS)
