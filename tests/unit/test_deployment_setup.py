"""Deployment setup preserves operator choices and configuration backups."""

from __future__ import annotations

import tomllib
from io import StringIO
from pathlib import Path

import pytest
import tomlkit

from yuki.deployment_setup.command import (
    _configure,
    _page_gateway,
    _page_web,
    _review_and_commit,
    _SetupDraft,
)
from yuki.deployment_setup.service import (
    EnvironmentDocument,
    SetupConfiguration,
    SetupPaths,
    SetupValidationError,
    _validate_gateway_configuration,
    build_model_profiles,
    commit_configuration,
    compose_profiles_with_features,
    initialize_soul,
    prepare_operator_configuration,
    selected_gateway_providers,
    validate_configuration,
)
from yuki.deployment_setup.terminal import TerminalUI
from yuki.model_runtime.models import ModelProtocol, ModelSearchMode
from yuki.model_runtime.profiles import parse_model_profile_catalog
from yuki.model_runtime.secrets import encode_model_secrets, model_secrets_path


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


def test_snowluma_selection_prepares_only_its_real_credentials(tmp_path: Path) -> None:
    environment = {"COMPOSE_PROFILES": "snowluma", "SNOWLUMA_IMAGE": "own/snowluma:kept"}
    draft = _SetupDraft(environment, "chat_completions", False)
    ui = TerminalUI(no_color=True, input_fn=lambda _prompt: "", output=StringIO())
    _page_gateway(SetupPaths(tmp_path), ui, draft)
    assert "SUPERUSERS" not in environment
    assert environment["ONEBOT_ACCESS_TOKEN"] and environment["SNOWLUMA_VNC_PASSWORD"]
    assert environment["SNOWLUMA_IMAGE"] == "own/snowluma:kept"
    assert environment["SNOWLUMA_VNC_HOST"] == "snowluma"
    _validate_gateway_configuration(environment)


def _template_environment() -> dict[str, str]:
    root = Path(__file__).resolve().parents[2]
    values = EnvironmentDocument((root / ".env.example").read_text(encoding="utf-8")).values()
    # The shipped template may still name a bundled profile or image; a new install
    # must neither inherit that choice nor accept an image the user did not give.
    values.update(COMPOSE_PROFILES="snowluma", SNOWLUMA_IMAGE="motricseven7/snowluma:latest")
    return values


def test_new_install_leaves_qq_unselected_by_default(tmp_path: Path) -> None:
    """Failure 3: an unchosen QQ must not keep the SnowLuma profile of the template."""
    draft = _SetupDraft(_template_environment(), "chat_completions", False, initial=True)
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    ui = TerminalUI(no_color=True, input_fn=answer, output=StringIO())
    _page_gateway(SetupPaths(tmp_path), ui, draft)
    assert selected_gateway_providers(draft.environment) == ()
    assert any("Telegram" in prompt for prompt in prompts)
    assert "飞书" in ui._output.getvalue()
    assert any("QQ 官方" in prompt for prompt in prompts)
    assert "跳过" in ui._output.getvalue() and "设置 → 平台" in ui._output.getvalue()


def test_new_install_selecting_snowluma_requires_the_users_own_image(tmp_path: Path) -> None:
    """Failure 3: SnowLuma has no public image; setup must ask for the user's own."""
    draft = _SetupDraft(_template_environment(), "chat_completions", False, initial=True)
    answers = iter(("y", "", "registry.example/me/snowluma:1", "n", "", "n"))
    ui = TerminalUI(no_color=True, input_fn=lambda _prompt: next(answers), output=StringIO())
    _page_gateway(SetupPaths(tmp_path), ui, draft)
    environment = draft.environment
    assert selected_gateway_providers(environment) == ("snowluma",)
    assert environment["SNOWLUMA_IMAGE"] == "registry.example/me/snowluma:1"
    assert environment["SNOWLUMA_VNC_HOST"] == "snowluma"
    assert len(environment["ONEBOT_ACCESS_TOKEN"]) >= 32
    assert not environment["SNOWLUMA_VNC_PASSWORD"].startswith("replace-with-")
    _validate_gateway_configuration(environment)


