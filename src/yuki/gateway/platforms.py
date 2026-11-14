"""One resident owner of credential-configured gateways, keyed by platform.

A saved change is checked with the new credentials before the running gateway is
touched; the old one closes (leaving the Registry) before its successor starts.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

from yuki.admin.config_files import ConfigFileError
from yuki.admin.platform_files import (
    PlatformBundle,
    PlatformConfig,
    load_platforms,
    write_platforms,
)
from yuki.gateway.health import GatewayHealthSource

logger = logging.getLogger(__name__)


class PlatformGateway(Protocol):
    async def preflight(self) -> None: ...

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def health(self) -> dict[str, object]: ...


PlatformGatewayFactory = Callable[[PlatformConfig], PlatformGateway]


class PlatformPreflightError(Exception):
    """The new credentials were refused; only safe protocol facts are kept."""

    def __init__(self, error: Exception) -> None:
        code = getattr(error, "code", None)
        number = getattr(error, "error_code", None)
        self.details: dict[str, str | int] = {
            "error_category": code if isinstance(code, str) else type(error).__name__
        }
        if type(number) is int:
            self.details["error_code"] = number
        super().__init__(self.details["error_category"])


async def _disabled() -> dict[str, object]:
    return {"enabled": False, "running": False, "healthy": None}


class PlatformGateways:
    def __init__(self, path: Path, health_sources: dict[str, GatewayHealthSource]) -> None:
        self.path = path
        self.health_sources = health_sources
        self._factories: dict[str, PlatformGatewayFactory] = {}
        self._running: dict[str, PlatformGateway] = {}
        self._lock = asyncio.Lock()

    def register(self, platform: str, factory: PlatformGatewayFactory) -> None:
        self._factories[platform] = factory

    def apply_mode(self, platform: str) -> Literal["hot", "restart"]:
        return "hot" if platform in self._factories else "restart"

    async def read(self) -> PlatformBundle:
        return await asyncio.to_thread(load_platforms, self.path)

    async def start(self) -> None:
        async with self._lock:
            bundle = await self.read()
            for platform, config in bundle.platforms.items():
                factory = self._factories.get(platform)
                if factory is not None:
                    await self._replace(platform, factory(config) if config.enabled else None)

    async def close(self) -> None:
        async with self._lock:
            failures: list[BaseException] = []
            for platform in tuple(self._running):
                try:
                    await self._replace(platform, None)
                except BaseException as exc:
                    failures.append(exc)
            if failures:
                raise failures[0]

    async def apply(
        self,
        expected_revision: int,
        platform: str,
        *,
        enabled: bool,
        settings: Mapping[str, Any],
        secrets: Mapping[str, str],
    ) -> tuple[int, bool]:
        """Return the saved revision and whether the running gateway now uses it."""
        async with self._lock:
            original = await self.read()
            if original.revision != expected_revision:
                raise ConfigFileError("version_conflict")
            updated = original.changed(
                platform, enabled=enabled, settings=settings, secrets=secrets
            )
            factory = self._factories.get(platform)
            successor = factory(updated.platforms[platform]) if factory and enabled else None
            if successor is not None:
                try:
                    await successor.preflight()
                except BaseException as exc:
                    await successor.close()
                    if not isinstance(exc, Exception):
                        raise
                    logger.warning(
                        "platform_preflight_failed platform=%s category=%s",
                        platform,
                        type(exc).__name__,
                    )
                    raise PlatformPreflightError(exc) from None
            return await _settle(self._persist(original, updated, platform, factory, successor))

    async def _persist(
        self,
        original: PlatformBundle,
        updated: PlatformBundle,
        platform: str,
        factory: PlatformGatewayFactory | None,
        successor: PlatformGateway | None,
    ) -> tuple[int, bool]:
        try:
            revision = await asyncio.to_thread(write_platforms, self.path, original, updated)
        except BaseException:
            if successor is not None:
                await successor.close()
            raise
        if factory is None:
            return revision, False
        await self._replace(platform, successor)
        return revision, True

    async def _replace(self, platform: str, successor: PlatformGateway | None) -> None:
        previous = self._running.pop(platform, None)
        self.health_sources[platform] = _disabled
        if previous is not None:
            try:
                await previous.close()
            except BaseException:
                if successor is not None:
                    await successor.close()
                raise
        if successor is None:
            return
        await successor.start()
        self._running[platform] = successor
        self.health_sources[platform] = successor.health


async def _settle[T](work: Awaitable[T]) -> T:
    """File replacement and gateway handover finish even if the caller is cancelled."""
    task = asyncio.ensure_future(work)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise
