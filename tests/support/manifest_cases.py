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

    async def snapshot():
        started.set()
        await release.wait()
        if failure:
            raise RuntimeError("configuration unavailable")

    tool = ChatTool(
        name="example",
        description="example",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        },
    )

    def build_registry(*args, **kwargs):
        assert args[0].declaration_only
        assert release.is_set()
        return SimpleNamespace(
            catalog=lambda context: SimpleNamespace(
                entries=(
                    SimpleNamespace(
                        descriptor=SimpleNamespace(
                            description=tool.description, as_chat_tool=lambda **kwargs: tool
                        )
                    ),
                )
            )
        )

    chat = SimpleNamespace(
        _runtime_config=SimpleNamespace(snapshot=AsyncMock(side_effect=snapshot)),
        _build_tool_registry=build_registry,
        # Faithful declaration fixture: this deployment has no plugin adapter.
        _plugin_tools=None,
    )
    contract = MainAgentContract(chat, state, code_enabled=True)
    pending = asyncio.create_task(contract.definitions())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert not pending.done() and contract._tools is None and not contract.revision
        release.set()
        with pytest.raises(RuntimeError, match="configuration unavailable"):
            await pending
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
    assert contract._tools is None and not contract.revision
    failure = False
    first = await contract.definitions()
    compact = await contract.model_definitions()
    assert contract.revision

    # Equal mappings with a different property order require a different revision.
    tool.parameters["properties"] = dict(reversed(tuple(tool.parameters["properties"].items())))
    reordered = MainAgentContract(chat, state, code_enabled=True)
    assert await reordered.definitions() == first
    assert reordered.revision != contract.revision
    # Invisible schema changes must still change the execution/recovery revision.
    assert await reordered.model_definitions() == compact
    assert await contract.definitions() == first

    # Failure during serialization must not publish an incomplete frozen object.
    tool.parameters["bad"] = object()
    invalid = MainAgentContract(chat, state, code_enabled=True)
    with pytest.raises(TypeError):
        await invalid.definitions()
    assert invalid._tools is None and not invalid.revision
    del tool.parameters["bad"]
    assert await invalid.definitions()
