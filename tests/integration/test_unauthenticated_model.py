"""Explicit credential-free profiles use real loopback HTTP without a synthetic key."""

import asyncio
import json

import pytest

from yuki.deployment_setup.service import (
    SetupConfiguration,
    SetupPaths,
    SetupValidationError,
    build_model_profiles,
    validate_configuration,
)
from yuki.domain.messages import ChatMessage, ChatRequest
from yuki.llm.base import LLMConfigurationError
from yuki.model_runtime.models import ModelProfile
from yuki.model_runtime.pool import ModelClientPool, create_model_provider


def profile(base_url, key_env=""):
    return ModelProfile(
        id="local",
        provider="openai_compatible",
        base_url=base_url,
        api_key_env=key_env,
        model="local-model",
        timeout_seconds=5,
        max_retries=0,
        default_temperature=0.7,
        default_max_output_tokens=64,
    )


async def test_explicit_no_key_reaches_real_http_without_authorization():
    observed = []

    async def handle(reader, writer):
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        headers = dict(line.split(": ", 1) for line in head.split("\r\n")[1:] if line)
        await reader.readexactly(int(headers["Content-Length"]))
        observed.append({key.lower(): value for key, value in headers.items()})
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "local response"},
                        "finish_reason": "stop",
                    }
                ]
            }
        ).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Connection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    pool = ModelClientPool()
    try:
        selected = profile(f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1")
        assert pool.configured(selected)
        result = await pool.get(selected).complete(
            ChatRequest(model=selected.model, messages=(ChatMessage(role="user", content="hello"),))
        )
        assert result.content == "local response"
        assert len(observed) == 1 and "authorization" not in observed[0]
    finally:
        await pool.close()
        server.close()
        await server.wait_closed()


async def test_declared_missing_secret_still_fails_before_http(monkeypatch):
    monkeypatch.delenv("YUKI_TEST_REQUIRED_SECRET", raising=False)
    pool = ModelClientPool()
    selected = profile("http://127.0.0.1:1/v1", "YUKI_TEST_REQUIRED_SECRET")
    try:
        assert not pool.configured(selected)
        with pytest.raises(LLMConfigurationError, match="YUKI_TEST_REQUIRED_SECRET"):
            pool.get(selected)
    finally:
        await pool.close()


def test_setup_uses_the_primary_profile_credential_declaration(tmp_path):
    (tmp_path / "character").mkdir()
    (tmp_path / "character/SOUL.md").write_text("Local test", encoding="utf-8")
    environment = {
        "LLM_BASE_URL": "http://127.0.0.1:11434/v1",
        "LLM_MODEL": "local-model",
        "LLM_API_KEY": "",
        "WEB_MODE": "disabled",
        "COMPOSE_PROFILES": "",
    }
    for key_env in ("", "LLM_API_KEY"):
        models = build_model_profiles(
            main_protocol="chat_completions", flash_enabled=False, main_api_key_env=key_env
        )
        configuration = SetupConfiguration(environment, models, None)
        if key_env:
            with pytest.raises(SetupValidationError, match="LLM_API_KEY"):
                validate_configuration(SetupPaths(tmp_path), configuration)
        else:
            validate_configuration(SetupPaths(tmp_path), configuration)


async def test_direct_factory_preserves_declared_secret_failure_at_dispatch():
    selected = profile("http://127.0.0.1:1/v1", "YUKI_TEST_REQUIRED_SECRET")
    provider = create_model_provider(selected, "")
    try:
        with pytest.raises(LLMConfigurationError, match="not configured"):
            await provider.complete(
                ChatRequest(
                    model=selected.model, messages=(ChatMessage(role="user", content="search"),)
                )
            )
    finally:
        await provider.close()
