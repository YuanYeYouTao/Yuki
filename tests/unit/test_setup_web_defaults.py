from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from qq_ai_bot.deployment_setup import command
from qq_ai_bot.deployment_setup.service import (
    EnvironmentDocument,
    SetupPaths,
    SetupValidationError,
    build_model_profiles,
    preserve_model_search_settings,
)


class DefaultUI:
    def __init__(self, choice: str | None = None) -> None:
        self.choice = choice
        self.defaults: list[str] = []

    def choose(self, _label, choices, *, default):
        self.defaults.append(default)
        result = self.choice or default
        assert result in dict(choices)
        return result

    def confirm(self, _label, *, default):
        return False  # Validate the complete draft without writing secrets/configuration.

    def info(self, _text):
        pass

    def success(self, _text):
        pass

    def warning(self, _text):
        pass

    def disabled(self, _text):
        pass

    def line(self, _text):
        pass


def draft_from_template(tmp_path, *, protocol, provider, initial=True):
    paths = SetupPaths(tmp_path)
    paths.env_example.write_bytes(Path(".env.example").read_bytes())
    (tmp_path / "config").mkdir()
    (tmp_path / "config/persona.md").write_text("Synthetic local persona", encoding="utf-8")
    document = EnvironmentDocument.load(paths)
    environment = document.values()
    environment.update(
        SUPERUSERS="12345",
        ONEBOT_ACCESS_TOKEN="synthetic-local-token",
        LLM_PROVIDER=provider,
        LLM_BASE_URL="https://api.deepseek.com/v1"
        if provider == "deepseek"
        else "https://wire.invalid/v1",
        LLM_API_KEY="synthetic-local-key",
        LLM_MODEL="configured-search-model",
        MEMORY_EMBEDDING_ENABLED="false",
    )
    return (
        paths,
        document,
        command._SetupDraft(
            environment=environment,
            protocol=protocol,
            flash_enabled=False,
            mcp_document={"mcpServers": {}},
            initial=initial,
        ),
    )


@pytest.mark.parametrize(
    "protocol,provider,mode,backend,connection",
    [
        ("gemini", "gemini", "bridge", "tavily", None),
        ("anthropic_messages", "anthropic", "native", "tavily", None),
        ("responses", "openai", "native", "tavily", None),
        ("responses", "deepseek", "external", "deepseek_anthropic", "primary_agent"),
    ],
)
def test_fresh_template_web_page_and_review_validate_without_tavily(
    tmp_path, monkeypatch, protocol, provider, mode, backend, connection
):
    paths, document, draft = draft_from_template(tmp_path, protocol=protocol, provider=provider)
    ui = DefaultUI()
    command._page_web(paths, ui, draft)
    assert ui.defaults == ["native"]
    assert draft.environment["WEB_ENABLED"] == "true"
    assert draft.environment["WEB_SEARCH_BACKEND"] == backend
    assert not draft.environment["TAVILY_API_KEY"]
    validated = []
    original = command.validate_configuration

    def validate(paths, configuration):
        settings = original(paths, configuration)
        validated.append(configuration)
        return settings

    monkeypatch.setattr(command, "validate_configuration", validate)
    assert (
        command._review_and_commit(
            paths=paths,
            ui=ui,
            document=document,
            draft=draft,
            sections=("basic", "web"),
            initial=True,
        )
        == 2
    )
    profiles = tomllib.loads(validated[0].model_profiles)
    main = profiles["profiles"]["primary_agent"]
    assert main["search_mode"] == mode
    assert ("native_web_search" in main["capabilities"]) == (mode == "native")
    assert profiles.get("search_connection") == connection
    assert not paths.env.exists() and not paths.model_profiles.exists()


@pytest.mark.parametrize(
    "switch", ["disabled", "legacy_false", "both", "native_with_tavily_fallback"]
)
def test_existing_web_only_preserves_off_and_combined_modes(tmp_path, monkeypatch, switch):
    paths, document, draft = draft_from_template(
        tmp_path, protocol="gemini", provider="gemini", initial=False
    )
    expected = switch
    if switch == "legacy_false":
        draft.environment.pop("WEB_MODE")
        draft.environment["WEB_ENABLED"] = "false"
        expected = "disabled"
    else:
        draft.environment["WEB_MODE"] = switch
    if switch == "native_with_tavily_fallback":
        expected = "both"
    if expected == "both":
        draft.environment["TAVILY_API_KEY"] = "synthetic-tavily-key"
        draft.environment["WEB_SEARCH_BACKEND"] = "tavily"
    profiles = build_model_profiles(
        main_protocol="gemini", main_provider="gemini", flash_enabled=False
    )
    if expected == "both":
        profiles = profiles.replace('["tools",', '["native_web_search", "tools",', 1)
    paths.model_profiles.parent.mkdir(parents=True)
    paths.model_profiles.write_text(profiles, encoding="utf-8")
    ui = DefaultUI()
    command._page_web(paths, ui, draft)
    assert draft.environment["WEB_MODE"] == expected
    monkeypatch.setattr(
        command,
        "build_model_profiles",
        lambda **kwargs: pytest.fail("web-only regenerated profiles"),
    )
    assert (
        command._review_and_commit(
            paths=paths, ui=ui, document=document, draft=draft, sections=("web",), initial=False
        )
        == 2
    )
    assert paths.model_profiles.read_text(encoding="utf-8") == profiles
    assert "search_mode" not in tomllib.loads(profiles)["profiles"]["primary_agent"]


