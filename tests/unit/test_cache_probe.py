"""The opt-in probe uses actual configured adapters over mock HTTP only."""

import io
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import TypeAdapter
from tools import cache_probe_replay as cache_probe

from qq_ai_bot.domain.messages import ChatMessage, ChatRequest
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool


def profile(provider, protocol):
    return ModelProfile(
        id="selected-profile",
        provider=provider,
        protocol=protocol,
        model="selected-model",
        base_url="https://wire.invalid/v1",
        api_key_env="PROBE_TEST_KEY",
        timeout_seconds=17,
        max_retries=0,
        default_temperature=0.4,
        default_max_output_tokens=1024,
        thinking_enabled=provider != "custom-response-name",
        reasoning_effort=None if provider == "custom-response-name" else "low",
        capabilities=frozenset({ModelCapability.TOOLS, ModelCapability.REASONING}),
        headers={"x-connection-label": "original-setting"},
    )


def answer(protocol):
    if protocol is ModelProtocol.GEMINI:
        return {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"role": "model", "parts": [{"text": "合成建议"}]},
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 20,
                "candidatesTokenCount": 3,
                "totalTokenCount": 23,
                "cachedContentTokenCount": 5,
            },
        }
    if protocol is ModelProtocol.RESPONSES:
        return {
            "id": "synthetic-response",
            "status": "completed",
            "output": [
                {
                    "id": "synthetic-message",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "合成建议", "annotations": []}],
                }
            ],
            "usage": {
                "input_tokens": 20,
                "output_tokens": 3,
                "total_tokens": 23,
                "input_tokens_details": {"cached_tokens": 5},
            },
        }
    return {
        "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": "合成建议"}}
        ],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 3,
            "total_tokens": 23,
            "prompt_tokens_details": {"cached_tokens": 5},
        },
    }


@pytest.mark.parametrize(
    "vendor,protocol",
    [
        ("renamed-gemini-connection", ModelProtocol.GEMINI),
        ("deepseek", ModelProtocol.CHAT_COMPLETIONS),
        ("openrouter", ModelProtocol.CHAT_COMPLETIONS),
        ("deepseek", ModelProtocol.RESPONSES),
        ("openai", ModelProtocol.RESPONSES),
        ("custom-response-name", ModelProtocol.RESPONSES),
    ],
)
async def test_manual_probe_preserves_profile_wire_and_closes_clients(
    tmp_path, monkeypatch, vendor, protocol
):
    configured = profile(vendor, protocol)
    wires = []

    def transport(request):
        wires.append((request, json.loads(request.content)))
        return httpx.Response(200, json=answer(protocol))

    pools = []

    def pool_factory(**kwargs):
        pool = ModelClientPool(**kwargs)
        client = httpx.AsyncClient(
            base_url=configured.base_url, transport=httpx.MockTransport(transport)
        )
        pool._connection_pools[
            (
                configured.provider.casefold(),
                configured.base_url.rstrip("/"),
                configured.api_key_env,
            )
        ] = client
        pools.append(pool)
        return pool

    monkeypatch.setattr(cache_probe, "ModelClientPool", pool_factory)
    monkeypatch.setattr(
        cache_probe.sys,
        "stdin",
        io.StringIO(
            json.dumps({"profile": configured.model_dump(mode="json"), "api_key": "synthetic-key"})
        ),
    )
    root = tmp_path / "replay"
    current = root / "current"
    current.mkdir(parents=True)
    request = ChatRequest(
        messages=(ChatMessage("user", "synthetic input"),),
        model=configured.model,
        max_output_tokens=1024,
        thinking_enabled=configured.thinking_enabled,
        reasoning_effort=configured.reasoning_effort,
    )
    (current / "chatrequests.jsonl").write_bytes(
        TypeAdapter(ChatRequest).dump_json(request) + b"\n"
    )
    (current / "cases.json").write_text(json.dumps([{"name": "sample"}]))
    if protocol is ModelProtocol.GEMINI:
        fixture_pool = ModelClientPool(secret_overrides={configured.api_key_env: "synthetic-key"})
        try:
            payload = fixture_pool.get(configured)._build_payload(request)
            (current / "gemini.bodies.jsonl").write_text(json.dumps(payload) + "\n")
        finally:
            await fixture_pool.close()
    output = tmp_path / "result"
    await cache_probe.replay(
        SimpleNamespace(
            replay_root=root, variant="current", output=output, sequence="0", name="isolated-test"
        )
    )
    report = json.loads((output / "results.json").read_text())
    assert len(report) == len(wires) == 1
    assert report[0]["provider"] == vendor
    assert report[0]["protocol"] == protocol.value
    assert report[0]["http_status"] == 200
    assert report[0]["client_dispatches"] == 1
    for request, payload in wires:
        assert request.headers["x-connection-label"] == "original-setting"
        assert all(value == 17 for value in request.extensions["timeout"].values())
        assert payload.get("model", configured.model) == "selected-model"
        if protocol is ModelProtocol.GEMINI:
            assert request.headers["x-goog-api-key"] == "synthetic-key"
            assert request.url.path == "/v1/models/selected-model:generateContent"
            assert payload["generationConfig"]["maxOutputTokens"] == 1024
            assert payload["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "low"
        else:
            assert request.headers["authorization"] == "Bearer synthetic-key"
            if protocol is ModelProtocol.CHAT_COMPLETIONS:
                assert request.url.path == "/v1/chat/completions"
                if vendor == "deepseek":
                    assert payload["thinking"] == {"type": "enabled"}
                    assert "tool_choice" not in payload
                else:
                    assert payload["reasoning"] == {
                        "enabled": True,
                        "effort": "low",
                        "exclude": False,
                    }
                    assert "thinking" not in payload
            else:
                assert payload["max_output_tokens"] == 1024
                if vendor == "deepseek":
                    assert "store" not in payload and "include" not in payload
                else:
                    assert payload["store"] is False
                    assert payload["include"] == ["reasoning.encrypted_content"]
    assert all(client.is_closed for pool in pools for client in pool._connection_pools.values())
    assert all(provider._client.is_closed for pool in pools for provider in pool._clients.values())
