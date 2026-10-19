"""Explicit local model configuration for benchmark processes."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tomllib
from pathlib import Path
from typing import Any

import tomlkit
from dotenv import dotenv_values
from pydantic import ValidationError

from yuki.config import Settings
from yuki.model_runtime.models import ModelTask

ROOT = Path(__file__).resolve().parents[1]


def load_settings(config_path: str | Path, output_dir: Path) -> Settings:
    """Read one explicit configuration; never import a production database or workspace."""
    path = Path(config_path).expanduser().resolve()
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    values = dict(config.get("settings", {}))
    values.setdefault("llm_provider", "deepseek")
    values.setdefault("llm_base_url", "https://api.deepseek.com")
    values.setdefault("llm_reasoning_effort", "high")
    key_name = config.get("api_key_env", "LLM_API_KEY")
    local_env = {}
    if config.get("env_file"):
        env_path = Path(config["env_file"]).expanduser()
        if not env_path.is_absolute():
            env_path = path.parent / env_path
        local_env = dotenv_values(env_path)
    key = os.environ.get(key_name) or local_env.get(key_name)
    if not key:
        raise ValueError(f"benchmark API key is missing: {key_name}")
    directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    profile = {
        "provider": values.get("llm_provider", "deepseek"),
        "protocol": config.get("protocol", "chat_completions"),
        "base_url": values.get("llm_base_url", "https://api.deepseek.com"),
        "api_key_env": "LLM_API_KEY",
        "model": values["llm_model"],
        "timeout_seconds": values.get("llm_timeout_seconds", 120),
        "max_retries": values.get("llm_max_retries", 2),
        "default_temperature": values.get("llm_temperature", 0.7),
        "default_max_output_tokens": values.get("llm_max_output_tokens", 8192),
        "thinking_mode": "enabled" if values.get("llm_thinking_enabled", True) else "disabled",
        "reasoning_effort": values.get("llm_reasoning_effort", "high"),
        "capabilities": ["tools", "reasoning", "long_context", "image_input", "structured_output"],
    }
    profile.update(config.get("profile", {}))
    profile["api_key_env"] = "LLM_API_KEY"
    for setting, field in (
        ("llm_model", "model"),
        ("llm_provider", "provider"),
        ("llm_base_url", "base_url"),
    ):
        values[setting] = profile[field]
    # Use the native v3 profile loader, including its vendor/protocol validation.
    profiles_path = directory / "model_profiles.toml"
    profiles_path.write_text(
        tomlkit.dumps(
            {
                "schema_version": 3,
                "profiles": {"benchmark": profile},
                "routes": {task.value: "benchmark" for task in ModelTask},
            }
        ),
        encoding="utf-8",
    )
    values.update(
        llm_api_key=key,
        database_url=f"sqlite+aiosqlite:///{directory / 'yuki.sqlite3'}",
        workspace_directory=directory / "artifacts",
        conversation_media_cache_directory=directory / "media",
        social_transfer_directory=directory / "transfer",
        model_profiles_file=profiles_path,
        bot_persona_file=ROOT / "config/persona.md",
        system_prompt_file=ROOT / "config/system_prompt.example.md",
        runtime_work_enabled=True,
        code_mode_enabled=False,
        subagents_enabled=False,
        plugin_system_enabled=False,
        automation_enabled=False,
        web_enabled=False,
        web_mode="disabled",
    )
    try:
        return Settings(_env_file=None, **values)
    except ValidationError as exc:
        # Settings validation may include its whole input mapping, including the local API key.
        fields = ", ".join(
            ".".join(map(str, item["loc"])) or "settings"
            for item in exc.errors(include_input=False)
        )
        raise ValueError(f"Invalid benchmark settings: {fields}") from None


def public_settings(settings: Settings) -> dict[str, Any]:
    """Stable experiment identity without credentials, local paths or authorization headers."""
    from yuki.model_runtime.profiles import load_model_profile_catalog, model_profile_environment

    catalog = load_model_profile_catalog(
        settings.model_profiles_file,
        environment=model_profile_environment(settings),
        web_mode=settings.web.mode,
        search_backend=settings.web.web_search_backend,
    )
    profiles = {}
    for name, profile in catalog.profiles.items():
        profiles[name] = profile.model_dump(mode="json", exclude={"headers", "api_key_env"})
        profiles[name]["capabilities"] = sorted(profiles[name]["capabilities"])
        if math.isinf(profile.timeout_seconds):
            profiles[name]["timeout_seconds"] = None
        profiles[name]["headers_sha256"] = hashlib.sha256(
            json.dumps(profile.headers, sort_keys=True).encode()
        ).hexdigest()
    routes = catalog.model_dump(mode="json")["routes"]
    for route in routes.values():
        route["required_capabilities"] = sorted(route["required_capabilities"])
    per_trial = {
        "database_url",
        "workspace_directory",
        "conversation_media_cache_directory",
        "social_transfer_directory",
        "model_profiles_file",
        "bot_persona_file",
        "system_prompt_file",
        "yuki_persona_file",
    }
    effective = {
        key: value
        for key, value in settings.model_dump(mode="json").items()
        if key not in per_trial
        and not any(
            secret in key
            for secret in (
                "api_key",
                "access_token",
                "device_token",
                "password",
                "secret",
                "bearer",
            )
        )
    }
    if math.isinf(settings.llm_timeout_seconds):
        effective["llm_timeout_seconds"] = None
    public = {
        "settings": effective,
        "profiles": profiles,
        "routes": routes,
        "agent": settings.conversation.model_dump(
            mode="json",
            include={
                "agent_max_tool_calls",
                "agent_max_model_requests",
                "agent_tool_result_max_characters",
            },
        ),
        "compact_trigger_tokens": settings.context_compact_trigger_tokens,
        "code_mode": settings.code_mode_enabled,
        "subagents": settings.subagents_enabled,
        "benchmark_runtime_limits": {"model_requests": None, "tool_calls": None},
        "web_mode": settings.web.mode.value,
        "persona_sha256": hashlib.sha256(settings.bot_persona.encode()).hexdigest(),
        "system_sha256": hashlib.sha256(settings.system_prompt.encode()).hexdigest(),
    }
    public["fingerprint"] = hashlib.sha256(
        json.dumps(public, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return public