@pytest.mark.parametrize("mode", [None, "external", "bridge", "native", "both"])
def test_basic_regeneration_keeps_saved_search_mode_and_capability(mode):
    old = build_model_profiles(main_protocol="gemini", main_provider="gemini", flash_enabled=True)
    if mode:
        old = old.replace(
            "[profiles.primary_agent]", f'[profiles.primary_agent]\nsearch_mode = "{mode}"'
        )
    if mode in {"native", "both"}:
        old = old.replace('["tools",', '["native_web_search", "tools",', 1)
    generated = build_model_profiles(
        main_protocol="gemini", main_provider="gemini", flash_enabled=True
    )
    result = tomllib.loads(preserve_model_search_settings(generated, old))
    main = result["profiles"]["primary_agent"]
    assert main.get("search_mode") == mode
    assert ("native_web_search" in main["capabilities"]) == (mode in {"native", "both"})
    assert "search_mode" not in result["profiles"]["background_tasks"]


def test_regeneration_keeps_explicit_deepseek_search_connection():
    old = build_model_profiles(
        main_protocol="responses",
        flash_enabled=True,
        main_search_mode=command.ModelSearchMode.EXTERNAL,
        search_connection=True,
    )
    generated = build_model_profiles(main_protocol="responses", flash_enabled=True)
    result = tomllib.loads(preserve_model_search_settings(generated, old))
    assert result["search_connection"] == "primary_agent"
    assert result["profiles"]["primary_agent"]["search_mode"] == "external"


def test_regeneration_refuses_to_collapse_different_saved_search_choices():
    old = build_model_profiles(main_protocol="gemini", flash_enabled=True)
    old = old.replace(
        "[profiles.primary_agent]", '[profiles.primary_agent]\nsearch_mode = "bridge"'
    )
    generated = build_model_profiles(main_protocol="gemini", flash_enabled=False)
    with pytest.raises(SetupValidationError, match="搜索选择不同"):
        preserve_model_search_settings(generated, old)


def test_fresh_user_can_opt_off(tmp_path):
    paths, _document, draft = draft_from_template(tmp_path, protocol="gemini", provider="gemini")
    command._page_web(paths, DefaultUI("disabled"), draft)
    assert draft.environment["WEB_MODE"] == "disabled"
    assert draft.environment["WEB_ENABLED"] == "false"
    assert command._new_main_search_mode(draft) is command.ModelSearchMode.EXTERNAL


def test_existing_native_independent_search_keeps_web_choice(tmp_path):
    paths, _document, draft = draft_from_template(
        tmp_path, protocol="chat_completions", provider="qwen", initial=False
    )
    command._page_web(paths, DefaultUI(), draft)
    assert draft.environment["WEB_MODE"] == "native"
    assert draft.environment["WEB_SEARCH_BACKEND"] == "deepseek_anthropic"


def test_basic_and_flash_regeneration_preserves_existing_search_connection(tmp_path, monkeypatch):
    paths, document, draft = draft_from_template(
        tmp_path, protocol="responses", provider="deepseek", initial=False
    )
    old = build_model_profiles(
        main_protocol="responses",
        flash_enabled=False,
        main_search_mode=command.ModelSearchMode.EXTERNAL,
        search_connection=True,
    )
    paths.model_profiles.parent.mkdir()
    paths.model_profiles.write_text(old, encoding="utf-8")
    draft.flash_enabled = True
    draft.environment.update(
        LLM_FLASH_BASE_URL="https://wire.invalid/v1",
        LLM_FLASH_API_KEY="synthetic-local-flash-key",
        LLM_FLASH_MODEL="configured-flash-model",
    )
    validated = []
    original = command.validate_configuration

    def validate(paths, configuration):
        result = original(paths, configuration)
        validated.append(configuration)
        return result

    monkeypatch.setattr(command, "validate_configuration", validate)
    assert (
        command._review_and_commit(
            paths=paths,
            ui=DefaultUI(),
            document=document,
            draft=draft,
            sections=("basic", "flash"),
            initial=False,
        )
        == 2
    )
    profile = tomllib.loads(validated[0].model_profiles)
    assert profile["search_connection"] == "primary_agent"
    assert profile["profiles"]["primary_agent"]["search_mode"] == "external"
    # Those tasks previously used the primary connection's explicit external choice.
    assert profile["profiles"]["background_tasks"]["search_mode"] == "external"
    assert "native_web_search" not in profile["profiles"]["background_tasks"]["capabilities"]
    assert paths.model_profiles.read_text(encoding="utf-8") == old


def test_fresh_deepseek_search_rejects_unofficial_connection(tmp_path):
    paths, document, draft = draft_from_template(
        tmp_path, protocol="responses", provider="deepseek"
    )
    draft.environment["LLM_BASE_URL"] = "https://unrelated.invalid/v1"
    command._page_web(paths, DefaultUI(), draft)
    with pytest.raises(SetupValidationError, match="官方 DeepSeek 搜索连接"):
        command._review_and_commit(
            paths=paths,
            ui=DefaultUI(),
            document=document,
            draft=draft,
            sections=("basic", "web"),
            initial=True,
        )


def test_inherited_native_without_capability_is_still_rejected(tmp_path):
    paths, document, draft = draft_from_template(
        tmp_path, protocol="responses", provider="openai", initial=False
    )
    paths.model_profiles.parent.mkdir()
    profiles = build_model_profiles(
        main_protocol="responses", main_provider="openai", flash_enabled=False
    )
    paths.model_profiles.write_text(profiles, encoding="utf-8")
    with pytest.raises(SetupValidationError, match="原生搜索能力"):
        command._review_and_commit(
            paths=paths,
            ui=DefaultUI(),
            document=document,
            draft=draft,
            sections=("web",),
            initial=False,
        )
