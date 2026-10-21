"""Shared sticker versions and explicit optional message illustration intent."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EmojiAsset(_FrozenModel):
    id: str
    platform: str
    sha256: str
    relative_path: str | None
    mime_type: str
    image_format: str
    byte_size: int
    width: int | None = None
    height: int | None = None
    frame_count: int | None = None
    animated: bool = False
    is_sticker: bool = False
    description: str = ""
    tags: tuple[str, ...] = ()
    ocr_text: str = ""
