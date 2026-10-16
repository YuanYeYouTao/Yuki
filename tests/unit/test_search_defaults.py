"""One configuration contract generates native search for setup and management."""

from __future__ import annotations

from copy import deepcopy

import pytest
import tomlkit

from qq_ai_bot.config import Settings
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProtocol,
    ModelSearchMode,
    ModelTask,
)
from qq_ai_bot.model_runtime.profiles import (
    ModelRuntimeConfigurationError,
    normalize_model_profile_document,
    parse_model_profile_catalog,
)
from qq_ai_bot.web.models import WebMode
from qq_ai_bot.web.search_config import effective_search_profile


def _profile(provider="deepseek", protocol="responses", **updates):
    return {
        "provider": provider,
        "protocol": protocol,
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "MAIN_KEY",
        "model": "selected-model",
        "timeout_seconds": 72,
        "max_retries": 0,
        "default_temperature": 0.3,
        "default_max_output_tokens": 4000,
        "max_output_tokens_limit": 8000,
        "capabilities": ["tools", "structured_output"],
        **updates,
    }


def _document(main=None):
    return {
        "schema_version": 3,
        "profiles": {"main": main or _profile()},
        "routes": {task.value: "main" for task in ModelTask},
    }


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
def test_deepseek_default_preserves_main_and_uses_same_connection_credentials(protocol):
    document = _document(_profile(protocol=protocol))
    original = deepcopy(document)
    normalized = normalize_model_profile_document(document)
    main = normalized["profiles"]["main"]
    selected = normalized["profiles"][main["search_connection"]]
    assert document == original
    assert main["protocol"] == protocol
    assert main["search_mode"] == "bridge"
    assert "search_connection" not in normalized
    assert selected["protocol"] == "anthropic_messages"
    assert selected["base_url"] == "https://api.deepseek.com/anthropic/v1"
    for field in (
        "api_key_env",
        "model",
        "timeout_seconds",
        "default_max_output_tokens",
        "max_output_tokens_limit",
    ):
        assert selected[field] == main[field]
    assert normalize_model_profile_document(normalized) == normalized
    catalog = parse_model_profile_catalog(tomlkit.dumps(normalized))
    resolved = effective_search_profile(catalog, "model", catalog.profiles["main"])
    assert resolved.protocol is ModelProtocol.ANTHROPIC_MESSAGES


@pytest.mark.parametrize("protocol,mode", [("gemini", "bridge"), ("anthropic_messages", "native")])
def test_provider_default_uses_its_existing_endpoint_and_key(protocol, mode):
    normalized = normalize_model_profile_document(
        _document(_profile(provider="custom-gateway", protocol=protocol))
    )
    main = normalized["profiles"]["main"]
    assert main["search_mode"] == mode
    assert main["base_url"] == "https://api.deepseek.com/v1"
    assert main["api_key_env"] == "MAIN_KEY"
    assert len(normalized["profiles"]) == 1
    if mode == "bridge":
        assert main["search_connection"] == "main"
    else:
        assert "native_web_search" in main["capabilities"]


@pytest.mark.parametrize("mode", [None, "external"])
def test_mixed_native_defaults_keep_each_provider_endpoint_and_credential(mode):
    document = _document(_profile(**({"search_mode": mode} if mode else {})))
    document["profiles"]["plugin"] = _profile(
        provider="gemini",
        protocol="gemini",
        base_url="https://google.example/v1beta",
        api_key_env="PLUGIN_KEY",
        model="gemini-model",
        **({"search_mode": mode} if mode else {}),
    )
    document["routes"]["plugin_agent_session"] = "plugin"
    normalized = normalize_model_profile_document(document)
    catalog = parse_model_profile_catalog(tomlkit.dumps(normalized))
    deepseek = effective_search_profile(catalog, "model", catalog.profiles["main"])
    gemini = effective_search_profile(catalog, "model", catalog.profiles["plugin"])
    assert deepseek.api_key_env == "MAIN_KEY"
    assert deepseek.base_url == "https://api.deepseek.com/anthropic/v1"
    assert gemini.api_key_env == "PLUGIN_KEY"
    assert gemini.base_url == "https://google.example/v1beta"
    assert gemini.protocol is ModelProtocol.GEMINI
    assert catalog.search_connection is None
    if mode:
        assert catalog.profiles["main"].search_mode is ModelSearchMode.EXTERNAL
        assert catalog.profiles["plugin"].search_mode is ModelSearchMode.EXTERNAL


