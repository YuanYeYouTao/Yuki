"""Application bootstrap compatibility tests."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import cast

from nonebot.adapters.onebot.v11 import Bot

from qq_ai_bot.adapters.onebot.provider_adapter import (
    SNOWLUMA_REVERSE_WS_PATH,
    NapCatOneBotAdapter,
    ProviderConnectionGuard,
    SnowLumaOneBotAdapter,
    provider_id_for_bot,
)
from qq_ai_bot.main import _nonebot_superusers_environment


def test_nonebot_superusers_environment_normalizes_and_restores_csv(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SUPERUSERS", "9001,9000")

    with _nonebot_superusers_environment(frozenset({"9000", "9001"})):
        assert json.loads(os.environ["SUPERUSERS"]) == ["9000", "9001"]

    assert os.environ["SUPERUSERS"] == "9001,9000"


def test_nonebot_superusers_environment_removes_temporary_value(monkeypatch) -> None:
    monkeypatch.delenv("SUPERUSERS", raising=False)

    with _nonebot_superusers_environment(frozenset()):
        assert os.environ["SUPERUSERS"] == "[]"

    assert "SUPERUSERS" not in os.environ


def test_provider_adapters_have_distinct_names_and_routes() -> None:
    assert NapCatOneBotAdapter.get_name() == "OneBot V11 / NapCat"
    assert SnowLumaOneBotAdapter.get_name() == "OneBot V11 / SnowLuma"
    assert SnowLumaOneBotAdapter.reverse_ws_paths == (
        SNOWLUMA_REVERSE_WS_PATH,
        f"{SNOWLUMA_REVERSE_WS_PATH}/",
    )


def test_provider_connection_guard_rejects_duplicate_until_disconnect() -> None:
    guard = ProviderConnectionGuard()
    assert guard.claim(
        external_account_id="8000",
        provider_id="napcat",
        connected_account_ids=(),
    )
    assert not guard.claim(
        external_account_id="8000",
        provider_id="snowluma",
        connected_account_ids=(),
    )
    guard.release(external_account_id="8000", provider_id="snowluma")
    assert not guard.claim(
        external_account_id="8000",
        provider_id="snowluma",
        connected_account_ids=(),
    )
    guard.release(external_account_id="8000", provider_id="napcat")
    assert guard.claim(
        external_account_id="8000",
        provider_id="snowluma",
        connected_account_ids=(),
    )


def test_provider_connection_guard_respects_driver_connections() -> None:
    guard = ProviderConnectionGuard()
    assert not guard.claim(
        external_account_id="8000",
        provider_id="snowluma",
        connected_account_ids={"8000"},
    )
    assert guard.claim(
        external_account_id="8001",
        provider_id="snowluma",
        connected_account_ids={"8000"},
    )


def test_provider_id_is_derived_from_bot_adapter() -> None:
    bot = cast(Bot, SimpleNamespace(adapter=SimpleNamespace(provider_id="SnowLuma")))
    assert provider_id_for_bot(bot) == "snowluma"
