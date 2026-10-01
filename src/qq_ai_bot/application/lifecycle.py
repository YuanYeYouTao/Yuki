"""Ordered lifecycle management without a service locator."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

AsyncHook = Callable[[], Awaitable[Any]]
HealthHook = Callable[[], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class LifecycleEntry:
    name: str
    start: AsyncHook | None = None
    close: AsyncHook | None = None
    health: HealthHook | None = None


class LifecycleRegistry:
    """Start in registration order and close in exact reverse order."""

    def __init__(self) -> None:
        self._entries: list[LifecycleEntry] = []
        self._names: set[str] = set()
        self._started = False

    def register(
        self,
        name: str,
        *,
        start: AsyncHook | None = None,
        close: AsyncHook | None = None,
        health: HealthHook | None = None,
    ) -> None:
        if self._started:
            raise RuntimeError("cannot register resources after lifecycle start")
        if name in self._names:
            raise ValueError(f"duplicate lifecycle resource: {name}")
        self._entries.append(LifecycleEntry(name, start, close, health))
        self._names.add(name)

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("application lifecycle has already started")
        started: list[LifecycleEntry] = []
        try:
            for entry in self._entries:
                started.append(entry)
                if entry.start is not None:
                    await entry.start()
        except BaseException as start_error:
            close_errors = await self._close_entries(started)
            if close_errors:
                if not isinstance(start_error, Exception):
                    for close_error in close_errors:
                        start_error.add_note(
                            f"startup rollback failed: {type(close_error).__name__}"
                        )
                    raise
                raise BaseExceptionGroup(
                    "application start and rollback failed",
                    [start_error, *close_errors],
                ) from start_error
            raise
        self._started = True

    async def close(self) -> None:
        if not self._started:
            return
        close_errors = await self._close_entries(self._entries)
        self._started = False
        if close_errors:
            cancellation = next(
                (error for error in close_errors if not isinstance(error, Exception)), None
            )
            if cancellation is not None:
                for error in close_errors:
                    if error is not cancellation:
                        cancellation.add_note(f"shutdown failed: {type(error).__name__}")
                raise cancellation
            raise BaseExceptionGroup("application shutdown failed", close_errors)

    async def health(self) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for entry in self._entries:
            if entry.health is not None:
                try:
                    results[entry.name] = await entry.health()
                except Exception as exc:
                    results[entry.name] = {
                        "ok": False,
                        "error_category": type(exc).__name__,
                    }
        return results

    @staticmethod
    async def _close_entries(entries: list[LifecycleEntry]) -> list[BaseException]:
        errors: list[BaseException] = []
        for entry in reversed(entries):
            if entry.close is None:
                continue
            try:
                await entry.close()
            except BaseException as exc:
                errors.append(exc)
        return errors

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self._entries)
