"""Deployment manifest publication and schema-order regression cases."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qq_ai_bot.domain.messages import ChatTool
from qq_ai_bot.services.main_agent_contract import MainAgentContract


async def run_manifest_cases(state):
    started, release = asyncio.Event(), asyncio.Event()
    failure = True

    async def prepare(context):
        assert context.declaration_only
        started.set()
        await release.wait()
        if failure:
            raise RuntimeError("catalog unavailable")

    tool = ChatTool(
        name="example",
        description="example",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        },
    )

    def build_registry(*args, **kwargs):
        assert release.is_set()
        return SimpleNamespace(
            catalog=lambda context: SimpleNamespace(
                entries=(SimpleNamespace(descriptor=SimpleNamespace(as_chat_tool=lambda: tool)),)
            )
        )

    chat = SimpleNamespace(
        _runtime_config=SimpleNamespace(snapshot=AsyncMock(return_value=None)),
        _external_tool_providers=[SimpleNamespace(prepare_manifest=prepare)],
        _build_tool_registry=build_registry,
    )
    automation = SimpleNamespace(_registry=None)
    contract = MainAgentContract(chat, automation, state)
    pending = asyncio.create_task(contract.definitions())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert not pending.done() and contract._tools is None and not contract.revision
        release.set()
        with pytest.raises(RuntimeError, match="catalog unavailable"):
            await pending
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
    assert contract._tools is None and contract.automation_names == {} and not contract.revision
    failure = False
    first = await contract.definitions()
    assert contract.revision

    # Equal mappings with a different property order require a different revision.
    tool.parameters["properties"] = dict(reversed(tuple(tool.parameters["properties"].items())))
    reordered = MainAgentContract(chat, automation, state)
    assert await reordered.definitions() == first
    assert reordered.revision != contract.revision
    assert await contract.definitions() == first

    # Failure during serialization must not publish an incomplete frozen object.
    tool.parameters["bad"] = object()
    invalid = MainAgentContract(chat, automation, state)
    with pytest.raises(TypeError):
        await invalid.definitions()
    assert invalid._tools is None and not invalid.revision and invalid.automation_names == {}
    del tool.parameters["bad"]
    assert await invalid.definitions()
