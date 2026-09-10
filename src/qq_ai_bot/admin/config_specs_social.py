"""Global social effect limits, shared across Presence and execution origin."""

from qq_ai_bot.admin.config_spec_helpers import _field, _spec
from qq_ai_bot.admin.models import ConfigSpec


def social_config_specs() -> tuple[ConfigSpec, ...]:
    return tuple(
        _spec(
            f"social.{name}",
            label,
            "按 canonical 目标/全局计算，聊天与自动化共享，不随切账号重置。",
            value_type="integer",
            minimum=1,
            env_alias=f"SOCIAL_{name.upper()}",
            getter=_field(f"social_{name}"),
            settings_fields=(f"social_{name}",),
            category="social",
        )
        for name, label in (
            ("send_per_target_per_minute", "主动发送每目标每分钟"),
            ("send_global_per_minute", "主动发送全局每分钟"),
            ("poke_per_target_per_minute", "戳一戳每目标每分钟"),
            ("poke_global_per_minute", "戳一戳全局每分钟"),
        )
    )
