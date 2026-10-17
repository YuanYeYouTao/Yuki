"""Admin apply, persisted startup catalog and real native adapters share defaults."""

import json

import httpx
import pytest
from tests.unit.test_admin_api import admin as admin
from tests.unit.test_admin_api import command

from yuki.application.lifecycle import LifecycleRegistry
from yuki.application.modules.web import WebModule
from yuki.domain.messages import (
    ChatMessage,
    ChatRequest,
    NativeToolDefinition,
    NativeToolType,
)
from yuki.model_runtime.models import ModelProtocol, ModelTask
from yuki.model_runtime.pool import create_model_provider
from yuki.model_runtime.profiles import load_model_profile_catalog, model_profile_environment
from yuki.web.models import WebMode, WebSearchRequest
from yuki.web.route_context import current_web_model_task


@pytest.mark.parametrize(
    "vendor,protocol,endpoint",
    [
        ("deepseek", "chat_completions", "https://deepseek.example/v1"),
        ("deepseek", "responses", "https://deepseek.example/v1"),
        ("gemini", "gemini", "https://gemini.example/v1beta"),
        ("anthropic", "anthropic_messages", "https://claude.example/v1"),
    ],
)
async def test_admin_apply_persists_native_defaults_and_dispatches_original_provider(
    admin, monkeypatch, tmp_path, vendor, protocol, endpoint
):
    client, api, _, models, profiles_path = admin
    files = api.commands.files
    files._settings = type(files._settings).model_validate(
        {
            **files._settings.model_dump(),
            "web_mode": WebMode.NATIVE,
            "web_search_backend": "model",
            "web_search_bridge_state_path": tmp_path / "search.sqlite3",
            "tavily_api_key": "",
        }
    )
    sent = []
    http_clients = []
    secret = "synthetic-provider-credential"

    def transport(request):
        sent.append(request)
        if protocol == "gemini":
            return httpx.Response(
                200,
                json={
                    "responseId": "original-search-response",
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {"role": "model", "parts": [{"text": "Search answer"}]},
                            "groundingMetadata": {
                                "webSearchQueries": ["actual query"],
                                "groundingChunks": [
                                    {
                                        "web": {
                                            "uri": "https://source.example/page",
                                            "title": "Source",
                                        }
                                    }
                                ],
                            },
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 100,
                        "candidatesTokenCount": 20,
                        "totalTokenCount": 120,
                        "cachedContentTokenCount": 70,
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "original-search-response",
                "stop_reason": "end_turn",
                "content": [
                    {
                        "type": "server_tool_use",
                        "id": "original-search-call",
                        "name": "web_search",
                        "input": {"query": "actual query"},
                    },
                    {
                        "type": "web_search_tool_result",
                        "tool_use_id": "original-search-call",
                        "content": [
                            {
                                "type": "web_search_result",
                                "url": "https://source.example/page",
                                "title": "Source",
                                "encrypted_content": "opaque-result",
                            }
                        ],
                    },
                    {"type": "text", "text": "Search answer"},
                ],
                "usage": {
                    "input_tokens": 30,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 70,
                    "cache_creation_input_tokens": 0,
                },
            },
        )

    def offline_provider(profile, api_key, *, client=None):
        offline = httpx.AsyncClient(
            base_url=profile.base_url.rstrip("/") + "/",
            transport=httpx.MockTransport(transport),
        )
        http_clients.append(offline)
        return create_model_provider(profile, api_key, client=offline)

    monkeypatch.setattr("yuki.model_runtime.pool.create_model_provider", offline_provider)
    monkeypatch.setattr("yuki.application.modules.web.create_model_provider", offline_provider)
    web = WebModule(
        files._settings.web,
        lifecycle=LifecycleRegistry(),
        catalog=files.loaded_catalog,
        clients=models._pool,
    )
    provider = web.build().provider
    assert provider is not None
    files._web_module = web
    try:
        current = (await client.get("/admin/v1/models")).json()["data"]
        document = current["document"]
        document["profiles"]["main"].update(
            provider=vendor,
            protocol=protocol,
            base_url=endpoint,
            api_key_env="TEST_NATIVE_SEARCH_KEY",
            model="selected-model",
            thinking_enabled=False,
        )
        document["profiles"]["main"].pop("reasoning_effort", None)
        body = command(
            current["revision"],
            document=document,
            api_keys={"TEST_NATIVE_SEARCH_KEY": secret},
        )
        applied = await client.post("/admin/v1/models/apply", json=body)
        assert applied.status_code == 200, applied.text
        assert applied.json()["data"]["effective_state"]["status"] == "applied"
        assert (await client.post("/admin/v1/models/apply", json=body)).json() == applied.json()
        catalog = files.loaded_catalog
        assert catalog.profiles["main"].protocol is ModelProtocol(protocol)
        assert catalog.profiles["main"].model == "selected-model"
        restarted = load_model_profile_catalog(
            profiles_path,
            environment=model_profile_environment(files._settings),
            web_mode=files._settings.web.mode,
            search_backend=files._settings.web_search_backend,
        )
        assert restarted == catalog
        assert secret not in profiles_path.read_text()
        assert secret not in (await client.get("/admin/v1/models")).text
        assert not sent  # Applying/replaying configuration performs no provider call.
        if protocol == "anthropic_messages":
            response = await models.execute(
                ModelTask.CHAT_AGENT,
                ChatRequest(
                    messages=(ChatMessage("user", "actual query"),),
                    native_tools=(NativeToolDefinition(NativeToolType.WEB_SEARCH),),
                ),
            )
            sources = response.citations
        else:
            token = current_web_model_task.set(ModelTask.CHAT_AGENT)
            try:
                response = await provider.search(WebSearchRequest("actual query"))
            finally:
                current_web_model_task.reset(token)
            sources = response.sources
        assert [source.url for source in sources] == ["https://source.example/page"]
        assert response.provider_request_id == "original-search-response"
        assert response.prompt_tokens == 100 and response.cached_prompt_tokens == 70
        assert response.completion_tokens == 20
        assert len(sent) == 1
        wire = json.loads(sent[0].content)
        if protocol == "gemini":
            assert str(sent[0].url) == endpoint + "/models/selected-model:generateContent"
            assert sent[0].headers["x-goog-api-key"] == secret
            assert wire["tools"] == [{"googleSearch": {}}]
        else:
            expected = (
                "https://deepseek.example/anthropic/v1/messages"
                if vendor == "deepseek"
                else endpoint + "/messages"
            )
            assert str(sent[0].url) == expected
            assert sent[0].headers["x-api-key"] == secret
            assert wire["model"] == "selected-model"
            assert wire["tools"][-1]["name"] == "web_search"
    finally:
        await provider.close()
        for offline in http_clients:
            await offline.aclose()
