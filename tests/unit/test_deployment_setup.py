"""Deployment setup preserves operator choices and configuration backups."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest

from qq_ai_bot.deployment_setup.command import _configure, _page_gateway, _SetupDraft
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
from qq_ai_bot.deployment_setup.terminal import TerminalUI


@pytest.mark.parametrize("profiles", ["", "external", "custom-a,custom-b"])
def test_gateway_page_preserves_unselected_and_extension_profiles(
    tmp_path: Path, profiles: str
) -> None:
    environment = {"COMPOSE_PROFILES": profiles}
    assert selected_gateway_providers(environment) == ()
    ui = TerminalUI(no_color=True, input_fn=lambda _prompt: "", output=StringIO())
    draft = _SetupDraft(environment, "chat_completions", False)

    _page_gateway(SetupPaths(tmp_path), ui, draft)

    assert draft.environment["COMPOSE_PROFILES"] == profiles
    _validate_gateway_configuration(draft.environment)


def test_gateway_profile_changes_retain_extensions() -> None:
    environment = {"COMPOSE_PROFILES": "custom-b,snowluma,custom-a"}
    assert selected_gateway_providers(environment) == ("snowluma",)
    assert compose_profiles_with_features(environment, gateways=()) == "custom-a,custom-b"
    assert (
        compose_profiles_with_features(environment, gateways=("snowluma",))
        == "snowluma,custom-a,custom-b"
    )


@pytest.mark.parametrize("profiles", ["", "external"])
def test_new_setup_writes_selected_profiles_without_retired_gateway_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profiles: str
) -> None:
    root = Path(__file__).resolve().parents[2]
    template = EnvironmentDocument((root / ".env.example").read_text(encoding="utf-8"))
    paths = SetupPaths(tmp_path)
    paths.env_example.write_text(template.merge({"COMPOSE_PROFILES": profiles}), encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config/persona.md").write_text(
        (root / "config/persona.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    answers = iter(
        (
            "12345",  # superuser
            "chat_completions",
            "openai_compatible",
            "https://api.example.com/v1",
            "test-model",
            "n",  # auxiliary model
            "n",  # embedding
            "disabled",  # web
            "n",  # vision
            "n",  # plugins
            "n",  # automation
            "",  # preserve no bundled gateway
            "y",  # write configuration
        )
    )
    ui = TerminalUI(
        no_color=True,
        input_fn=lambda _prompt: next(answers),
        secret_fn=lambda _prompt: "test-api-key",
        output=StringIO(),
    )

    assert _configure(paths, ui) == 0

    environment = EnvironmentDocument.load(paths).values()
    assert environment["COMPOSE_PROFILES"] == profiles
    assert environment["ONEBOT_ACCESS_TOKEN"] != template.values()["ONEBOT_ACCESS_TOKEN"]
    assert not any(key.startswith("NAPCAT_") for key in environment)
    assert not (paths.root / "data/setup/gateway-action.json").exists()
    assert not paths.restart_required.exists()
    assert not any(
        (paths.root / directory).exists()
        for directory in ("napcat-data", "napcat-config", "napcat-plugins")
    )


def test_config_change_preserves_unknown_environment_and_backs_up_originals(tmp_path: Path) -> None:
    paths = SetupPaths(tmp_path)
    original = (
        "# operator extension\n"
        "COMPOSE_PROFILES=external\n"
        "EXTENSION_TOKEN=private-extension\n"
        "NAPCAT_WEBUI_TOKEN=private-legacy\n"
    )
    paths.env.write_text(original, encoding="utf-8")
    paths.model_profiles.parent.mkdir()
    paths.model_profiles.write_text("original-models\n", encoding="utf-8")
    document = EnvironmentDocument.load(paths)

    backup = commit_configuration(
        paths,
        document,
        SetupConfiguration(
            environment={"COMPOSE_PROFILES": "snowluma,external"},
            model_profiles="new-models\n",
            pending_plugins=None,
        ),
    )

    assert backup is not None
    assert (backup / ".env").read_text(encoding="utf-8") == original
    assert (backup / "webui-config/model_profiles.toml").read_text(encoding="utf-8") == (
        "original-models\n"
    )
    environment = EnvironmentDocument.load(paths).values()
    assert environment["EXTENSION_TOKEN"] == "private-extension"
    assert environment["NAPCAT_WEBUI_TOKEN"] == "private-legacy"
    assert paths.restart_required.read_text(encoding="utf-8") == "configuration-changed\n"
    assert not (paths.root / "data/setup/gateway-action.json").exists()


def test_snowluma_port_validation_uses_current_listeners() -> None:
    environment = {
        "COMPOSE_PROFILES": "snowluma",
        "SNOWLUMA_IMAGE": "snowluma:test",
        "SNOWLUMA_VNC_PASSWORD": "test-password",
        "SNOWLUMA_NOVNC_PORT": "6099",
        "SNOWLUMA_WEBUI_HOST_PORT": "5099",
    }
    _validate_gateway_configuration(environment)
    environment["SNOWLUMA_WEBUI_HOST_PORT"] = "6099"
    with pytest.raises(SetupValidationError, match="宿主端口不能重复"):
        _validate_gateway_configuration(environment)
