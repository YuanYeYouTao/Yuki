"""Semantic observation stays beside the chat catalog and uses one selected connection."""

import tomllib
from types import SimpleNamespace

import pytest
from tests.conftest import make_settings
from yuki_participation.controller import Controller
from yuki_participation.models import Scope
from yuki_participation.observer import SemanticConnection

from yuki.admin.config_files import ConfigFileError, ConfigFileService
from yuki.model_runtime.models import ModelCapability, ModelProfile, ModelProtocol, ModelTask
from yuki.model_runtime.profiles import parse_model_profile_catalog
from yuki.model_runtime.semantic_observation import (
    ENVIRONMENT_CONNECTION_NAME,
    PRESET_ENDPOINTS,
    SemanticConfigError,
    connection_revision,
    normalize_semantic_section,
    provider_catalog,
    runtime_connection,
    semantic_use_document,
)
from yuki.services.semantic_participation import SemanticParticipationService, _Session


def _profile(name: str) -> dict[str, object]:
    return ModelProfile(
        id=name,
        provider="agm",
        protocol=ModelProtocol.CHAT_COMPLETIONS,
        base_url="https://model.invalid/v1",
        api_key_env="CHAT_KEY",
        model="chat-model",
        timeout_seconds=30,
        max_retries=0,
        default_temperature=0.4,
        default_max_output_tokens=256,
        capabilities=frozenset({ModelCapability.TOOLS}),
    ).model_dump(mode="json", exclude={"id", "headers"}, exclude_none=True)


def _document() -> dict[str, object]:
    return {
        "schema_version": 3,
        "profiles": {"main": _profile("main")},
        "routes": {task.value: "main" for task in ModelTask},
    }


def _lookup(keys: dict[str, str]):
    return lambda name: keys.get(name, "")


def test_presets_fill_endpoints_without_replacing_model_names() -> None:
    section = normalize_semantic_section(
        {
            "route": "jev",
            "connections": {
                "jev": {"provider": "jev", "request_model": "operator-jev"},
                "tencent": {
                    "provider": "tencent_jev",
                    "endpoint": "https://operator.example/systemone",
                    "request_model": "operator-request",
                    "response_model": "operator-response",
                },
            },
        }
    )
    assert section is not None
    assert section["connections"]["jev"]["endpoint"] == PRESET_ENDPOINTS["jev"]
    assert section["connections"]["jev"]["request_model"] == "operator-jev"
    assert section["connections"]["jev"]["response_model"] == "operator-jev"
    assert section["connections"]["tencent"]["endpoint"] == "https://operator.example/systemone"
    assert section["connections"]["tencent"]["response_model"] == "operator-response"


@pytest.mark.parametrize(
    ("raw", "category"),
    [
        (
            {"route": "jev", "connections": {"jev": {"provider": "jev"}}},
            "semantic_request_model_required",
        ),
        (
            {
                "route": "aliyun",
                "connections": {
                    "aliyun": {"provider": "aliyun_decision", "request_model": "decision"}
                },
            },
            "semantic_endpoint_required",
        ),
        (
            {
                "route": "other",
                "connections": {"other": {"provider": "other", "request_model": "x"}},
            },
            "semantic_provider_unknown",
        ),
    ],
)
def test_incomplete_semantic_sections_are_rejected(raw: dict[str, object], category: str) -> None:
    with pytest.raises(SemanticConfigError) as failure:
        normalize_semantic_section(raw)
    assert failure.value.category == category


def test_semantic_use_changes_only_the_semantic_section() -> None:
    document = _document()
    updated, key_env = semantic_use_document(
        document,
        provider="custom",
        name="custom_example",
        endpoint="https://example.invalid/v1/systemone",
        request_model="operator-request",
        response_model="operator-response",
        key_env="YUKI_SEMANTIC_CUSTOM_API_KEY",
    )
    assert updated["routes"] == document["routes"]
    assert updated["profiles"] == document["profiles"]
    assert key_env == "YUKI_SEMANTIC_CUSTOM_API_KEY"
    assert updated["semantic_observation"]["route"] == "custom_example"
    connection = updated["semantic_observation"]["connections"]["custom_example"]
    assert connection["request_model"] == "operator-request"
    assert connection["response_model"] == "operator-response"


def test_environment_connection_records_typesafe_and_a_section_does_not_fall_back() -> None:
    env = runtime_connection(
        None,
        environment_model="env-model",
        lookup=_lookup({"SEMANTIC_PARTICIPATION_API_KEY": "env-key"}),
    )
    assert env is not None
    assert env["name"] == ENVIRONMENT_CONNECTION_NAME
    assert env["endpoint"] == PRESET_ENDPOINTS["jev"]
    assert env["request_model"] == env["response_model"] == "env-model"
    section = normalize_semantic_section(
        {
            "route": "missing",
            "connections": {
                "aliyun": {
                    "provider": "aliyun_decision",
                    "endpoint": "https://workspace.example/systemone",
                    "request_model": "decision",
                }
            },
        }
    )
    assert (
        runtime_connection(
            section,
            environment_model="env-model",
            lookup=_lookup({"SEMANTIC_PARTICIPATION_API_KEY": "env-key"}),
        )
        is None
    )


