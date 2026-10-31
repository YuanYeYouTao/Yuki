"""Model administration performs one real HTTP request without saving draft secrets."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import jsonschema
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from tests.unit.test_admin_api import READER, TOKEN, command
from tests.unit.test_admin_api import admin as admin

from yuki.admin_api.http import attach_admin_api
from yuki.model_runtime.models import ModelTask
from yuki.model_runtime.repository import ModelInvocationRepository
from yuki.model_runtime.secrets import model_secrets_path

SECRET = "synthetic-probe-key-not-for-storage"


def profile(base, protocol="chat_completions"):
    return {
        "id": "probe",
        "provider": "custom-test-service",
        "protocol": protocol,
        "base_url": f"{base}/upstream/{protocol}/v1",
        "api_key_env": "PROBE_KEY",
        "model": "raw-model-a",
        "timeout_seconds": 2,
        "max_retries": 3,
        "default_temperature": 0.7,
        "default_max_output_tokens": 2048,
        "capabilities": ["tools"],
    }


@pytest.fixture
async def model_http(admin, request):
    _, api, _, _, profiles = admin
    state = {"calls": [], "behavior": "ok", "responses": []}
    app = FastAPI()
    attach_admin_api(app, lambda: api)

    @app.api_route(
        "/upstream/{protocol}/v1/{path:path}", methods=["GET", "POST"], include_in_schema=False
    )
    async def upstream(protocol: str, path: str, incoming: Request):
        payload = await incoming.json() if incoming.method == "POST" else None
        credential = incoming.headers.get(
            {"gemini": "x-goog-api-key", "anthropic_messages": "x-api-key"}.get(
                protocol, "authorization"
            )
        )
        assert credential == (
            f"Bearer {SECRET}" if protocol not in {"gemini", "anthropic_messages"} else SECRET
        )
        state["calls"].append(
            {
                "method": incoming.method,
                "protocol": protocol,
                "path": path,
                "payload": payload,
                "credential_matched": True,
            }
        )
        behavior = state["behavior"]
        if behavior == "timeout":
            await asyncio.sleep(0.2)
        if behavior in {"authentication", "rate_limit", "quota", "unavailable", "unsupported"}:
            status, code = {
                "authentication": (401, "invalid_api_key"),
                "rate_limit": (429, "rate_limit_exceeded"),
                "quota": (429, "insufficient_quota"),
                "unavailable": (503, "upstream_unavailable"),
                "unsupported": (404, "not_found"),
            }[behavior]
            return JSONResponse({"error": {"code": code, "message": SECRET}}, status_code=status)
        if behavior == "protocol":
            return Response("not-json", media_type="application/json")
        if incoming.method == "GET":
            if protocol == "gemini":
                return {
                    "models": [
                        {"name": "models/raw-model-a", "displayName": "Raw A"},
                        {"name": "models/second@opaque"},
                    ]
                }
            return {
                "data": [{"id": "raw-model-a", "display_name": "Raw A"}, {"id": "second@opaque"}]
            }
        if protocol == "gemini":
            return {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "ok"}]},
                        "finishReason": "STOP",
                    }
                ]
            }
        if protocol == "anthropic_messages":
            return {
                "id": "probe-response",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
            }
        if protocol == "responses":
            return {
                "id": "probe-response",
                "status": "completed",
                "output": [
                    {
                        "id": "probe-message",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ],
            }
        return {
            "choices": [
                {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
            ]
        }

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    with config.bind_socket() as listener:
        server = uvicorn.Server(config)
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                if serving.done():
                    await serving
                    raise AssertionError("isolated HTTP server exited before startup")
                await asyncio.sleep(0.01)
            base = f"http://127.0.0.1:{listener.getsockname()[1]}"
            async with httpx.AsyncClient(
                base_url=base,
                headers={"Authorization": f"Bearer {TOKEN}"},
                trust_env=False,
            ) as client:
                yield SimpleNamespace(client=client, base=base, state=state, profiles=profiles)
        finally:
            server.should_exit = True
            await serving
            artifact = Path("test-results/model-connections")
            await asyncio.to_thread(artifact.mkdir, parents=True, exist_ok=True)
            (artifact / f"{request.node.name}.json").write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )


@pytest.mark.parametrize(
    "protocol", ["chat_completions", "responses", "anthropic_messages", "gemini"]
)
async def test_draft_probe_and_discovery_use_real_protocol_without_save(
    model_http, protocol, caplog
):
    rig = model_http
    before = rig.profiles.read_bytes()
    draft = profile(rig.base, protocol)
    response = await rig.client.post(
        "/admin/v1/models/test",
        json={
            "draft_profile": draft,
            "api_key": SECRET,
        },
    )
    rig.state["responses"].append(response.json())
    assert response.status_code == 200
    result = response.json()["data"]
    assert result["ok"] is True and result["error_category"] is None
    assert result["latency_seconds"] >= 0
    assert len(rig.state["calls"]) == 1
    sent = rig.state["calls"][0]
    body = sent["payload"]
    maximum = body.get(
        "max_tokens",
        body.get(
            "max_output_tokens",
            body.get(
                "max_completion_tokens", body.get("generationConfig", {}).get("maxOutputTokens")
            ),
        ),
    )
    assert 1 <= maximum <= 16
    assert not body.get("tools") and "tool_choice" not in body
    connection = {
        key: draft[key]
        for key in ("provider", "protocol", "base_url", "api_key_env", "timeout_seconds")
    }
    response = await rig.client.post(
        "/admin/v1/models/discover",
        json={
            "draft_connection": connection,
            "api_key": SECRET,
        },
    )
    rig.state["responses"].append(response.json())
    assert response.status_code == 200
    prefix = "models/" if protocol == "gemini" else ""
    assert response.json()["data"] == {
        "models": [
            {"id": prefix + "raw-model-a", "display_name": "Raw A"},
            {"id": prefix + "second@opaque"},
        ],
        "error_category": None,
    }
    assert len(rig.state["calls"]) == 2 and rig.state["calls"][1]["path"] == "models"
    assert rig.profiles.read_bytes() == before
    assert not model_secrets_path(rig.profiles).exists()
    assert SECRET not in json.dumps(rig.state) and SECRET not in caplog.text
    stats = await rig.client.get("/admin/v1/models/stats")
    assert stats.json()["data"] == {"by_task": {}, "by_profile": {}}


@pytest.mark.parametrize(
    "behavior", ["authentication", "rate_limit", "quota", "timeout", "protocol", "unavailable"]
)
async def test_probe_classifies_one_attempt_without_echoing_upstream_error(
    model_http, behavior, caplog
):
    rig = model_http
    rig.state["behavior"] = behavior
    draft = profile(rig.base)
    if behavior == "timeout":
        draft["timeout_seconds"] = 0.05
    response = await rig.client.post(
        "/admin/v1/models/test",
        json={
            "draft_profile": draft,
            "api_key": SECRET,
        },
    )
    rig.state["responses"].append(response.json())
    assert response.status_code == 200
    assert response.json()["data"]["ok"] is False
    assert response.json()["data"]["error_category"] == behavior
    assert len(rig.state["calls"]) == 1
    assert SECRET not in response.text and SECRET not in caplog.text
    assert not model_secrets_path(rig.profiles).exists()


async def test_unsupported_discovery_and_denied_requests_do_not_probe_or_persist(model_http):
    rig = model_http
    draft = profile(rig.base)
    connection = {
        key: draft[key]
        for key in ("provider", "protocol", "base_url", "api_key_env", "timeout_seconds")
    }
    rig.state["behavior"] = "unsupported"
    response = await rig.client.post(
        "/admin/v1/models/discover",
        json={
            "draft_connection": connection,
            "api_key": SECRET,
        },
    )
    rig.state["responses"].append(response.json())
    assert response.status_code == 200
    assert response.json()["data"] == {"models": [], "error_category": "unsupported"}
    for endpoint, payload in (
        ("test", {"draft_profile": draft}),
        ("discover", {"draft_connection": connection}),
    ):
        denied = await rig.client.post(
            f"/admin/v1/models/{endpoint}",
            headers={"Authorization": f"Bearer {READER}"},
            json={**payload, "api_key": SECRET},
        )
        assert denied.status_code == 403 and SECRET not in denied.text
    denied = await rig.client.get(
        "/admin/v1/models/stats", headers={"Authorization": f"Bearer {READER}"}
    )
    assert denied.status_code == 403
    malformed = await rig.client.post(
        "/admin/v1/models/test",
        json={
            "draft_profile": {**draft, "model": ""},
            "api_key": SECRET,
        },
    )
    assert malformed.status_code == 422 and SECRET not in malformed.text
    assert len(rig.state["calls"]) == 1


async def test_saved_profile_uses_sidecar_and_stats_keep_original_invocations(model_http, database):
    rig = model_http
    current = (await rig.client.get("/admin/v1/models")).json()["data"]
    document = current["document"]
    selected = profile(rig.base)
    document["profiles"]["probe"] = {key: value for key, value in selected.items() if key != "id"}
    applied = await rig.client.post(
        "/admin/v1/models/apply",
        json=command(
            current["revision"],
            document=document,
            api_keys={"PROBE_KEY": SECRET},
        ),
    )
    assert applied.status_code == 200 and applied.json()["data"]["success"] is True
    before = rig.profiles.read_bytes(), model_secrets_path(rig.profiles).read_bytes()
    repository = ModelInvocationRepository(database)
    await repository.record(
        task=ModelTask.MEMORY_DREAM,
        profile_id="original-profile",
        provider="original-provider",
        model="original-model",
        success=True,
        prompt_tokens=20,
        completion_tokens=3,
        total_tokens=23,
        cached_prompt_tokens=7,
        latency_seconds=0.25,
        error_category=None,
    )
    await repository.record(
        task=ModelTask.MEMORY_DREAM,
        profile_id="original-profile",
        provider="original-provider",
        model="original-model",
        success=False,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        cached_prompt_tokens=None,
        latency_seconds=0.75,
        error_category="unknown",
    )
    for endpoint in ("test", "discover"):
        response = await rig.client.post(
            f"/admin/v1/models/{endpoint}", json={"profile_id": "probe"}
        )
        rig.state["responses"].append(response.json())
        assert response.status_code == 200 and response.json()["data"]["error_category"] is None
    stats = (await rig.client.get("/admin/v1/models/stats")).json()["data"]
    rig.state["responses"].append(stats)
    assert stats["by_task"]["memory_dream"] == stats["by_profile"]["original-profile"]
    facts = stats["by_task"]["memory_dream"]
    assert (facts["invocations"], facts["successes"], facts["failures"]) == (2, 1, 1)
    assert (
        facts["prompt_tokens"],
        facts["completion_tokens"],
        facts["cached_prompt_tokens"],
        facts["unknown_usage"],
        facts["average_latency_seconds"],
    ) == (20, 3, 7, 1, 0.5)
    assert before == (rig.profiles.read_bytes(), model_secrets_path(rig.profiles).read_bytes())
    assert SECRET not in json.dumps(rig.state)


async def test_model_contract_validates_real_bundle_and_mutation_result(model_http):
    rig = model_http
    contract = (await rig.client.get("/openapi.json")).json()
    read_schema = contract["paths"]["/admin/v1/models"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert "$ref" in read_schema
    current = (await rig.client.get("/admin/v1/models")).json()
    jsonschema.validate(current, {**read_schema, "components": contract["components"]})
    applied = await rig.client.post(
        "/admin/v1/models/apply",
        json=command(
            current["data"]["revision"],
            document=current["data"]["document"],
        ),
    )
    assert applied.status_code == 200
    result_schema = contract["paths"]["/admin/v1/models/apply"]["post"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    assert "$ref" in result_schema
    jsonschema.validate(applied.json(), {**result_schema, "components": contract["components"]})
    rig.profiles.write_text("not valid TOML", encoding="utf-8")
    invalid = (await rig.client.get("/admin/v1/models")).json()
    assert invalid["data"]["valid"] is False
    jsonschema.validate(invalid, {**read_schema, "components": contract["components"]})
