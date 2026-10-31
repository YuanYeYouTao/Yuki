"""Saved auxiliary connections drive the original vision and ASR over real HTTP."""

from __future__ import annotations

import json

from tests.integration.test_embedding_connection_bundle import embedding_http as embedding_http
from tests.integration.test_embedding_connection_bundle import evidence
from tests.support.model_profiles import write_fake_profiles

from yuki.admin.config_files import ConfigFileService
from yuki.application.lifecycle import LifecycleRegistry
from yuki.application.modules.asr import build_asr
from yuki.application.modules.media import MediaModule
from yuki.config import Settings
from yuki.memory.embedding.runtime import MemoryEmbeddingRuntime
from yuki.model_runtime.connections import EmbeddingConnection
from yuki.model_runtime.pool import ModelClientPool
from yuki.model_runtime.profiles import load_model_profile_catalog
from yuki.model_runtime.secrets import model_secret_environment, read_model_secrets
from yuki.vision.models import PreparedFrame, PreparedVisualInput


async def test_bundle_vision_asr_and_search_use_saved_connections(tmp_path, embedding_http):
    url, state = embedding_http
    path = write_fake_profiles(tmp_path / "model_profiles.toml")
    settings = Settings(
        _env_file=None,
        model_profiles_file=path,
        web_mode="disabled",
        vision_enabled=False,
        asr_enabled=False,
    )
    files = ConfigFileService(settings)
    before = await files.read()
    document = before["document"]
    document.update(
        vision={
            "enabled": True,
            "base_url": url,
            "api_key_env": "VISION_TEST_KEY",
            "model": "fixture-vision",
        },
        asr={
            "enabled": True,
            "base_url": url,
            "api_key_env": "ASR_TEST_KEY",
            "model": "fixture-asr",
            "protocol": "chat_completions",
        },
        search={"tavily_api_key_env": "SEARCH_TEST_KEY"},
    )
    keys = {
        "VISION_TEST_KEY": "fixture-private-vision",
        "ASR_TEST_KEY": "fixture-private-asr",
        "SEARCH_TEST_KEY": "fixture-private-search",
    }
    await files.save(before["revision"], {"document": document, "api_keys": keys})
    after = await files.read()
    assert after["apply_mode"] == "restart"
    assert set(after["saved_connection_keys"]) == {"vision", "asr", "search"}
    for secret in keys.values():
        assert secret not in json.dumps(after)
        assert secret not in path.read_text()
    catalog = load_model_profile_catalog(path, web_mode=settings.web.mode)
    saved = read_model_secrets(path)[1]
    assert catalog.vision is not None and catalog.asr is not None and catalog.search is not None
    asr = build_asr(
        settings,
        LifecycleRegistry(),
        connection=catalog.asr,
        api_key=saved[catalog.asr.api_key_env],
    )
    vision = MediaModule._build_provider(
        settings.vision, connection=catalog.vision, api_key=saved[catalog.vision.api_key_env]
    )
    assert asr._provider is not None and vision is not None
    try:
        state["response"] = {
            "choices": [{"message": {"content": "fixture transcript"}, "finish_reason": "stop"}]
        }
        assert await asr._provider.transcribe(b"fixture audio") == "fixture transcript"
        assert state["requests"][-1]["body"]["model"] == "fixture-asr"
        state["response"] = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "items": [{"index": 1, "description": "fixture square"}],
                                "overall_description": "fixture",
                            }
                        )
                    }
                }
            ]
        }
        inputs = (
            PreparedVisualInput(
                media_hash="fixture",
                animated=False,
                source="current",
                frames=(
                    PreparedFrame(
                        content_hash="fixture",
                        mime_type="image/png",
                        width=1,
                        height=1,
                        frame_index=0,
                        frame_count=1,
                        data_url="data:image/png;base64,aW1hZ2U=",
                    ),
                ),
            ),
        )
        observed = await vision.analyze(inputs, "describe")
        assert observed.items[0].description == "fixture square"
        assert state["requests"][-1]["body"]["model"] == "fixture-vision"
        assert len(state["requests"]) == 2
        assert all(request["authorized"] for request in state["requests"])
        evidence("auxiliary-connections", {"public_bundle": after, "requests": state["requests"]})
    finally:
        await asr.close()
        await vision.close()


async def test_disabled_explicit_connections_do_not_use_legacy_credentials(tmp_path):
    path = write_fake_profiles(tmp_path / "model_profiles.toml")
    settings = Settings(
        _env_file=None,
        model_profiles_file=path,
        web_mode="disabled",
        vision_enabled=True,
        vision_provider="qwen",
        vision_base_url="https://legacy.invalid/v1",
        vision_api_key="fixture-legacy",
        asr_enabled=True,
        asr_base_url="https://legacy.invalid/v1",
        asr_api_key="fixture-legacy",
    )
    files = ConfigFileService(settings)
    before = await files.read()
    document = before["document"]
    for name in ("vision", "asr"):
        document[name] = {
            "enabled": False,
            "base_url": "http://127.0.0.1:1/v1",
            "api_key_env": "",
            "model": "disabled",
        }
    await files.save(before["revision"], {"document": document})
    catalog = load_model_profile_catalog(path, web_mode=settings.web.mode)
    service = build_asr(settings, LifecycleRegistry(), connection=catalog.asr)
    try:
        assert service._provider is None
        assert MediaModule._build_provider(settings.vision, connection=catalog.vision) is None
        evidence("disabled-connections", {"asr": service.health(), "vision": "disabled"})
    finally:
        await service.close()


async def test_settings_dotenv_credentials_reach_explicit_connection(
    tmp_path, embedding_http, monkeypatch
):
    url, state = embedding_http
    monkeypatch.delenv("MEMORY_EMBEDDING_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("MEMORY_EMBEDDING_API_KEY=fixture-dotenv-private\n", encoding="utf-8")
    settings = Settings(
        _env_file=env_file,
        model_profiles_file=write_fake_profiles(tmp_path / "models.toml"),
        web_mode="disabled",
        memory_embedding_enabled=False,
    )
    pool = ModelClientPool(secret_overrides=model_secret_environment(settings.model_runtime))
    connection = EmbeddingConnection(
        provider="openai_compatible",
        base_url=url,
        api_key_env="MEMORY_EMBEDDING_API_KEY",
        model="fixture-dotenv",
        dimensions=2,
    )
    provider = MemoryEmbeddingRuntime._build_provider(
        settings, connection, pool.api_key_for_alias(connection.api_key_env)
    )
    assert provider is not None
    try:
        result = await provider.embed_query("fixture")
        assert result.vectors[0].dimensions == 2
        assert state["requests"][0]["authorized"]
        assert "fixture-dotenv-private" not in json.dumps(state["requests"])
        evidence(
            "dotenv-connection",
            {"requests": state["requests"], "profile": provider.profile.model_dump()},
        )
    finally:
        await provider.close()
        await pool.close()