@pytest.mark.parametrize(
    "env",
    [
        "COMPOSE_PROFILES=custom\nONEBOT_ACCESS_TOKEN=replace-with-a-long-random-token\n",
        "COMPOSE_PROFILES=snowluma,custom\nSNOWLUMA_IMAGE=old/snowluma\n"
        "ONEBOT_ACCESS_TOKEN=kept-token\nSNOWLUMA_VNC_PASSWORD=kept-password\n"
        "SNOWLUMA_VNC_HOST=qq.example\n",
    ],
)
def test_setup_snowluma_command_writes_what_the_gateway_page_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    """deploy/snowluma.sh and the interactive gateway page enable SnowLuma alike."""
    import secrets
    import sys

    from yuki.cli import main

    generated = iter(f"generated-{index}-" + "x" * 32 for index in range(4))
    monkeypatch.setattr(secrets, "token_urlsafe", lambda _size: next(generated))
    image = "registry.example/me/snowluma:1"
    draft = _SetupDraft(EnvironmentDocument(env).values(), "chat_completions", False)
    answers = iter(("y", image, "n", "", "n"))
    ui = TerminalUI(no_color=True, input_fn=lambda _prompt: next(answers), output=StringIO())
    _page_gateway(SetupPaths(tmp_path), ui, draft)

    (tmp_path / ".env").write_text(env, encoding="utf-8")
    generated = iter(f"generated-{index}-" + "x" * 32 for index in range(4))
    monkeypatch.setattr(
        sys,
        "argv",
        ["yuki", "setup", "snowluma", "--deployment-root", str(tmp_path), "--image", image],
    )
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 0
    assert EnvironmentDocument.load(SetupPaths(tmp_path)).values() == draft.environment
    assert selected_gateway_providers(draft.environment) == ("snowluma",)
    if "kept-token" in env:
        assert draft.environment["ONEBOT_ACCESS_TOKEN"] == "kept-token"
        assert draft.environment["SNOWLUMA_VNC_PASSWORD"] == "kept-password"


def test_setup_snowluma_command_requires_an_image(tmp_path: Path) -> None:
    import argparse

    from yuki.deployment_setup.command import run_setup_command

    env = "COMPOSE_PROFILES=\n"
    (tmp_path / ".env").write_text(env, encoding="utf-8")
    args = argparse.Namespace(
        deployment_root=tmp_path, no_color=True, setup_action="snowluma", image=""
    )
    assert run_setup_command(args) == 1
    assert (tmp_path / ".env").read_text(encoding="utf-8") == env


