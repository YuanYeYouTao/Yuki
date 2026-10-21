"""The social surface has no configurable outbound frequency gate."""

from yuki.admin.models import ConfigSpec


def social_config_specs() -> tuple[ConfigSpec, ...]:
    return ()
