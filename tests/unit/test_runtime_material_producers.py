"""Actual producer paths retain useful values without auxiliary prompt copies."""

from __future__ import annotations

import json

import pytest

from qq_ai_bot.domain.messages import ChatMessage, ChatRequest
from qq_ai_bot.model_runtime.capacity import ModelCapacity, estimate_request_tokens
from qq_ai_bot.plugin_host.extension_registry import ExtensionKind, ExtensionRegistry
from qq_ai_bot.plugin_host.prompt_adapter import PluginPromptAdapter
from qq_ai_bot.prompting import (
    PromptChannel,
    PromptCompiler,
    PromptContribution,
    PromptProgram,
    PromptTrust,
)
from qq_ai_bot.services.model_context_projection import project_short_state
from qq_ai_bot.services.prompt_registry import PromptRegistry
from qq_ai_bot.workspace.short_state import ShortState
from qq_ai_bot.workspace.store import WorkspaceStore
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.models import PromptFragment, PromptStage, TrustedLevel
from yuki_plugin_sdk.permissions import PluginPermission


@pytest.mark.asyncio
async def test_short_state_first_write_and_clear_only_return_the_changed_slot(tmp_path) -> None:
    state = ShortState(WorkspaceStore(tmp_path / "workspace"))
    first = await state.execute(json.dumps({"slot": 1, "text": "remember this"}))
    assert first.ok and first.mutation_committed
    assert first.data == {"slot": 1, "text": "remember this"}
    await state.execute(json.dumps({"slot": 2, "text": "another conversation"}))
    updated = await state.execute(json.dumps({"slot": 1, "text": "next step"}))
    assert updated.ok and updated.mutation_committed
    assert updated.data == {"slot": 1, "text": "next step"}
    cleared = await state.execute(json.dumps({"slot": 1, "text": ""}))
    assert cleared.ok and cleared.mutation_committed
    assert cleared.data == {"slot": 1, "text": ""}
    assert project_short_state(state.snapshot()) == [{"slot": 2, "text": "another conversation"}]


def test_short_state_expiry_changes_the_read_value_without_renewal(tmp_path, monkeypatch) -> None:
    clock = 1_000_000
    monkeypatch.setattr("qq_ai_bot.workspace.short_state.time.time", lambda: clock)
    state = ShortState(WorkspaceStore(tmp_path / "workspace"))
    state.update({"slot": 1, "text": "temporary"})
    before = state.snapshot()[0]
    clock += 86_401
    expired = state.snapshot()[0]
    assert expired["text"] == ""
    assert expired["revision"] == before["revision"]
    assert expired["expires_at"] == before["expires_at"]
    assert project_short_state([expired]) == []
    with state.store._transaction(write=False) as reader:
        assert reader.execute("SELECT text FROM short_state WHERE slot=1").fetchone()[0] == (
            "temporary"
        )


def test_registered_long_plugin_fragment_stays_structured_and_complete() -> None:
    extensions = ExtensionRegistry()
    registrar = extensions.registrar(
        "com.example.context", (PluginPermission.PROMPT_CONTEXT_REGISTER,)
    )
    content = "真实插件资料" * 4_000
    registrar.register_prompt_fragment(
        PromptFragment(
            id="context",
            stage=PromptStage.PLUGIN_CONTEXT,
            content=content,
            trusted_level=TrustedLevel.CORE,
        )
    )
    (registered,) = extensions.list(kind=ExtensionKind.PROMPT_FRAGMENT)
    assert isinstance(registered.registration, PromptFragment)
    assert registered.registration.trusted_level is TrustedLevel.PLUGIN_UNTRUSTED
    registry = PromptRegistry()
    PluginPromptAdapter(extensions, registry).activate("com.example.context")
    fragments = registry.render()
    assert fragments == (
        {
            "plugin_id": "com.example.context",
            "fragment_id": "plugin.com.example.context.context",
            "content": content,
        },
    )
    compiled = PromptCompiler().compile(
        PromptProgram(
            contributions=(
                PromptContribution(
                    id="context.plugins",
                    channel=PromptChannel.PLUGIN,
                    trust=PromptTrust.UNTRUSTED,
                    payload=list(fragments),
                ),
            )
        ),
        current_message=ChatMessage("user", "读取这些资料"),
    )
    request = ChatRequest(model="primary", messages=compiled.messages)
    assert content in (compiled.messages[-1].content or "")
    assert estimate_request_tokens(request) < ModelCapacity(input_tokens=96_000).input_budget(
        96_000
    )


def test_prompt_registration_still_checks_actual_permission() -> None:
    registrar = ExtensionRegistry().registrar("com.example.context", ())
    with pytest.raises(PluginPermissionError):
        registrar.register_prompt_fragment(
            PromptFragment(id="context", stage=PromptStage.PLUGIN_CONTEXT, content="资料")
        )