def test_connection_revision_follows_the_selected_key_not_another_secret() -> None:
    first = connection_revision(
        endpoint="https://workspace.example/systemone",
        request_model="decision",
        response_model="decision",
        api_key="one",
    )
    changed = connection_revision(
        endpoint="https://workspace.example/systemone",
        request_model="decision",
        response_model="decision",
        api_key="two",
    )
    same = connection_revision(
        endpoint="https://workspace.example/systemone",
        request_model="decision",
        response_model="decision",
        api_key="one",
    )
    assert first == same
    assert first != changed
    assert "one" not in first and "two" not in changed


def test_provider_catalog_is_not_part_of_the_saved_section() -> None:
    catalog = provider_catalog()
    assert [item["provider"] for item in catalog] == [
        "jev",
        "tencent_jev",
        "aliyun_decision",
        "custom",
    ]
    assert "semantic_providers" not in str(normalize_semantic_section({}) or "")


async def test_model_save_preserves_and_clears_the_semantic_section(tmp_path) -> None:
    path = tmp_path / "models.toml"
    service = ConfigFileService(
        make_settings("sqlite+aiosqlite:///:memory:", model_profiles_file=path)
    )
    document = _document()
    document["semantic_observation"] = {
        "route": "aliyun",
        "connections": {
            "aliyun": {
                "provider": "aliyun_decision",
                "endpoint": "https://workspace.example/systemone",
                "request_model": "operator-decision",
                "response_model": "operator-revision",
            },
            "jev": {"provider": "jev", "request_model": "operator-jev"},
        },
    }
    await service.save(
        0, {"document": document, "api_keys": {"YUKI_SEMANTIC_ALIYUN_DECISION_API_KEY": "secret"}}
    )
    saved = tomllib.loads(path.read_text(encoding="utf-8"))
    assert (
        saved["semantic_observation"]["connections"]["aliyun"]["request_model"]
        == "operator-decision"
    )
    assert (
        saved["semantic_observation"]["connections"]["aliyun"]["response_model"]
        == "operator-revision"
    )
    catalog = parse_model_profile_catalog(path.read_text(encoding="utf-8"))
    assert set(catalog.routes) == set(ModelTask)

    shown = await service.read()
    assert [item["provider"] for item in shown["semantic_providers"]] == [
        "jev",
        "tencent_jev",
        "aliyun_decision",
        "custom",
    ]
    assert shown["document"]["routes"]["chat_agent"] == "main"
    assert "aliyun" in shown["saved_semantic_keys"]
    chat_only = {
        key: value for key, value in shown["document"].items() if key != "semantic_observation"
    }
    await service.save(shown["revision"], {"document": chat_only})
    preserved = tomllib.loads(path.read_text(encoding="utf-8"))
    assert preserved["semantic_observation"]["route"] == "aliyun"
    assert preserved["routes"]["chat_agent"] == "main"

    cleared = await service.read()
    cleared["document"]["semantic_observation"] = {}
    await service.save(cleared["revision"], {"document": cleared["document"]})
    assert "semantic_observation" not in tomllib.loads(path.read_text(encoding="utf-8"))


async def test_unreferenced_semantic_key_is_rejected(tmp_path) -> None:
    path = tmp_path / "models.toml"
    service = ConfigFileService(
        make_settings("sqlite+aiosqlite:///:memory:", model_profiles_file=path)
    )
    with pytest.raises(ConfigFileError) as failure:
        await service.save(
            0,
            {
                "document": _document(),
                "api_keys": {"YUKI_SEMANTIC_ALIYUN_DECISION_API_KEY": "secret"},
            },
        )
    assert failure.value.category == "validation_error"


async def test_hot_save_keeps_one_observer_and_clears_backoff_for_the_new_key(tmp_path) -> None:
    path = tmp_path / "models.toml"
    path.write_text("", encoding="utf-8")
    settings = make_settings(
        "sqlite+aiosqlite:///:memory:",
        model_profiles_file=path,
        semantic_participation_api_key="env-key",
        semantic_participation_model="env-model",
    )
    service = SemanticParticipationService(
        database=object(),
        settings=settings,
        ledger=object(),
        runtime_config=object(),
        validate_turn_snapshot=lambda _snapshot: None,
        promote=lambda _event, _binding: None,
    )
    await service.reload_semantic_configuration()
    assert service._observer is not None
    assert service._observer.model == "env-model"
    await service._observer.aclose()
    service._observer = None
    session = _Session(
        SimpleNamespace(),
        Controller(Scope(conversation_id="c", generation=1), 0),
        None,
        0,
    )
    service._sessions[("c", 1)] = session
    section = normalize_semantic_section(
        {
            "route": "aliyun",
            "connections": {
                "aliyun": {
                    "provider": "aliyun_decision",
                    "endpoint": "https://workspace.example/systemone",
                    "request_model": "operator-decision",
                }
            },
        }
    )
    await service.apply_configuration(
        section, {"YUKI_SEMANTIC_ALIYUN_DECISION_API_KEY": "first"}, usable=True
    )
    assert session.observation is not None
    stable = service._observer
    assert stable is not None and stable.available()
    session.observation.retry_after = 500
    session.observation._backoff_connection = stable.connection_revision
    await service.apply_configuration(
        section, {"YUKI_SEMANTIC_ALIYUN_DECISION_API_KEY": "second"}, usable=True
    )
    assert service._observer is stable
    session.observation._adopt_connection()
    assert session.observation.retry_after == 0
    assert isinstance(stable._connection, SemanticConnection)
    assert stable._connection.name == "aliyun"
    await stable.aclose()
