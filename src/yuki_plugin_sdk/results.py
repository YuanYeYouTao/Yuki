"""Structured results returned across the Plugin API boundary."""

from __future__ import annotations

from pydantic import Field, model_validator

from yuki_plugin_sdk.models import JsonValue, MediaArtifactHandle, StrictModel


class PluginResult(StrictModel):
    ok: bool = True
    data: dict[str, JsonValue] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    detail: str = Field(default="", max_length=1_000)
    # Explicit selection of owned Host handles, never pixels or a bearer read grant.
    media_artifacts: tuple[MediaArtifactHandle, ...] = Field(
        default=(), max_length=16, exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def _consistent(self) -> PluginResult:
        if self.ok and self.error_code is not None:
            raise ValueError("successful result cannot contain error_code")
        if not self.ok and self.error_code is None:
            raise ValueError("failed result requires error_code")
        return self


class ToolResult(PluginResult):
    """Plugin tool output; the Host treats data as untrusted model context."""

    # Conditional tools can inspect or disambiguate before they perform their
    # declared SEND/MUTATE effect. ``None`` delegates commit classification to
    # the declared tool risk; explicit ``False`` marks a harmless intermediate
    # result eligible for a corrected follow-up call.
    mutation_committed: bool | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class CommandResult(PluginResult):
    text: str = Field(default="", max_length=12_000)
