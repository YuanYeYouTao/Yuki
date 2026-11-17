"""Setup -> persisted profile -> provider HTTP, with no external model calls.

Failure cases fixed before implementation: missing defaults on supported official
Responses models; leaking OpenAI fields into older/unknown models, Chat, proxies,
or other providers; overwriting an existing opt-out during setup or upgrade.
"""

import json
from pathlib import Path

import httpx
import pytest

from yuki.deployment_setup.desktop import DesktopConfiguration, configure
from yuki.deployment_setup.service import EnvironmentDocument, SetupPaths
from yuki.domain.messages import ChatMessage, ChatRequest
from yuki.model_runtime import ModelCapability, ModelTask, load_model_profile_catalog
from yuki.model_runtime.pool import create_model_provider
from yuki.prompting.compiler import PromptCompiler
from yuki.prompting.models import PromptChannel, PromptContribution, PromptProgram, PromptStability
from yuki.web.models import WebMode

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "overrides,existing,enabled",
    [
        pytest.param({"model": "gpt-5.6"}, False, True, id="first-supported-model"),
        pytest.param({"model": "gpt-6-sol"}, False, True, id="integer-major-version"),
        pytest.param(
            {"base_url": "https://api.openai.com/v1/"},
            False,
            True,
            id="current-model-trailing-slash",
        ),
        pytest.param({"model": "gpt-5.5"}, False, False, id="older-model"),
        pytest.param({"model": "custom-model"}, False, False, id="unknown-model"),
        pytest.param({"protocol": "chat_completions"}, False, False, id="chat-adapter"),
        pytest.param(
            {"base_url": "https://proxy.invalid/v1"},
            False,
            False,
            id="custom-endpoint-openai-label",
        ),
        pytest.param(
            {"provider": "openai_compatible"},
            False,
            False,
            id="compatible-provider",
        ),
        pytest.param(
            {
                "provider": "deepseek",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
            },
            False,
            False,
            id="deepseek-responses",
        ),
        pytest.param({"provider": None}, False, False, id="unspecified-provider"),
        pytest.param({}, True, False, id="existing-profile-stays-opted-out"),
    ],
)
async def test_setup_cache_default_reaches_http_without_changing_existing_profiles(
    tmp_path,
    monkeypatch,
    tool_delivery_evidence,
    overrides,
    existing,
    enabled,
):
    (tmp_path / ".env.example").write_bytes((ROOT / ".env.example").read_bytes())
    (tmp_path / "config").mkdir()
    (tmp_path / "config/SOUL.md").write_text("Synthetic setup persona", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    paths = SetupPaths(tmp_path)
    if existing:
        paths.model_profiles.parent.mkdir()
        paths.model_profiles.write_text(
            'schema_version = 3\n[profiles.primary_agent]\nprovider = "openai"\n'
            'protocol = "responses"\nbase_url_env = "LLM_BASE_URL"\n'
            'model_env = "LLM_MODEL"\napi_key_env = "LLM_API_KEY"\n'
            "timeout_seconds = 120\nmax_retries = 0\n"
            "default_temperature = 0.7\ndefault_max_output_tokens = 8192\n"
            'capabilities = ["tools"]\n[routes]\n'
            + "".join(f'{task.value} = "primary_agent"\n' for task in ModelTask),
            encoding="utf-8",
        )
        original_profiles = paths.model_profiles.read_bytes()
    body = DesktopConfiguration.model_validate(
        {
            "protocol": "responses",
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-6.1-terra",
            "api_key": "synthetic-setup-key",
            **overrides,
        }
    )
    assert configure(paths, body)["configured"] is True
    if existing:
        assert paths.model_profiles.read_bytes() == original_profiles
    saved = {path: path.read_bytes() for path in (paths.env, paths.model_profiles, paths.soul)}
    assert configure(paths, body)["preserved"] is True
    assert {path: path.read_bytes() for path in saved} == saved

    environment = EnvironmentDocument.load(paths).values()
    catalog = load_model_profile_catalog(
        paths.model_profiles,
        environment=environment,
        web_mode=WebMode.DISABLED,
    )
    profile = catalog.profiles[catalog.routes[ModelTask.CHAT_AGENT].profile_id]
    evidence_dir, evidence = tool_delivery_evidence
    (evidence_dir / "model_profiles.toml").write_bytes(paths.model_profiles.read_bytes())
    evidence.update(
        expected_breakpoints=enabled, existing_profile=existing, transport="MockTransport"
    )
    compiled = PromptCompiler().compile(
        PromptProgram(
            contributions=(
                PromptContribution(
                    id="persona",
                    channel=PromptChannel.PERSONA,
                    stability=PromptStability.STATIC,
                    content="Stable synthetic instructions",
                ),
            )
        ),
        history=(
            ChatMessage(role="user", content="Previous synthetic question"),
            ChatMessage(role="assistant", content="Previous synthetic answer"),
        ),
        current_message=ChatMessage(role="user", content="Current synthetic question"),
    )
    wire = []

    def transport(request):
        payload = json.loads(request.content)
        wire.append(payload)
        evidence.update(url=str(request.url), request=payload)
        return httpx.Response(
            200,
            json={
                "id": "synthetic-response",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Synthetic reply"}],
                    }
                ],
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Synthetic reply"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(
        base_url=profile.base_url.rstrip("/") + "/",
        transport=httpx.MockTransport(transport),
    ) as client:
        adapter = create_model_provider(profile, "synthetic-setup-key", client=client)
        result = await adapter.complete(
            ChatRequest(model=profile.model, messages=compiled.messages)
        )
    assert result.content == "Synthetic reply"
    assert len(wire) == 1
    assert (ModelCapability.PROMPT_CACHE_BREAKPOINTS in profile.capabilities) is enabled
    if enabled:
        assert evidence["url"] == "https://api.openai.com/v1/responses"
        assert "instructions" not in wire[0]
        assert "prompt_cache_options" not in wire[0]  # Retain default implicit caching and TTL.
        messages = wire[0]["input"]
        assert [item["role"] for item in messages] == ["developer", "user", "assistant", "user"]
        assert [
            index
            for index, item in enumerate(messages)
            if isinstance(item["content"], list)
            and item["content"][-1].get("prompt_cache_breakpoint") == {"mode": "explicit"}
        ] == [0, 1]
        assert json.dumps(wire[0]).count('"prompt_cache_breakpoint"') == 2
    else:
        assert "prompt_cache_breakpoint" not in json.dumps(wire[0])
