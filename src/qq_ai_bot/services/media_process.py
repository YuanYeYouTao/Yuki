"""Run the existing local media parsers with bounded process lifetime."""

from __future__ import annotations

import asyncio


class MediaProcessError(RuntimeError):
    """The selected parser exited unsuccessfully; its caller owns the media kind."""


async def run_media_process(*args: str, env: dict[str, str] | None = None) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env
    )
    try:
        async with asyncio.timeout(15):
            output, _ = await process.communicate()
        if process.returncode:
            raise MediaProcessError("media parser process failed")
        return output
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
