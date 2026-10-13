"""The opt-in probe uses actual configured adapters over mock HTTP only."""

import json
import re

import httpx
import pytest
from tests.support.work_compaction import summary_json
from tools import cache_probe

from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    StructuredOutputMode,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.persistence.database import Database


def track_pool(monkeypatch):
    selected = []

    def pool(**kwargs):
        value = ModelClientPool(**kwargs)
        selected.append(value)
        return value

    monkeypatch.setattr(cache_probe, "ModelClientPool", pool)
    return selected


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
    pools = track_pool(monkeypatch)
    configured = profile(vendor, protocol)
    wires = []

    def transport(request):
        wires.append((request, json.loads(request.content)))
        return httpx.Response(200, json=answer(protocol))

    report = await cache_probe.run_experiment(
        profile=configured,
        api_key="synthetic-key",
        output=tmp_path / "probe.json",
        samples=1,
        warmups=1,
        prefix_characters=64,
        prefix_control=False,
        transport=httpx.MockTransport(transport),
    )
    assert report["scenarios"][0]["status"] == "complete"
    assert report["provider"] == vendor and report["protocol"] == protocol.value
    assert report["profile_settings"]["reasoning_effort"] == (
        configured.reasoning_effort.value if configured.reasoning_effort is not None else None
    )
    assert report["totals"]["known_input_tokens"] == 40
    assert report["totals"]["cached_input_tokens"] == 10
    assert all(
        report["accounting"][name]
        for name in ("physical_match", "unknown_usage_match", "logical_match")
    )
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


async def test_manual_work_compaction_probe_resumes_the_original_work(tmp_path):
    stages = []

    def transport(request):
        body = json.loads(request.content)
        text = body["contents"][-1]["parts"][-1].get("text", "")
        payload = answer(ModelProtocol.GEMINI)
        try:
            source = json.loads(text)
        except json.JSONDecodeError:
            source = None
        if isinstance(source, dict) and "task_material" in source:
            stages.append("compaction")
            payload["candidates"][0]["content"]["parts"][0]["text"] = summary_json(source)
        else:
            stages.append("main")
        return httpx.Response(200, json=payload)

    configured = profile("renamed-gemini-connection", ModelProtocol.GEMINI).model_copy(
        update={"structured_output_mode": StructuredOutputMode.TEXT_JSON}
    )
    report = await cache_probe.run_experiment(
        profile=configured,
        api_key="synthetic-key",
        output=tmp_path / "work-compaction.json",
        scenarios=("C06",),
        samples=1,
        warmups=1,
        prefix_characters=64,
        prefix_control=False,
        compaction_mode="work",
        transport=httpx.MockTransport(transport),
    )
    scenario = report["scenarios"][0]
    assert scenario["status"] == "complete"
    evidence = scenario["runtime_evidence"]
    assert evidence["compaction"]["explicit_boundary"]
    assert all(
        identity == evidence["original_work_ids"][0] for identity in evidence["request_work_ids"]
    )
    assert stages == ["main", "compaction", "main"]
    assert report["accounting"]["physical_match"]


async def test_manual_probe_keeps_failed_request_and_closes_clients(tmp_path, monkeypatch):
    pools = track_pool(monkeypatch)

    def transport(request):
        raise httpx.ReadError("synthetic transport failure", request=request)

    report = await cache_probe.run_experiment(
        profile=profile("renamed-gemini-connection", ModelProtocol.GEMINI),
        api_key="synthetic-key",
        output=tmp_path / "failed.json",
        samples=1,
        warmups=1,
        prefix_characters=64,
        prefix_control=False,
        transport=httpx.MockTransport(transport),
    )
    assert report["scenarios"][0]["status"] == "failed"
    assert report["totals"]["requests"] == 1
    assert report["totals"]["reported_input_tokens"] is None
    assert report["physical_calls"][0]["unknown_usage"] is True
    assert all(client.is_closed for pool in pools for client in pool._connection_pools.values())
    assert all(provider._client.is_closed for pool in pools for provider in pool._clients.values())


async def test_manual_work_restart_probe_preserves_receipt_and_closes_database(
    tmp_path, monkeypatch
):
    databases = []
    closed = []
    initialize = Database.__init__
    close = Database.close

    def observe_initialize(self, url):
        initialize(self, url)
        databases.append(self)

    async def observe_close(self):
        closed.append(self)
        await close(self)

    monkeypatch.setattr(Database, "__init__", observe_initialize)
    monkeypatch.setattr(Database, "close", observe_close)
    requests = []

    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        payload = answer(ModelProtocol.GEMINI)
        if len(requests) == 1:
            text = body["contents"][-1]["parts"][-1]["text"]
            handle = re.search(r"handle=([^，]+)", text).group(1)
            payload["candidates"][0]["content"]["parts"] = [
                {
                    "thoughtSignature": "synthetic-original-read",
                    "functionCall": {
                        "id": "original-read",
                        "name": "read_tool_artifact",
                        "args": {"handle": handle, "operation": "text", "limit": 100},
                    },
                }
            ]
        return httpx.Response(200, json=payload)

    report = await cache_probe.run_experiment(
        profile=profile("renamed-gemini-connection", ModelProtocol.GEMINI),
        api_key="synthetic-key",
        output=tmp_path / "work-restart.json",
        scenarios=("C05",),
        samples=1,
        warmups=1,
        prefix_characters=64,
        prefix_control=False,
        transport=httpx.MockTransport(transport),
    )
    scenario = report["scenarios"][0]
    assert scenario["status"] == "complete"
    evidence = scenario["runtime_evidence"]
    assert evidence["restart"]["work_id_unchanged"]
    for counter in ("model_requests", "tool_calls"):
        assert evidence["restart"][f"{counter}_before"] == evidence["restart"][f"{counter}_after"]
    assert evidence["accepted_read"]["same_receipt"]
    assert evidence["accepted_read"]["business_invocations"] == 1
    assert databases and all(database in closed for database in databases)
    assert report["accounting"]["physical_match"]
