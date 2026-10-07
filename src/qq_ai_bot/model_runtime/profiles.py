"""Load validated model profiles and task routes from TOML."""

from __future__ import annotations

import logging
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelRoute,
    ModelTask,
)

if TYPE_CHECKING:
    from qq_ai_bot.config import Settings
    from qq_ai_bot.settings_domains import ModelRuntimeSettings


def model_profile_environment(settings: Settings | ModelRuntimeSettings) -> dict[str, str]:
    """Startup, CLI and management resolve the same public Settings aliases."""
    return {
        "LLM_BASE_URL": settings.llm_base_url,
        "LLM_MODEL": settings.llm_model,
        "LLM_REASONING_EFFORT": settings.llm_reasoning_effort.value
        if settings.llm_reasoning_effort
        else "",
        "LLM_FLASH_BASE_URL": settings.llm_flash_base_url,
        "LLM_FLASH_MODEL": settings.llm_flash_model,
    }


logger = logging.getLogger(__name__)

PROFILE_SCHEMA_VERSION = 3
RETIRED_MODEL_ROUTES = frozenset({"planner", "tool_selection"})
CURRENT_CONFIGURATION_HINT = "regenerate model_profiles.toml with qq-ai-bot-cli setup"


class ModelRuntimeConfigurationError(ValueError):
    """The profile file or compatibility configuration is unusable."""


class _ProfileDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[3]
    profiles: dict[str, dict[str, Any]]
    routes: dict[str, str]
    search_connection: str | None = None


