"""Health follows the active profile/pool and never sends a model request."""

import json

import httpx
import pytest
from tests.conftest import make_settings
from tests.support.model_profiles import write_fake_profiles
from tests.unit.test_telegram_health import health_container

from yuki.application.lifecycle import LifecycleRegistry
from yuki.application.modules.model_runtime import ModelRuntimeModule
from yuki.health import build_health_payload
from yuki.llm.base import LLMConfigurationError
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.executor import TaskModelExecutor
from yuki.model_runtime.models import ModelTask
from yuki.model_runtime.pool import ModelClientPool
from yuki.model_runtime.profiles import load_model_profile_catalog
from yuki.model_runtime.routes import ModelRouter
from yuki.model_runtime.secrets import encode_model_secrets, model_secrets_path


def keyed_profiles(path):
    write_fake_profiles(path)
    path.write_text(
        path.read_text()
        .replace('provider = "fake"', 'provider = "openai"')
        .replace('api_key_env = ""', 'api_key_env = "PROFILE_HEALTH_TEST_KEY"'),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("source", ["environment", "sidecar"])
async def test_flat_values_can_be_empty_when_the_actual_profile_is_configured(
    database, tmp_path, monkeypatch, source
):
    path = keyed_profiles(tmp_path / "models.toml")
    key = "profile-health-secret-never-in-response"
    if source == "sidecar":
        model_secrets_path(path).write_bytes(encode_model_secrets({"PROFILE_HEALTH_TEST_KEY": key}))
    else:
        monkeypatch.setenv("PROFILE_HEALTH_TEST_KEY", key)
    settings = make_settings(
        database.url,
        model_profiles_file=path,
        llm_provider="openai",
        llm_base_url="",
        llm_model="",
        llm_api_key="",
    )
    requests = []

    async def unexpected_http(*args, **kwargs):
        requests.append((args, kwargs))
        raise AssertionError("health must not call the model")

    monkeypatch.setattr(httpx.AsyncClient, "send", unexpected_http)
    lifecycle = LifecycleRegistry()
    model = ModelRuntimeModule(
        settings.model_runtime, database, lifecycle=lifecycle, web_settings=settings.web
    ).build()
    container = health_container(database, None)
    container.settings, container.models = settings, model.executor
    try:
        payload = await build_health_payload(container)
        assert payload["llm_configured"] is True
        assert key not in json.dumps(payload) and requests == []
    finally:
        await model.executor.close()


async def test_missing_profile_key_keeps_the_actual_startup_error(database, tmp_path, monkeypatch):
    path = keyed_profiles(tmp_path / "models.toml")
    monkeypatch.delenv("PROFILE_HEALTH_TEST_KEY", raising=False)
    settings = make_settings(database.url, model_profiles_file=path)
    with pytest.raises(LLMConfigurationError, match="PROFILE_HEALTH_TEST_KEY"):
        ModelRuntimeModule(
            settings.model_runtime,
            database,
            lifecycle=LifecycleRegistry(),
            web_settings=settings.web,
        ).build()


async def test_configured_projection_reads_the_current_pool_even_inside_an_old_request_pin(
    database, tmp_path
):
    old_catalog = load_model_profile_catalog(write_fake_profiles(tmp_path / "old.toml"))
    new_catalog = load_model_profile_catalog(keyed_profiles(tmp_path / "new.toml"))
    models = TaskModelExecutor(router=ModelRouter(old_catalog), pool=ModelClientPool())
    container = health_container(database, None)
    container.models = models
    try:
        with models.pin():
            models.apply_catalog(
                new_catalog, ModelClientPool(secret_overrides={"PROFILE_HEALTH_TEST_KEY": ""})
            )
            assert not (await build_health_payload(container))["llm_configured"]
        injected = FakeLLMProvider()
        models.apply_catalog(new_catalog, ModelClientPool(injected_profiles={"main": injected}))
        assert (await build_health_payload(container))["llm_configured"]
        assert injected.requests == []
        invalid = new_catalog.model_copy(update={"routes": {}})
        models.apply_catalog(invalid, ModelClientPool())
        with pytest.raises(KeyError):
            models.configured(ModelTask.CHAT_AGENT)
    finally:
        await models.close()
