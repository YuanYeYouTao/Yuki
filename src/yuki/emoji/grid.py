"""Build deterministic numbered preview grids for visual candidate selection."""

from __future__ import annotations

import io
import math
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont, ImageOps

from yuki.emoji.retriever import RankedEmoji
from yuki.emoji.storage import EmojiStorage


@dataclass(frozen=True, slots=True)
class EmojiGrid:
    content: bytes
    mapping: tuple[str, ...]
    columns: int
    rows: int


class EmojiGridBuilder:
    def __init__(self, storage: EmojiStorage, *, cell_size: int = 256) -> None:
        if cell_size <= 32:
            raise ValueError("emoji grid cell_size must exceed 32 pixels")
        self._storage = storage
        self._cell_size = cell_size

    def build(self, candidates: tuple[RankedEmoji, ...]) -> EmojiGrid:
        if not candidates:
            raise ValueError("emoji grid requires at least one candidate")
        columns = math.ceil(math.sqrt(len(candidates)))
        rows = math.ceil(len(candidates) / columns)
        canvas = Image.new("RGB", (columns * self._cell_size, rows * self._cell_size), "white")
        draw = ImageDraw.Draw(canvas)
        font = ImageFont.load_default(size=24)
        mapping: list[str] = []
        for index, candidate in enumerate(candidates, start=1):
            preview_path = candidate.asset.preview_relative_path or candidate.asset.relative_path
            content = self._storage.read(preview_path)
            with Image.open(io.BytesIO(content)) as image:
                image.seek(0)
                tile = ImageOps.contain(
                    image.convert("RGB"),
                    (self._cell_size - 16, self._cell_size - 16),
                    Image.Resampling.LANCZOS,
                )
            column = (index - 1) % columns
            row = (index - 1) // columns
            left = column * self._cell_size + (self._cell_size - tile.width) // 2
            top = row * self._cell_size + (self._cell_size - tile.height) // 2
            canvas.paste(tile, (left, top))
            draw.rectangle(
                (
                    column * self._cell_size + 4,
                    row * self._cell_size + 4,
                    column * self._cell_size + 44,
                    row * self._cell_size + 38,
                ),
                fill="black",
            )
            draw.text(
                (column * self._cell_size + 12, row * self._cell_size + 7),
                str(index),
                fill="white",
                font=font,
            )
            mapping.append(candidate.asset.id)
        output = io.BytesIO()
        canvas.save(output, format="PNG", optimize=True)
        return EmojiGrid(output.getvalue(), tuple(mapping), columns, rows)
