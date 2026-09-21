"""The social surface has no configurable outbound frequency gate."""

from qq_ai_bot.admin.models import ConfigSpec


def social_config_specs() -> tuple[ConfigSpec, ...]:
    return ()
