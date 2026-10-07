"""Preserve supplied healthy profile and isolated declaration-copy audit assertions."""

import asyncio
import json
from dataclasses import asdict

import httpx
import pytest
from tests.support.correctness_wire import KINDS, body
from tests.unit.test_history_dispatch_ownership import _scene

from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatResponse
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import ModelCapability, ModelProfile, ModelRoute, ModelTask
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.model_runtime.routes import ModelRouter


def settings(payload, kind):
    return {
        k: v
        for k, v in payload.items()
        if k != ("input" if kind == "responses" else "contents" if kind == "gemini" else "messages")
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(KINDS))
async def test_profile_hot_save_keeps_pinned_wire(kind):
    cls, vendor, protocol = KINDS[kind]
    wires = []

    async def transport(req):
        wires.append(json.loads(req.content))
        return httpx.Response(200, json=body(kind, ChatResponse("done", 0), len(wires)))

    async with httpx.AsyncClient(
        base_url="https://wire.invalid", transport=httpx.MockTransport(transport)
    ) as client:
        provider = cls(
            base_url="https://wire.invalid",
            api_key="synthetic",
            timeout_seconds=2,
            max_retries=0,
            client=client,
            **({"provider_name": "openai"} if kind == "chat" else {}),
        )

        def catalog(label, limit):
            profile = ModelProfile(
                id="profile",
                provider=vendor,
                protocol=protocol,
                base_url="https://wire.invalid",
                api_key_env="UNUSED",
                model=label,
                timeout_seconds=2,
                max_retries=0,
                default_temperature=0.5,
                default_max_output_tokens=limit,
                capabilities={ModelCapability.TOOLS, ModelCapability.REASONING},
            )
            return ModelProfileCatalog(
                profiles={"profile": profile},
                routes={task: ModelRoute(task=task, profile_id="profile") for task in ModelTask},
            )

        def pool():
            return ModelClientPool(injected_profiles={"profile": provider})

        executor = TaskModelExecutor(router=ModelRouter(catalog("old-model", 1024)), pool=pool())
        req = ChatRequest(messages=(ChatMessage("system", "fixed"), ChatMessage("user", "task")))
        try:
            with executor.pin():
                first_revision = executor.profile_revision(ModelTask.CHAT_AGENT)
                await executor.execute(ModelTask.CHAT_AGENT, req)
                executor.apply_catalog(catalog("new-model", 2048), pool())
                assert executor.profile_revision(ModelTask.CHAT_AGENT) == first_revision
                await executor.execute(ModelTask.CHAT_AGENT, req)
            assert executor.profile_revision(ModelTask.CHAT_AGENT) != first_revision
            await executor.execute(ModelTask.CHAT_AGENT, req)
        finally:
            await executor.close()
    assert wires[0] == wires[1]
    assert settings(wires[2], kind) != settings(wires[1], kind)
    if kind != "gemini":
        assert [w["model"] for w in wires] == ["old-model", "old-model", "new-model"]


@pytest.mark.asyncio
async def test_concurrent_contract_copies_and_discovery_are_isolated(database, tmp_path):
    _, _, chat, _, _ = await _scene(database, tmp_path, FakeLLMProvider(), code_enabled=True)
    contract = chat.runtime.runner.main_contract
    original = await contract.definitions()
    canonical = [asdict(t) for t in original]
    revision = contract.revision

    async def consume(index):
        defs = await contract.definitions()
        # Callers may customize their returned copy; this must not alter the source.
        defs[0].parameters["audit_local_marker"] = index
        await asyncio.sleep(0)
        return [asdict(t) for t in await contract.definitions()]

    copies = await asyncio.gather(*(consume(i) for i in range(8)))
    assert all(copy == canonical for copy in copies)
    assert contract.revision == revision
    if hasattr(contract, "script_api"):
        from qq_ai_bot.codemode.tool_visibility import lookup_tools

        for name in contract.script_api.schemas:
            reply = json.loads(lookup_tools(contract.script_api, json.dumps({"name": name})))
            reply["data"]["parameters"]["audit_local_marker"] = "local"
        assert [asdict(t) for t in await contract.definitions()] == canonical
        assert all("audit_local_marker" not in s for s in contract.script_api.schemas.values())
