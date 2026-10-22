"""Line framing for the existing Manager JSON transport."""

from __future__ import annotations

import asyncio


async def read_protocol_line(reader: asyncio.StreamReader) -> bytes:
    """Read one original frame; the stream buffer is not a message-size policy."""
    chunks: list[bytes] = []
    while True:
        try:
            chunks.append(await reader.readuntil(b"\n"))
            return b"".join(chunks)
        except asyncio.LimitOverrunError as exc:
            chunks.append(await reader.readexactly(exc.consumed))
        except asyncio.IncompleteReadError as exc:
            if chunks or exc.partial:
                raise ValueError("incomplete protocol frame") from exc
            return b""
