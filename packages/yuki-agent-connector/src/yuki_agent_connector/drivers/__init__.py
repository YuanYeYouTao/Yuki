"""Native drivers are ordinary modules, not another plugin host."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ..config import Instance

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


class DriverError(Exception):
    def __init__(
        self, code: str, message: str, details: Any = None, *, uncertain: bool = False
    ) -> None:
        super().__init__(message)
        self.code, self.details = code, details
        self.uncertain = uncertain


class Driver(Protocol):
    spec: Instance

    @property
    def capabilities(self) -> list[str]: ...

    async def start(self) -> None: ...
    async def execute(self, command: dict[str, Any]) -> dict[str, Any]: ...
    async def recover(self, command: dict[str, Any], result: dict[str, Any]) -> None: ...
    async def stop(self) -> None: ...


def make_driver(spec: Instance, emit: Emit) -> Driver:
    if spec.driver == "codex":
        from .codex import CodexDriver

        return CodexDriver(spec, emit)
    if spec.driver == "claude":
        from .claude import ClaudeDriver

        return ClaudeDriver(spec, emit)
    if spec.driver == "acp":
        from .acp import ACPDriver

        return ACPDriver(spec, emit)
    raise ValueError(f"Unknown native driver: {spec.driver}")
