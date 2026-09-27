"""Optional bounded observations for management, separate from tools and commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import Field

from yuki_plugin_sdk.models import JsonValue, StrictModel

type JsonObject = dict[str, JsonValue]


class PluginObservationRequest(StrictModel):
    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=10, ge=1, le=20)


@dataclass(frozen=True, slots=True)
class PluginObservationContext:
    get_config: Callable[[str], Awaitable[JsonValue]]
    get_state: Callable[[str, str], Awaitable[JsonValue]]


@runtime_checkable
class ObservablePlugin(Protocol):
    async def observe(
        self, context: PluginObservationContext, request: PluginObservationRequest
    ) -> JsonObject: ...