def test_new_setup_writes_platform_credentials_to_the_platform_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telegram/Lark/QQ official credentials go to webui-config, never to .env."""
    import json
    import stat

    root = Path(__file__).resolve().parents[2]
    paths = SetupPaths(tmp_path)
    paths.env_example.write_bytes((root / ".env.example").read_bytes())
    (tmp_path / "config").mkdir()
    (tmp_path / "config/SOUL.md").write_bytes((root / "config/SOUL.md").read_bytes())
    monkeypatch.chdir(tmp_path)
    secrets = {
        "Telegram": "123:telegram-setup-secret",
        "Lark": "lark-setup-secret",
        "QQ 官方": "qq-official-setup-secret",
    }
    answers = iter(
        (
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
            "n",  # bundled SnowLuma
            "y",  # Telegram
            "lark",  # Feishu or Lark
            "cli_lark_setup",
            "y",  # QQ official Bot
            "102000002",
            "y",  # sandbox
            "y",  # write configuration
        )
    )

    def secret(prompt: str) -> str:
        return next((value for key, value in secrets.items() if key in prompt), "test-api-key")

    ui = TerminalUI(
        no_color=True,
        input_fn=lambda _prompt: next(answers),
        secret_fn=secret,
        output=StringIO(),
    )
    assert _configure(paths, ui) == 0

    env_text = paths.env.read_text(encoding="utf-8")
    assert all(value not in env_text for value in secrets.values())
    assert "TELEGRAM_BOT_TOKEN" not in env_text and "FEISHU_APP_ID" not in env_text
    document = tomllib.loads((tmp_path / "webui-config/platforms.toml").read_text("utf-8"))
    assert document["telegram"] == {"enabled": True}
    assert document["lark"] == {"enabled": True, "app_id": "cli_lark_setup"}
    assert document["qq_official"] == {"enabled": True, "app_id": "102000002", "sandbox": True}
    sidecar = tmp_path / "webui-config/platforms.secrets.json"
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    assert set(json.loads(sidecar.read_text("utf-8"))["keys"].values()) == set(secrets.values())
    assert all(value not in ui._output.getvalue() for value in secrets.values())


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (
            "FEISHU_APP_ID=cli_lark\nFEISHU_APP_SECRET=lark-secret\n"
            "FEISHU_DOMAIN=https://open.larksuite.com\n",
            {"lark": {"enabled": True, "app_id": "cli_lark"}},
        ),
        ("FEISHU_APP_ID=cli_half\n", None),
        ("TELEGRAM_BOT_TOKEN=\nFEISHU_APP_ID=\n", {}),
    ],
)
def test_platform_migration_maps_the_old_domain_and_refuses_unpaired_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str, expected: dict | None
) -> None:
    import argparse

    from yuki.deployment_setup.command import run_setup_command

    for name in ("TELEGRAM_BOT_TOKEN", "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_DOMAIN"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "webui-config").mkdir()
    (tmp_path / ".env").write_text(env, encoding="utf-8")
    args = argparse.Namespace(
        deployment_root=tmp_path, no_color=True, setup_action="platforms-migrate"
    )
    document = tmp_path / "webui-config/platforms.toml"
    if expected is None:
        assert run_setup_command(args) == 1
        assert not document.exists()
        return
    assert run_setup_command(args) == 0
    assert (tomllib.loads(document.read_text("utf-8")) if document.exists() else {}) == expected


@pytest.mark.parametrize("profiles", ["", "external"])
def test_new_setup_writes_selected_profiles_and_private_operator_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profiles: str
) -> None:
    root = Path(__file__).resolve().parents[2]
    template = EnvironmentDocument((root / ".env.example").read_text(encoding="utf-8"))
    paths = SetupPaths(tmp_path)
    paths.env_example.write_text(template.merge({"COMPOSE_PROFILES": profiles}), encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config/SOUL.md").write_text(
        (root / "config/SOUL.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    answers = iter(
        (
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
            "",  # no Telegram
            "",  # no Feishu or Lark
            "",  # no QQ official Bot
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
    assert environment["SOUL_FILE"] == "character/SOUL.md"
    assert paths.soul.read_bytes() == paths.soul_template.read_bytes()
    assert environment["ONEBOT_ACCESS_TOKEN"] == template.values()["ONEBOT_ACCESS_TOKEN"]
    assert environment["SNOWLUMA_VNC_PASSWORD"] == template.values()["SNOWLUMA_VNC_PASSWORD"]
    policy = tomllib.loads((tmp_path / environment["CONTROL_OPERATORS_FILE"]).read_text("utf-8"))
    operator = policy["operators"][0]
    assert operator["token_env"] == "YUKI_CONTROL_OPERATOR_TOKEN"
    assert "identity.person.superuser" in operator["capabilities"]
    assert "person_id" not in operator
    assert len(environment["YUKI_CONTROL_OPERATOR_TOKEN"]) >= 32
    assert environment["YUKI_CONTROL_OPERATOR_TOKEN"] not in ui._output.getvalue()
    assert not paths.restart_required.exists()


def test_operator_setup_preserves_policy_and_credential(tmp_path: Path) -> None:
    paths = SetupPaths(tmp_path)
    environment = {
        "CONTROL_OPERATORS_FILE": "config/reviewed-operators.toml",
        "YUKI_CONTROL_OPERATOR_TOKEN": "existing-credential-must-be-preserved",
    }
    original = dict(environment)
    assert prepare_operator_configuration(paths, environment) is None
    assert environment == original
    assert not (tmp_path / "config/control-operators.toml").exists()


async def test_generated_operator_uses_existing_authentication(database, tmp_path, monkeypatch):
    from yuki.admin.authority.operators import ControlOperatorAccess
    from yuki.admin.authority.principal import PrincipalSource

    environment = {}
    policy = prepare_operator_configuration(SetupPaths(tmp_path), environment)
    path = tmp_path / environment["CONTROL_OPERATORS_FILE"]
    path.parent.mkdir()
    path.write_text(policy, encoding="utf-8")
    credential = environment["YUKI_CONTROL_OPERATOR_TOKEN"]
    monkeypatch.setenv("YUKI_CONTROL_OPERATOR_TOKEN", credential)
    principal = await ControlOperatorAccess(database, path).authenticate(
        credential, source=PrincipalSource.CLI
    )
    assert principal.person_id is None
    assert principal.allows("identity.person.create")
    assert principal.allows("identity.person.superuser")
    assert principal.allows("identity.client.pair")
    example = Path(__file__).parents[2] / "config/control-operators.example.toml"
    documented = tomllib.loads(example.read_text("utf-8"))["operators"][0]["capabilities"]
    assert set(principal.granted_capabilities) == set(documented)


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
    _validate_gateway_configuration(environment)  # The retired 6081 listener reserves no port.


def _search_setup(tmp_path: Path, profiles: str, **updates: str):
    (tmp_path / "character").mkdir(exist_ok=True)
    (tmp_path / "character/SOUL.md").write_text("Test persona", encoding="utf-8")
    environment = {
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


def test_personal_configuration_does_not_require_qq_material(tmp_path: Path) -> None:
    profiles = build_model_profiles(main_protocol="gemini", flash_enabled=False)
    paths, configuration = _search_setup(tmp_path, profiles, WEB_MODE="disabled")
    validate_configuration(paths, configuration)
    assert "ONEBOT_ACCESS_TOKEN" not in configuration.environment
    assert "SNOWLUMA_VNC_PASSWORD" not in configuration.environment


@pytest.mark.parametrize("web_mode", ["native", "tavily", "both"])
def test_deepseek_setup_generates_independent_messages_search_profile(
    tmp_path: Path, web_mode: str
) -> None:
    text = build_model_profiles(
        main_protocol="responses",
        main_provider="deepseek",
        flash_enabled=False,
        main_search_mode=ModelSearchMode.EXTERNAL,
        environment={"LLM_BASE_URL": "https://api.deepseek.com/v1"},
    )
    payload = tomllib.loads(text)
    assert payload["search_backend"] == "model"
    search_id = payload["profiles"]["primary_agent"]["search_connection"]
    search = payload["profiles"][search_id]
    assert search["protocol"] == "anthropic_messages"
    assert search["base_url"] == "https://api.deepseek.com/anthropic/v1"
    assert search["model_env"] == "LLM_MODEL" and search["thinking_enabled"] is False
    assert search["api_key_env"] == payload["profiles"]["primary_agent"]["api_key_env"]
    paths, configuration = _search_setup(tmp_path, text, WEB_MODE=web_mode)
    validate_configuration(paths, configuration)
    catalog = parse_model_profile_catalog(text, environment=configuration.environment)
    assert catalog.profiles["primary_agent"].protocol is ModelProtocol.RESPONSES
    assert catalog.profiles[search_id].protocol is ModelProtocol.ANTHROPIC_MESSAGES
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
        environment={"LLM_BASE_URL": "https://api.deepseek.com/v1"},
    )
    # Only the selected independent connection gets this sidecar key alias.
    head, selected = profiles.split("[profiles.primary_agent_search]", 1)
    profiles = (
        head
        + "[profiles.primary_agent_search]"
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
    )
    document = tomllib.loads(profiles)
    document["search_connection"] = "primary_agent"
    profiles = tomlkit.dumps(document)
    paths, configuration = _search_setup(tmp_path, profiles)
    with pytest.raises(SetupValidationError, match="does not support native"):
        validate_configuration(paths, configuration)


def test_setup_still_requires_key_for_selected_tavily_backend(tmp_path: Path) -> None:
    profiles = build_model_profiles(
        main_protocol="responses",
        main_provider="deepseek",
        flash_enabled=False,
        main_search_mode=ModelSearchMode.EXTERNAL,
        search_backend="tavily",
    )
    paths, configuration = _search_setup(tmp_path, profiles, WEB_MODE="tavily")
    with pytest.raises(SetupValidationError, match="Tavily"):
        validate_configuration(paths, configuration)


def test_setup_native_mode_checks_explicit_tavily_backend_without_profile_mode(tmp_path: Path):
    profiles = build_model_profiles(
        main_protocol="gemini",
        main_provider="gemini",
        flash_enabled=False,
        search_backend="tavily",
    )
    paths, configuration = _search_setup(tmp_path, profiles)
    with pytest.raises(SetupValidationError, match="Tavily"):
        validate_configuration(paths, configuration)


@pytest.mark.parametrize("mode", [None, "external", "bridge", "both"])
def test_setup_profile_search_target_checks_own_key_without_tavily(
    tmp_path: Path, mode: str | None
):
    profiles = build_model_profiles(
        main_protocol="gemini",
        main_provider="gemini",
        flash_enabled=False,
        search_backend="tavily",
    )
    document = tomllib.loads(profiles)
    main = document["profiles"]["primary_agent"]
    main["search_connection"] = "primary_agent"
    if mode is not None:
        main["search_mode"] = mode
    if mode == "both":
        main["capabilities"].append("native_web_search")
    paths, configuration = _search_setup(tmp_path, tomlkit.dumps(document))
    validate_configuration(paths, configuration)
    main["api_key_env"] = "MISSING_SEARCH_KEY"
    missing = SetupConfiguration(configuration.environment, tomlkit.dumps(document), None)
    with pytest.raises(SetupValidationError, match="API Key"):
        validate_configuration(paths, missing)


def test_setup_still_reads_old_deepseek_key_borrowing_configuration(tmp_path: Path) -> None:
    profiles = build_model_profiles(
        main_protocol="responses",
        main_provider="deepseek",
        flash_enabled=False,
        main_search_mode=ModelSearchMode.EXTERNAL,
        search_backend="deepseek_anthropic",
    ).replace("schema_version = 3", 'schema_version = 3\nsearch_connection = "primary_agent"')
    paths, configuration = _search_setup(
        tmp_path, profiles, WEB_SEARCH_BACKEND="deepseek_anthropic"
    )
    validate_configuration(paths, configuration)
    catalog = parse_model_profile_catalog(
        profiles, environment=configuration.environment, search_backend="deepseek_anthropic"
    )
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


@pytest.mark.parametrize(
    "protocol,provider,endpoint,expected_mode",
    [
        ("responses", "deepseek", "https://api.deepseek.com/v1", "bridge"),
        ("chat_completions", "deepseek", "https://api.deepseek.com/v1", "bridge"),
        ("gemini", "gemini", "https://generativelanguage.googleapis.com/v1beta", "bridge"),
        ("anthropic_messages", "anthropic", "https://api.anthropic.com/v1", "native"),
    ],
)
def test_native_setup_commits_provider_search_without_tavily(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    provider: str,
    endpoint: str,
    expected_mode: str,
) -> None:
    root = Path(__file__).resolve().parents[2]
    paths = SetupPaths(tmp_path)
    paths.env_example.write_text(
        (root / ".env.example").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "config").mkdir()
    (tmp_path / "config/SOUL.md").write_text("Test persona", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    answers = iter(
        (
            "12345",
            protocol,
            *([provider] if protocol in {"responses", "chat_completions"} else []),
            endpoint,
            "deepseek-main",
            "n",
            "n",
            "native",
            "n",
            "n",
            "n",
            "n",  # bundled SnowLuma
            "n",  # Telegram
            "skip",  # Feishu or Lark
            "n",  # QQ official Bot
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
    assert payload["search_backend"] == "model"
    assert payload["profiles"]["primary_agent"]["protocol"] == protocol
    main = payload["profiles"]["primary_agent"]
    assert main["search_mode"] == expected_mode
    if provider == "deepseek":
        selected = payload["profiles"][main["search_connection"]]
        assert selected["protocol"] == "anthropic_messages"
        assert selected["api_key_env"] == main["api_key_env"]
        assert selected["model_env"] == main["model_env"]
    elif protocol == "gemini":
        assert main["search_connection"] == "primary_agent"
    else:
        assert "native_web_search" in main["capabilities"]
    assert "TAVILY_API_KEY=\n" in paths.env.read_text(encoding="utf-8")


def test_existing_setup_native_selection_replaces_old_tavily_inheritance(tmp_path: Path) -> None:
    profiles = build_model_profiles(
        main_protocol="chat_completions",
        main_provider="deepseek",
        flash_enabled=False,
        main_search_mode=ModelSearchMode.EXTERNAL,
        search_backend="tavily",
    )
    paths, configuration = _search_setup(tmp_path, profiles)
    environment = configuration.environment
    environment.pop("WEB_MODE")
    environment["WEB_ENABLED"] = "true"
    paths.model_profiles.parent.mkdir()
    paths.model_profiles.write_text(profiles, encoding="utf-8")
    paths.env.write_text(
        "\n".join(f"{name}={value}" for name, value in environment.items()) + "\n",
        encoding="utf-8",
    )
    draft = _SetupDraft(environment, "chat_completions", False, initial=False)
    page_ui = TerminalUI(no_color=True, input_fn=lambda _: "native", output=StringIO())
    _page_web(paths, page_ui, draft)
    assert environment["WEB_SEARCH_BACKEND"] == "model"
    assert (
        _review_and_commit(
            paths=paths,
            ui=TerminalUI(no_color=True, input_fn=lambda _: "y", output=StringIO()),
            document=EnvironmentDocument(paths.env.read_text(encoding="utf-8")),
            draft=draft,
            sections=("web",),
            initial=False,
        )
        == 0
    )
    saved = tomllib.loads(paths.model_profiles.read_text(encoding="utf-8"))
    assert saved["search_backend"] == "model"
    target = saved["profiles"]["primary_agent"]["search_connection"]
    assert saved["profiles"][target]["api_key_env"] == "LLM_API_KEY"
    assert saved["profiles"]["primary_agent"]["protocol"] == "chat_completions"


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
    profiles += _auxiliary_profile(aux_mode)
    paths, configuration = _search_setup(tmp_path, profiles, WEB_MODE="tavily")
    validate_configuration(paths, configuration)


def test_setup_actual_plugin_external_route_still_requires_tavily(tmp_path: Path) -> None:
    profiles = build_model_profiles(
        main_protocol="gemini",
        main_provider="gemini",
        flash_enabled=False,
        main_search_mode=ModelSearchMode.BRIDGE,
        search_backend="tavily",
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


def test_setup_removes_retired_role_sources_including_multiline_values() -> None:
    document = EnvironmentDocument(
        "# operator notes\n"
        "SYSTEM_PROMPT='private first line\nprivate second line'\n"
        "SYSTEM_PROMPT_FILE=old-main.md\n"
        "BOT_PERSONA_FILE=old-self.md\n"
        "YUKI_PERSONA_FILE=oldest.md\n"
        "BOT_DISPLAY_NAME=old-name\n"
        "CUSTOM_EXTENSION=keep\n"
    )
    rendered = document.merge({**document.values(), "SOUL_FILE": "character/SOUL.md"})
    assert "private" not in rendered
    assert "old-" not in rendered and "oldest" not in rendered
    assert "# operator notes" in rendered and "CUSTOM_EXTENSION=keep" in rendered
    assert "SOUL_FILE=character/SOUL.md" in rendered


def test_soul_initialization_preserves_an_existing_empty_user_file(tmp_path: Path) -> None:
    paths = SetupPaths(tmp_path)
    paths.soul.parent.mkdir()
    paths.soul.write_bytes(b"")
    initialize_soul(paths)
    assert paths.soul.read_bytes() == b""
    assert not paths.soul_template.exists()


def test_setup_cancel_does_not_initialize_the_user_soul(tmp_path: Path) -> None:
    paths = SetupPaths(tmp_path)
    paths.soul_template.parent.mkdir()
    paths.soul_template.write_text("# Explicit template", encoding="utf-8")
    draft = _SetupDraft(
        {
            "LLM_PROVIDER": "gemini",
            "LLM_BASE_URL": "https://generativelanguage.googleapis.com/v1beta",
            "LLM_API_KEY": "fixture-key",
            "LLM_MODEL": "fixture-model",
            "COMPOSE_PROFILES": "",
            "WEB_MODE": "disabled",
        },
        "gemini",
        False,
        initial=True,
    )
    ui = TerminalUI(no_color=True, input_fn=lambda _prompt: "n", output=StringIO())
    assert (
        _review_and_commit(
            paths=paths,
            ui=ui,
            document=EnvironmentDocument(""),
            draft=draft,
            sections=("basic",),
            initial=True,
        )
        == 2
    )
    assert not paths.soul.exists()
    assert not paths.env.exists() and not paths.model_profiles.exists()


def test_validation_reads_only_the_selected_soul_and_allows_empty_text(tmp_path: Path) -> None:
    profiles = build_model_profiles(main_protocol="gemini", flash_enabled=False)
    paths, configuration = _search_setup(tmp_path, profiles, WEB_MODE="disabled")
    paths.soul.write_bytes(b"")
    paths.soul_template.parent.mkdir()
    paths.soul_template.write_text("A different role", encoding="utf-8")
    assert validate_configuration(paths, configuration).soul == ""
    paths.soul.unlink()
    with pytest.raises(FileNotFoundError):
        validate_configuration(paths, configuration)


@pytest.mark.parametrize(
    ("env", "host"),
    [
        ("COMPOSE_PROFILES=snowluma\nONEBOT_ACCESS_TOKEN=kept\n", "snowluma"),
        ("COMPOSE_PROFILES=snowluma\nSNOWLUMA_VNC_HOST=qq.example\n", "qq.example"),
        ("COMPOSE_PROFILES=\n", None),
    ],
)
def test_platform_migration_keeps_qq_login_of_the_bundled_snowluma(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str, host: str | None
) -> None:
    """QQ login now follows SNOWLUMA_VNC_HOST; an upgrade must not make it disappear."""
    import argparse
    import os
    import stat

    from yuki.deployment_setup.command import run_setup_command

    for name in ("TELEGRAM_BOT_TOKEN", "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_DOMAIN"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "webui-config").mkdir()
    env_path = tmp_path / ".env"
    env_path.write_text(env, encoding="utf-8")
    os.chmod(env_path, 0o600)
    args = argparse.Namespace(
        deployment_root=tmp_path, no_color=True, setup_action="platforms-migrate"
    )
    for _ in range(2):
        assert run_setup_command(args) == 0
        values = EnvironmentDocument.load(SetupPaths(tmp_path)).values()
        assert values.get("SNOWLUMA_VNC_HOST") == host
        assert values.get("ONEBOT_ACCESS_TOKEN") == ("kept" if "kept" in env else None)
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