class ModelProfileCatalog(BaseModel):
    """Immutable, fully validated model configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profiles: dict[str, ModelProfile]
    routes: dict[ModelTask, ModelRoute]
    search_connection: str | None = None

    @model_validator(mode="after")
    def _validate_routes(self) -> ModelProfileCatalog:
        if self.search_connection is not None and self.search_connection not in self.profiles:
            raise ValueError("search connection references an unknown model connection")
        missing = set(ModelTask).difference(self.routes)
        if missing:
            names = ", ".join(sorted(task.value for task in missing))
            raise ValueError(f"missing model routes: {names}")
        for task, route in self.routes.items():
            profile = self.profiles.get(route.profile_id)
            if profile is None:
                raise ValueError(
                    f"model route {task.value} references unknown profile {route.profile_id}"
                )
            unavailable = route.required_capabilities.difference(profile.capabilities)
            if unavailable:
                names = ", ".join(sorted(item.value for item in unavailable))
                raise ValueError(
                    f"model route {task.value} requires unsupported capabilities: {names}"
                )
        return self


_DEFAULT_REQUIREMENTS: dict[ModelTask, frozenset[ModelCapability]] = {
    ModelTask.CHAT_AGENT: frozenset({ModelCapability.TOOLS}),
    ModelTask.MEMORY_EXTRACTION: frozenset({ModelCapability.STRUCTURED_OUTPUT}),
    ModelTask.MEMORY_SELF_REFLECTION: frozenset({ModelCapability.STRUCTURED_OUTPUT}),
    ModelTask.MEMORY_CONSOLIDATION: frozenset({ModelCapability.STRUCTURED_OUTPUT}),
    ModelTask.MEMORY_DREAM: frozenset({ModelCapability.STRUCTURED_OUTPUT}),
    ModelTask.MEMORY_ATTRIBUTION: frozenset({ModelCapability.STRUCTURED_OUTPUT}),
    ModelTask.RELATIONSHIP_EVALUATION: frozenset({ModelCapability.STRUCTURED_OUTPUT}),
    ModelTask.EMOJI_REPLACEMENT: frozenset({ModelCapability.STRUCTURED_OUTPUT}),
    ModelTask.AUTOMATION_TEXT_GENERATION: frozenset(),
    ModelTask.AUTOMATION_AGENT: frozenset({ModelCapability.TOOLS}),
    ModelTask.PLUGIN_AGENT_SESSION: frozenset({ModelCapability.TOOLS}),
    ModelTask.UTILITY_STRUCTURED: frozenset({ModelCapability.STRUCTURED_OUTPUT}),
    ModelTask.CONVERSATION_COMPACTION: frozenset({ModelCapability.STRUCTURED_OUTPUT}),
}


def load_model_profile_catalog(
    path: Path, *, environment: Mapping[str, str] | None = None
) -> ModelProfileCatalog:
    """Load an explicit, complete v3 TOML model configuration."""
    if not path.is_file():
        raise ModelRuntimeConfigurationError(
            f"model profile configuration is missing: {path}; create model_profiles.toml with setup"
        )

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ModelRuntimeConfigurationError("cannot read model profile configuration") from exc
    return parse_model_profile_catalog(content, environment=environment)


def parse_model_profile_catalog(
    content: str, *, environment: Mapping[str, str] | None = None
) -> ModelProfileCatalog:
    """Validate the same document for startup and management, without file I/O."""
    try:
        raw = tomllib.loads(content)
        version = raw.get("schema_version", 1)
        if version != PROFILE_SCHEMA_VERSION:
            raise ModelRuntimeConfigurationError(
                f"model profile schema v{version} is no longer accepted; "
                f"{CURRENT_CONFIGURATION_HINT}"
            )
        document = _ProfileDocument.model_validate(raw)
        profiles = {
            profile_id: ModelProfile.model_validate(
                {
                    "id": profile_id,
                    **_resolve_profile_environment(payload, environment=environment),
                }
            )
            for profile_id, payload in document.profiles.items()
        }
        raw_routes = dict(document.routes)
        retired = RETIRED_MODEL_ROUTES.intersection(raw_routes)
        if retired:
            names = ", ".join(sorted(retired))
            raise ModelRuntimeConfigurationError(
                f"retired model routes remain ({names}); {CURRENT_CONFIGURATION_HINT}"
            )
        routes = {
            ModelTask(task_name): ModelRoute(
                task=ModelTask(task_name),
                profile_id=profile_id,
                required_capabilities=_DEFAULT_REQUIREMENTS[ModelTask(task_name)],
            )
            for task_name, profile_id in raw_routes.items()
        }
        return ModelProfileCatalog(
            profiles=profiles,
            routes=routes,
            search_connection=document.search_connection,
        )
    except ModelRuntimeConfigurationError:
        raise
    except (OSError, tomllib.TOMLDecodeError, ValidationError, KeyError, ValueError) as exc:
        raise ModelRuntimeConfigurationError(f"invalid model profile configuration: {exc}") from exc


def _resolve_profile_environment(
    payload: dict[str, Any],
    *,
    environment: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Resolve public endpoint/model indirections while leaving API keys unread."""

    resolved = dict(payload)
    for value_name, env_name_key in (("base_url", "base_url_env"), ("model", "model_env")):
        env_name = resolved.pop(env_name_key, None)
        if env_name is None:
            continue
        if not isinstance(env_name, str) or not env_name:
            raise ValueError(f"{env_name_key} must name an environment variable")
        value = (environment or {}).get(env_name) or os.environ.get(env_name)
        if not value:
            raise ValueError(f"environment variable {env_name} is required")
        resolved[value_name] = value

    reasoning_effort_env = resolved.pop("reasoning_effort_env", None)
    if reasoning_effort_env is not None:
        if not isinstance(reasoning_effort_env, str) or not reasoning_effort_env:
            raise ValueError("reasoning_effort_env must name an environment variable")
        value = (environment or {}).get(reasoning_effort_env) or os.environ.get(
            reasoning_effort_env
        )
        if value:
            resolved["reasoning_effort"] = value

    thinking_mode = resolved.pop("thinking_mode", None)
    if thinking_mode is not None:
        modes = {"configurable": None, "disabled": False, "enabled": True}
        try:
            resolved["thinking_enabled"] = modes[thinking_mode]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"unknown thinking_mode: {thinking_mode}") from exc
    return resolved
