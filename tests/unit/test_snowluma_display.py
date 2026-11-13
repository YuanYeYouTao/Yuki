"""「QQ 登入」 follows the configured SnowLuma display address, not the Compose profile."""

import pytest

from yuki.client_api import snowluma_display


@pytest.mark.parametrize(
    ("environment", "available"),
    [
        ({}, False),
        ({"COMPOSE_PROFILES": "snowluma"}, False),
        ({"COMPOSE_PROFILES": "snowluma", "SNOWLUMA_VNC_HOST": ""}, False),
        ({"SNOWLUMA_VNC_HOST": "snowluma"}, True),
        ({"SNOWLUMA_VNC_HOST": "qq.example.internal"}, True),
    ],
)
def test_qq_display_is_offered_only_with_a_configured_host(
    monkeypatch: pytest.MonkeyPatch, environment: dict[str, str], available: bool
) -> None:
    for name in ("COMPOSE_PROFILES", "SNOWLUMA_VNC_HOST"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    assert snowluma_display.configured() is available
