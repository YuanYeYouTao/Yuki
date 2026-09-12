"""Lookup and scalar conversion for the reviewed runtime configuration catalog."""

from __future__ import annotations

import math
from collections.abc import Iterable

from qq_ai_bot.admin.config_specs_asr import asr_config_specs
from qq_ai_bot.admin.config_specs_emoji import emoji_config_specs
from qq_ai_bot.admin.config_specs_future import future_config_specs
from qq_ai_bot.admin.config_specs_hot import hot_config_specs
from qq_ai_bot.admin.config_specs_planner_plugins import planner_plugin_config_specs
from qq_ai_bot.admin.config_specs_protected import protected_config_specs
from qq_ai_bot.admin.config_specs_restart import restart_config_specs
from qq_ai_bot.admin.config_specs_social import social_config_specs
from qq_ai_bot.admin.config_specs_speech import speech_config_specs
from qq_ai_bot.admin.config_specs_tooling_mcp import tooling_mcp_config_specs
from qq_ai_bot.admin.models import ConfigSpec, ConfigValue


def _registered_specs() -> tuple[ConfigSpec, ...]:
    """Return every explicitly reviewed configuration declaration."""

    return (
        *hot_config_specs(),
        *emoji_config_specs(),
        *planner_plugin_config_specs(),
        *future_config_specs(),
        *restart_config_specs(),
        *speech_config_specs(),
        *asr_config_specs(),
        *social_config_specs(),
        *tooling_mcp_config_specs(),
        *protected_config_specs(),
    )


class ConfigRegistry:
    """Resolve only explicitly reviewed keys and aliases."""

    def __init__(self, specs: Iterable[ConfigSpec] | None = None) -> None:
        selected = tuple(specs) if specs is not None else _registered_specs()
        self._specs = {spec.key: spec for spec in selected}
        if len(self._specs) != len(selected):
            raise ValueError("duplicate runtime configuration key")
        aliases: dict[str, str] = {}
        for spec in selected:
            for alias in (spec.key, *spec.aliases):
                normalized = alias.strip().casefold()
                previous = aliases.get(normalized)
                if previous is not None and previous != spec.key:
                    raise ValueError(f"duplicate runtime configuration alias: {alias}")
                aliases[normalized] = spec.key
        self._aliases = aliases

    def get(self, key_or_alias: str) -> ConfigSpec:
        """Return a spec or reject unknown input before any value is processed."""

        normalized = key_or_alias.strip().casefold()
        key = self._aliases.get(normalized)
        if key is None:
            raise KeyError(key_or_alias)
        return self._specs[key]

    def maybe_get(self, key_or_alias: str) -> ConfigSpec | None:
        try:
            return self.get(key_or_alias)
        except KeyError:
            return None

    def list(self, category: str | None = None) -> tuple[ConfigSpec, ...]:
        """List the stable allowlist, optionally within one category."""

        normalized = category.strip().casefold() if category else None
        return tuple(
            spec
            for spec in self._specs.values()
            if normalized is None or spec.category.casefold() == normalized
        )

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self._specs)

    @staticmethod
    def convert(spec: ConfigSpec, value: object) -> ConfigValue:
        """Convert untrusted command/tool input into the registered scalar type."""

        if spec.value_type == "boolean":
            if isinstance(value, bool):
                converted: ConfigValue = value
            elif isinstance(value, int) and value in {0, 1}:
                converted = bool(value)
            elif isinstance(value, str):
                token = value.strip().casefold()
                true_values = {"true", "1", "on", "yes", "是", "开启", "启用"}
                false_values = {"false", "0", "off", "no", "否", "关闭", "停用"}
                if token in true_values:
                    converted = True
                elif token in false_values:
                    converted = False
                else:
                    raise ValueError("必须是 true/false、on/off 或开启/关闭")
            else:
                raise ValueError("必须是布尔值")
        elif spec.value_type == "integer":
            if isinstance(value, bool):
                raise ValueError("必须是整数")
            if isinstance(value, int):
                converted = value
            elif isinstance(value, float) and value.is_integer():
                converted = int(value)
            elif isinstance(value, str):
                try:
                    converted = int(value.strip())
                except ValueError as exc:
                    raise ValueError("必须是整数") from exc
            else:
                raise ValueError("必须是整数")
        elif spec.value_type == "number":
            if isinstance(value, bool):
                raise ValueError("必须是数字")
            if isinstance(value, int | float):
                converted = float(value)
            elif isinstance(value, str):
                try:
                    converted = float(value.strip())
                except ValueError as exc:
                    raise ValueError("必须是数字") from exc
            else:
                raise ValueError("必须是数字")
        else:
            if not isinstance(value, str):
                raise ValueError("必须是字符串")
            converted = value.strip()
            if not converted:
                raise ValueError("不能为空")
            if spec.value_type == "enum":
                normalized_choices = {choice.casefold(): choice for choice in spec.choices}
                choice = normalized_choices.get(converted.casefold())
                if choice is None:
                    raise ValueError(f"必须是以下值之一：{', '.join(spec.choices)}")
                converted = choice

        if isinstance(converted, int | float) and not isinstance(converted, bool):
            if isinstance(converted, float) and not math.isfinite(converted):
                raise ValueError("必须是有限数字")
            if spec.minimum is not None and converted < spec.minimum:
                raise ValueError(f"不能小于 {spec.minimum:g}")
            if spec.maximum is not None and converted > spec.maximum:
                raise ValueError(f"不能大于 {spec.maximum:g}")
        return converted