def test_explicit_profile_search_connection_precedes_shared_connection():
    document = _document(_profile(search_mode="bridge", search_connection="own"))
    document["search_connection"] = "shared"
    document["profiles"]["own"] = _profile(protocol="gemini", api_key_env="OWN_KEY")
    document["profiles"]["shared"] = _profile(protocol="gemini", api_key_env="SHARED_KEY")
    catalog = parse_model_profile_catalog(tomlkit.dumps(document))
    assert effective_search_profile(catalog, "model", catalog.profiles["main"]).id == "own"


def test_explicit_shared_connection_and_external_mode_are_preserved():
    document = _document(_profile(search_mode="external"))
    document["search_connection"] = "shared"
    document["profiles"]["shared"] = _profile(protocol="gemini", api_key_env="SHARED_KEY")
    normalized = normalize_model_profile_document(document)
    assert normalized["profiles"] == document["profiles"]
    assert normalized["search_backend"] == "model"
    assert normalized["search_connection"] == "shared"


@pytest.mark.parametrize("mode,backend", [(WebMode.DISABLED, "model"), (WebMode.NATIVE, "tavily")])
def test_disabled_and_explicit_external_backend_do_not_generate_defaults(mode, backend):
    document = _document()
    normalized = normalize_model_profile_document(document, web_mode=mode, search_backend=backend)
    assert normalized == (
        document if mode is WebMode.DISABLED else {**document, "search_backend": backend}
    )
    explicit = {**document, "search_backend": "tavily"}
    assert normalize_model_profile_document(explicit) == explicit


def test_legacy_enabled_uses_native_without_tavily_and_false_stays_disabled():
    enabled = Settings(_env_file=None, web_enabled=True)
    disabled = Settings(_env_file=None, web_enabled=False)
    assert enabled.web.mode is WebMode.NATIVE
    assert enabled.web_search_backend == "model"
    assert disabled.web.mode is WebMode.DISABLED
    assert Settings(_env_file=None, web_mode="tavily").web_search_backend == "tavily"


def test_custom_deepseek_standard_endpoint_stays_on_the_same_gateway():
    document = _document(_profile(base_url="https://gateway.example/v1"))
    normalized = normalize_model_profile_document(document)
    search = normalized["profiles"][normalized["profiles"]["main"]["search_connection"]]
    assert search["base_url"] == "https://gateway.example/anthropic/v1"
    assert search["api_key_env"] == "MAIN_KEY"


def test_custom_path_and_unknown_chat_do_not_invent_a_search_endpoint():
    for profile in (
        _profile(base_url="https://gateway.example/project/deepseek/v1"),
        _profile(provider="custom-gateway", protocol="chat_completions"),
    ):
        catalog = parse_model_profile_catalog(tomlkit.dumps(_document(profile)))
        assert len(catalog.profiles) == 1
        assert catalog.search_connection is None
        assert catalog.profiles["main"].search_mode is None
        assert catalog.profiles["main"].base_url == profile["base_url"]


def test_environment_references_are_shared_without_reading_or_copying_key_values():
    document = _document(_profile(base_url_env="MAIN_ENDPOINT", model_env="MAIN_MODEL"))
    document["profiles"]["main"].pop("base_url")
    document["profiles"]["main"].pop("model")
    normalized = normalize_model_profile_document(
        document,
        environment={"MAIN_ENDPOINT": "https://gateway.example/v1", "MAIN_MODEL": "my-model"},
    )
    selected = normalized["profiles"][normalized["profiles"]["main"]["search_connection"]]
    assert selected["base_url"] == "https://gateway.example/anthropic/v1"
    assert selected["model_env"] == "MAIN_MODEL"
    assert selected["api_key_env"] == "MAIN_KEY"


def test_explicit_native_mode_and_capabilities_are_preserved():
    profile = _profile(
        provider="gemini",
        protocol="gemini",
        search_mode="native",
        capabilities=["tools", "native_web_search"],
    )
    catalog = parse_model_profile_catalog(tomlkit.dumps(_document(profile)))
    assert catalog.profiles["main"].search_mode is ModelSearchMode.NATIVE
    assert ModelCapability.NATIVE_WEB_SEARCH in catalog.profiles["main"].capabilities


def test_unknown_profile_search_connection_is_a_configuration_error():
    with pytest.raises(ModelRuntimeConfigurationError, match="search connection references"):
        parse_model_profile_catalog(
            tomlkit.dumps(_document(_profile(search_mode="bridge", search_connection="missing")))
        )


@pytest.mark.parametrize("updates", [{"provider": 12}, {"capabilities": "tools"}, {"base_url": 12}])
def test_invalid_profile_fields_are_rejected_by_configuration_validation(updates):
    with pytest.raises(ModelRuntimeConfigurationError, match="invalid model profile configuration"):
        parse_model_profile_catalog(tomlkit.dumps(_document(_profile(**updates))))
