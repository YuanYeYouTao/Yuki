"""Shared constructors for the reviewed runtime configuration catalog."""

from __future__ import annotations

from typing import Any

from qq_ai_bot.admin.models import (
    ConfigApplyMode,
    ConfigScopeType,
    ConfigSpec,
    ConfigValue,
)
from qq_ai_bot.config import Settings

_G = (ConfigScopeType.GLOBAL,)
_GG = (ConfigScopeType.GLOBAL, ConfigScopeType.GROUP)
_GGU = (ConfigScopeType.GLOBAL, ConfigScopeType.GROUP, ConfigScopeType.USER)


def _field(name: str) -> Any:
    return lambda settings: getattr(settings, name)


def _path_field(name: str) -> Any:
    return lambda settings: str(getattr(settings, name))


def _constant(value: ConfigValue) -> Any:
    return lambda _settings: value


def _configured(name: str) -> Any:
    return lambda settings: bool(getattr(settings, name, ""))


def _database_password_configured(settings: Settings) -> bool:
    url = settings.database_url
    authority = url.split("://", maxsplit=1)[-1].split("/", maxsplit=1)[0]
    return ":" in authority and "@" in authority


def _spec(
    key: str,
    display_name: str,
    description: str,
    *,
    aliases: tuple[str, ...] = (),
    value_type: str,
    minimum: float | None = None,
    maximum: float | None = None,
    choices: tuple[str, ...] = (),
    scopes: tuple[ConfigScopeType, ...] = _G,
    mode: ConfigApplyMode = ConfigApplyMode.HOT,
    env_alias: str | None = None,
    getter: Any,
    settings_fields: tuple[str, ...] = (),
    category: str,
    sensitive: bool = False,
) -> ConfigSpec:
    return ConfigSpec(
        key=key,
        display_name=display_name,
        description=description,
        aliases=aliases,
        value_type=value_type,  # type: ignore[arg-type]
        minimum=minimum,
        maximum=maximum,
        choices=choices,
        allowed_scopes=scopes,
        apply_mode=mode,
        permission="superuser",
        sensitive=sensitive,
        env_alias=env_alias,
        default_getter=getter,
        settings_fields=settings_fields,
        category=category,
    )
