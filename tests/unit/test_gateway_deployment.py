"""SnowLuma deployment config, profile selection, and transition receipts."""

from __future__ import annotations

import json

import pytest
from tests.conftest import make_settings

from qq_ai_bot.cli import _render_snowluma_config
from qq_ai_bot.deployment_setup.service import (
    EnvironmentDocument,
    SetupConfiguration,
    SetupPaths,
    SetupValidationError,
    _validate_gateway_configuration,
    commit_configuration,
    compose_profiles_with_features,
    selected_gateway_providers,
)


def test_snowluma_renderer_merges_only_yuki_client_and_is_idempotent(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "config/onebot.json"
    output.parent.mkdir(parents=True)
    output.write_text(
        json.dumps(
            {
                "logLevel": "debug",
                "networks": {
                    "httpServers": [{"name": "operator-owned"}],
                    "wsClients": [
                        {"name": "other", "url": "ws://example.invalid/other"},
                        {"name": "yuki", "url": "ws://stale.invalid"},
                        {"name": "yuki", "url": "ws://duplicate.invalid"},
                    ],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SNOWLUMA_REVERSE_WS_URL", "ws://bot:8080/onebot/v11/snowluma/ws")
    settings = make_settings(
        "sqlite+aiosqlite:///:memory:",
        onebot_access_token="onebot-test-token",
    )

    _render_snowluma_config(settings, output)
    first = output.read_bytes()
    _render_snowluma_config(settings, output)

    assert output.read_bytes() == first
    payload = json.loads(first)
    assert payload["logLevel"] == "debug"
    assert payload["networks"]["httpServers"] == [{"name": "operator-owned"}]
    clients = payload["networks"]["wsClients"]
    assert [item["name"] for item in clients] == ["other", "yuki"]
    assert clients[1] == {
        "name": "yuki",
        "enabled": True,
        "url": "ws://bot:8080/onebot/v11/snowluma/ws",
        "role": "Universal",
        "accessToken": "onebot-test-token",
        "messageFormat": "array",
        "reportSelfMessage": False,
        "reconnectIntervalMs": 30000,
    }


def test_snowluma_renderer_refuses_invalid_existing_config(tmp_path) -> None:
    output = tmp_path / "onebot.json"
    output.write_text("not-json", encoding="utf-8")
    settings = make_settings(
        "sqlite+aiosqlite:///:memory:",
        onebot_access_token="onebot-test-token",
    )

    with pytest.raises(ValueError, match="invalid"):
        _render_snowluma_config(settings, output)

    assert output.read_text(encoding="utf-8") == "not-json"
    assert tuple(tmp_path.glob(".*.tmp")) == ()


def test_profiles_default_legacy_to_napcat_and_preserve_extensions() -> None:
    assert selected_gateway_providers({"COMPOSE_PROFILES": ""}) == ("napcat",)
    assert selected_gateway_providers({"COMPOSE_PROFILES": "speech,snowluma"}) == ("snowluma",)
    assert (
        compose_profiles_with_features(
            {"COMPOSE_PROFILES": "custom,napcat"},
            gateways=("snowluma",),
            speech_enabled=True,
        )
        == "snowluma,speech,custom"
    )


def test_snowluma_profile_validation_is_fail_closed() -> None:
    base = {
        "COMPOSE_PROFILES": "snowluma",
        "SNOWLUMA_IMAGE": "motricseven7/snowluma:latest",
        "SNOWLUMA_VNC_PASSWORD": "secure-vnc-password",
        "SNOWLUMA_UID": "1000",
        "SNOWLUMA_GID": "1000",
        "SNOWLUMA_NOVNC_PORT": "6081",
        "SNOWLUMA_WEBUI_HOST_PORT": "5099",
        "SNOWLUMA_EXTRA_QQ_HOMES": "/app/qq-accounts/qq2,/app/qq-accounts/qq3",
    }
    _validate_gateway_configuration(base)
    with pytest.raises(SetupValidationError, match="不能重复"):
        _validate_gateway_configuration({**base, "SNOWLUMA_WEBUI_HOST_PORT": "6081"})
    with pytest.raises(SetupValidationError, match="/app/qq-accounts"):
        _validate_gateway_configuration({**base, "SNOWLUMA_EXTRA_QQ_HOMES": "/root/qq2"})


def test_commit_writes_retryable_stop_old_then_start_new_action(tmp_path) -> None:
    paths = SetupPaths(tmp_path)
    paths.env.write_text(
        "COMPOSE_PROFILES=napcat\nSPEECH_ENABLED=false\n",
        encoding="utf-8",
    )
    document = EnvironmentDocument.load(paths)
    configuration = SetupConfiguration(
        environment={
            **document.values(),
            "COMPOSE_PROFILES": "snowluma",
        },
        model_profiles="",
        mcp_document={"mcpServers": {}},
        pending_plugins=None,
        write_model_profiles=False,
        write_mcp=False,
    )

    commit_configuration(paths, document, configuration)

    assert json.loads(paths.gateway_action.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "previous": ["napcat"],
        "target": ["snowluma"],
    }
    assert "onebot-test-token" not in paths.gateway_action.read_text(encoding="utf-8")
