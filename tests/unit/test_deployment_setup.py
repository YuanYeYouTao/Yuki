"""Deployment setup preserves operator choices and configuration backups."""

from __future__ import annotations

import tomllib
from io import StringIO
from pathlib import Path

import pytest

from qq_ai_bot.deployment_setup.command import _configure, _page_gateway, _page_web, _SetupDraft
from qq_ai_bot.deployment_setup.service import (
    EnvironmentDocument,
    SetupConfiguration,
    SetupPaths,
    SetupValidationError,
    _validate_gateway_configuration,
    build_model_profiles,
    commit_configuration,
    compose_profiles_with_features,
    selected_gateway_providers,
    validate_configuration,
)
from qq_ai_bot.deployment_setup.terminal import TerminalUI
from qq_ai_bot.model_runtime.models import ModelProtocol, ModelSearchMode
from qq_ai_bot.model_runtime.profiles import parse_model_profile_catalog
from qq_ai_bot.model_runtime.secrets import encode_model_secrets, model_secrets_path


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


def _search_setup(tmp_path: Path, profiles: str, **updates: str):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config/persona.md").write_text("Test persona", encoding="utf-8")
    environment = {
        "SUPERUSERS": "9000",
        "LLM_PROVIDER": "deepseek",
        "LLM_BASE_URL": "https://api.deepseek.com/v1",
        "LLM_API_KEY": "test-main-key",
        "LLM_MODEL": "deepseek-main",
        "WEB_MODE": "native",
        "WEB_SEARCH_BACKEND": "tavily",
        "COMPOSE_PROFILES": "",
        "TAVILY_API_KEY": "",
    }
    environment.update(updates)
    paths = SetupPaths(tmp_path)
    return paths, SetupConfiguration(environment, profiles, None)


@pytest.mark.parametrize("web_mode", ["native", "tavily", "both"])
def test_deepseek_setup_generates_independent_messages_search_profile(
    tmp_path: Path, web_mode: str
) -> None:
    text = build_model_profiles(
        main_protocol="responses",
        main_provider="deepseek",
        flash_enabled=False,
        main_search_mode=ModelSearchMode.EXTERNAL,
        search_connection=True,
    )
    payload = tomllib.loads(text)
    assert payload["search_backend"] == "model" and payload["search_connection"] == "search"
    search = payload["profiles"]["search"]
    assert search["protocol"] == "anthropic_messages"
    assert search["base_url"] == "https://api.deepseek.com/anthropic/v1"
    assert search["model"] == "deepseek-flash" and search["thinking_mode"] == "disabled"
    assert search["api_key_env"] == payload["profiles"]["primary_agent"]["api_key_env"]
    paths, configuration = _search_setup(tmp_path, text, WEB_MODE=web_mode)
    validate_configuration(paths, configuration)
    catalog = parse_model_profile_catalog(text, environment=configuration.environment)
    assert catalog.profiles["primary_agent"].protocol is ModelProtocol.RESPONSES
    assert catalog.profiles["search"].protocol is ModelProtocol.ANTHROPIC_MESSAGES
    assert all(route.profile_id == "primary_agent" for route in catalog.routes.values())


@pytest.mark.parametrize(
    "protocol, provider_name",
    [
        ("anthropic_messages", "anthropic"),
        ("responses", "openai"),
        ("gemini", "gemini"),
    ],
)
def test_setup_validates_selected_native_connection_instead_of_tavily(
    tmp_path: Path, protocol: str, provider_name: str
) -> None:
    profiles = build_model_profiles(
        main_protocol=protocol,
        main_provider=provider_name,
        flash_enabled=False,
        main_search_mode=ModelSearchMode.BRIDGE,
    )
    paths, configuration = _search_setup(tmp_path, profiles, WEB_MODE="tavily")
    validate_configuration(paths, configuration)


def test_setup_rejects_missing_selected_search_key_and_accepts_sidecar(tmp_path: Path) -> None:
    alias = "YUKI_WEBUI_KEY_" + "A" * 32
    profiles = build_model_profiles(
        main_protocol="responses",
        main_provider="deepseek",
        flash_enabled=False,
        main_search_mode=ModelSearchMode.EXTERNAL,
        search_connection=True,
    )
    # Only the selected independent connection gets this sidecar key alias.
    head, selected = profiles.split("[profiles.search]", 1)
    profiles = (
        head
        + "[profiles.search]"
        + selected.replace('api_key_env = "LLM_API_KEY"', f'api_key_env = "{alias}"')
    )
    paths, configuration = _search_setup(tmp_path, profiles)
    with pytest.raises(SetupValidationError, match="API Key"):
        validate_configuration(paths, configuration)
    paths.model_profiles.parent.mkdir(parents=True)
    secret_file = model_secrets_path(paths.model_profiles)
    secret_file.write_bytes(encode_model_secrets({alias: "test-search-key"}))
    secret_file.chmod(0o600)
    validate_configuration(paths, configuration)


def test_setup_rejects_deepseek_responses_as_a_native_search_connection(tmp_path: Path) -> None:
    profiles = build_model_profiles(
        main_protocol="responses",
        main_provider="deepseek",
        flash_enabled=False,
        main_search_mode=ModelSearchMode.EXTERNAL,
    ).replace(
        "schema_version = 3",
        'schema_version = 3\nsearch_backend = "model"\nsearch_connection = "primary_agent"',
    )
    paths, configuration = _search_setup(tmp_path, profiles)
    with pytest.raises(SetupValidationError, match="does not support native"):
        validate_configuration(paths, configuration)


