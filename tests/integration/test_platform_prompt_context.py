"""Actual native Main requests retain a shared static prefix and current entry facts."""

from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from tests.conftest import make_settings
from tests.integration import test_telegram_runtime as native
from tests.unit.test_external_event_prompt_isolation import _assembler, _external, _snapshot

from yuki.conversation.scope import ConversationTurnSnapshot
from yuki.domain.conversations import ConversationScope
from yuki.runtime.trigger import ExternalEventTurnTrigger
from yuki.services.prompt_composer import PromptComposer

telegram_env = native.telegram_env


async def test_telegram_main_has_neutral_static_prompt_and_current_platform(telegram_env):
    env = telegram_env
    await env.gateway.receive(native.update())
    await native.drain(env)
    request = env.model.requests[0]
    static = "\n".join(
        message.content or "" for message in request.messages if message.role == "system"
    )
    assert "运行在 QQ 中" not in static
    assert "你是 AI 助手 Yuki" in static
    assert any('"platform":"telegram"' in (message.content or "") for message in request.messages)
    frozen = deepcopy(request.messages)
    await env.gateway.receive(native.update(2, text="another turn"))
    await native.drain(env)
    assert request.messages == frozen


def test_custom_persona_and_system_prompt_remain_exact(tmp_path):
    persona = tmp_path / "persona.md"
    persona.write_text("私人人格", encoding="utf-8")
    settings = make_settings(
        "sqlite+aiosqlite://", bot_persona_file=persona, system_prompt="我的固定指令"
    )
    assert settings.system_prompt == "我的固定指令" and settings.bot_persona == "私人人格"
    assert any(
        "我的固定指令" in message.content for message in PromptComposer(settings).static_messages()
    )


@pytest.mark.parametrize("platform", ["qq", "telegram", "app"])
@pytest.mark.parametrize("entry", ["activation", "external", "plugin"])
async def test_shared_entrypoints_put_platform_only_in_current_context(platform, entry):
    identity = ConversationScope.private(
        "8000", "1001", platform=platform, conversation_id="conv-platform"
    )
    turn = ConversationTurnSnapshot(
        "conv-platform",
        identity.key,
        1,
        3 if entry == "external" else None,
        1,
        transport_scope_key=identity.key,
        initiative_run_id="current-initiative" if entry != "external" else None,
    )
    assembler = _assembler()
    if entry == "activation":
        turn = replace(turn, scope_key="fixed-canonical-primary-alias")
    event = replace(
        _external(3, "original external turn"),
        canonical_conversation_id="conv-platform",
        platform="",
    )
    snapshot = _snapshot(
        event,
        checkpoint_text="",
        coverage_end=0,
        revision=1,
        checkpoint_kind=None,
    )
    snapshot = replace(snapshot, read_version=replace(snapshot.read_version, scope=identity))
    assembler._load_history_snapshot = AsyncMock(return_value=snapshot)
    runtime = MagicMock()
    runtime.context.local_event_limit = 2_048
    if entry == "plugin":
        assembler._ledger.read_scope_context = AsyncMock(return_value=(None, ()))
        metadata = {"plugin": "fixture", "platform": "untrusted-placeholder"}
        context = await assembler.assemble_plugin(
            identity=identity,
            content="current plugin turn",
            metadata=metadata,
            read_history=False,
            runtime=runtime,
        )
        assert metadata["platform"] == "untrusted-placeholder"
    else:
        kwargs = {}
        if entry == "external":
            kwargs = {
                "external_event": event,
                "external_trigger": ExternalEventTurnTrigger(
                    plugin_id="github-monitor",
                    source_event_id=event.id,
                    target_type="private",
                    target_id="1001",
                    agent_intent="react",
                ),
            }
        context = await assembler.assemble(
            inbound=None,
            profile=None,
            identity=identity,
            turn=turn,
            content="current execution",
            runtime=runtime,
            **kwargs,
        )
    assert context.metadata_payload["platform"] == platform
    assert context.history_messages == ()
    if entry != "plugin":
        assert context.prompt_scope_key == turn.scope_key
