"""Ephemeral platform activity bound to one actual connection and target."""

from typing import Protocol


class PlatformActivity(Protocol):
    refresh_interval_seconds: float

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
