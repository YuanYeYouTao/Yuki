"""Host-only media paired with a tool receipt; never encoded in tool JSON."""

from __future__ import annotations

from typing import Any

from qq_ai_bot.domain.messages import ChatImage


class PreparedMediaData(dict[str, Any]):
    """Service result with immutable prepared pixels outside its public mapping."""

    def __init__(self, data: dict[str, Any], images: tuple[ChatImage, ...]) -> None:
        super().__init__(data)
        self.images = images


class MediaResultText(str):
    """Compatible textual receipt carrying Host-selected pixels to the Runner."""

    images: tuple[ChatImage, ...]

    def __new__(cls, text: str, images: tuple[ChatImage, ...] = ()) -> MediaResultText:
        value = super().__new__(cls, text)
        value.images = images
        return value


def result_images(value: object) -> tuple[ChatImage, ...]:
    return value.images if isinstance(value, (PreparedMediaData, MediaResultText)) else ()