def test_setup_still_requires_key_for_selected_tavily_backend(tmp_path: Path) -> None:
    profiles = build_model_profiles(
        main_protocol="responses",
        main_provider="deepseek",
        flash_enabled=False,
        main_search_mode=ModelSearchMode.EXTERNAL,
    )
    paths, configuration = _search_setup(tmp_path, profiles, WEB_MODE="tavily")
    with pytest.raises(SetupValidationError, match="Tavily"):
        validate_configuration(paths, configuration)


def test_setup_still_reads_old_deepseek_key_borrowing_configuration(tmp_path: Path) -> None:
    profiles = build_model_profiles(
        main_protocol="responses",
        main_provider="deepseek",
        flash_enabled=False,
        main_search_mode=ModelSearchMode.EXTERNAL,
    ).replace("schema_version = 3", 'schema_version = 3\nsearch_connection = "primary_agent"')
    paths, configuration = _search_setup(
        tmp_path, profiles, WEB_SEARCH_BACKEND="deepseek_anthropic"
    )
    validate_configuration(paths, configuration)
    catalog = parse_model_profile_catalog(profiles, environment=configuration.environment)
    assert catalog.search_backend is None
    assert catalog.profiles["primary_agent"].protocol is ModelProtocol.RESPONSES


def test_deepseek_new_install_uses_model_backend_without_changing_main_protocol(
    tmp_path: Path,
) -> None:
    draft = _SetupDraft(
        {"LLM_PROVIDER": "deepseek", "WEB_MODE": "native"}, "responses", False, initial=True
    )
    ui = TerminalUI(no_color=True, input_fn=lambda _prompt: "", output=StringIO())
    _page_web(SetupPaths(tmp_path), ui, draft)
    assert draft.environment["WEB_SEARCH_BACKEND"] == "model"
    assert draft.protocol == "responses"


def test_deepseek_native_setup_commits_independent_search_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[2]
    paths = SetupPaths(tmp_path)
    paths.env_example.write_text(
        (root / ".env.example").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "config").mkdir()
    (tmp_path / "config/persona.md").write_text("Test persona", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    answers = iter(
        (
            "12345",
            "responses",
            "deepseek",
            "https://api.deepseek.com/v1",
            "deepseek-main",
            "n",
            "n",
            "native",
            "n",
            "n",
            "n",
            "n",
            "y",
        )
    )
    ui = TerminalUI(
        no_color=True,
        input_fn=lambda _prompt: next(answers),
        secret_fn=lambda _prompt: "test-api-key",
        output=StringIO(),
    )
    assert _configure(paths, ui) == 0
    payload = tomllib.loads(paths.model_profiles.read_text(encoding="utf-8"))
    assert payload["search_backend"] == "model" and payload["search_connection"] == "search"
    assert payload["profiles"]["primary_agent"]["protocol"] == "responses"
    assert payload["profiles"]["primary_agent"]["search_mode"] == "external"
    assert payload["profiles"]["search"]["protocol"] == "anthropic_messages"


def _auxiliary_profile(search_mode: str) -> str:
    return (
        '\n[profiles.aux]\nprovider = "deepseek"\nprotocol = "responses"\n'
        'base_url = "https://api.deepseek.com/v1"\napi_key_env = "UNUSED_SEARCH_KEY"\n'
        'model = "auxiliary"\ntimeout_seconds = 30\nmax_retries = 0\n'
        "default_temperature = 0.1\ndefault_max_output_tokens = 128\n"
        f'capabilities = ["tools"]\nsearch_mode = "{search_mode}"\n'
    )


@pytest.mark.parametrize("aux_mode", ["external", "bridge"])
@pytest.mark.parametrize("routed", [False, True])
def test_setup_auxiliary_or_unrouted_search_modes_do_not_block_main_bridge(
    tmp_path: Path, aux_mode: str, routed: bool
) -> None:
    profiles = build_model_profiles(
        main_protocol="gemini",
        main_provider="gemini",
        flash_enabled=False,
        main_search_mode=ModelSearchMode.BRIDGE,
    )
    if routed:
        profiles = profiles.replace(
            'memory_extraction = "primary_agent"', 'memory_extraction = "aux"'
        )
        profiles = profiles.replace(
            'emoji_replacement = "primary_agent"', 'emoji_replacement = "aux"'
        )
    profiles += _auxiliary_profile(aux_mode)
    paths, configuration = _search_setup(tmp_path, profiles, WEB_MODE="tavily")
    validate_configuration(paths, configuration)


def test_setup_actual_plugin_external_route_still_requires_tavily(tmp_path: Path) -> None:
    profiles = build_model_profiles(
        main_protocol="gemini",
        main_provider="gemini",
        flash_enabled=False,
        main_search_mode=ModelSearchMode.BRIDGE,
    ).replace('plugin_agent_session = "primary_agent"', 'plugin_agent_session = "aux"')
    profiles += _auxiliary_profile("external")
    paths, configuration = _search_setup(tmp_path, profiles, WEB_MODE="tavily")
    with pytest.raises(SetupValidationError, match="Tavily"):
        validate_configuration(paths, configuration)


def test_setup_explicit_native_ignores_old_tavily_default(tmp_path: Path) -> None:
    profiles = build_model_profiles(
        main_protocol="gemini",
        main_provider="gemini",
        flash_enabled=False,
        main_search_mode=ModelSearchMode.NATIVE,
    )
    paths, configuration = _search_setup(tmp_path, profiles, WEB_MODE="tavily")
    validate_configuration(paths, configuration)
