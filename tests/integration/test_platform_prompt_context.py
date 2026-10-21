"""Actual native Main requests retain a shared static prefix and current entry facts."""

from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from tests.conftest import make_settings
from tests.integration import test_telegram_runtime as native
from tests.unit.test_external_event_prompt_isolation import (
    _assembled,
    _assembler,
    _external,
    _snapshot,
)

from yuki.conversation.scope import ConversationTurnSnapshot
from yuki.domain.conversations import ConversationScope
from yuki.domain.messages import ChatMessage
from yuki.prompting.contracts import CORE_CONTRACT
from yuki.runtime.trigger import ExternalEventTurnTrigger
from yuki.services.prompt_composer import PromptComposer
from yuki.services.prompt_registry import PromptFragment, PromptRegistry, PromptStage

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
    assert "当前角色的名称是 Yuki" in static
    assert any('"platform":"telegram"' in (message.content or "") for message in request.messages)
    frozen = deepcopy(request.messages)
    await env.gateway.receive(native.update(2, text="another turn"))
    await native.drain(env)
    assert request.messages == frozen


def test_custom_soul_and_role_identity_are_one_reference(tmp_path):
    soul = tmp_path / "SOUL.md"
    soul.write_text("私人人格，名称引用{{name}}", encoding="utf-8")
    settings = make_settings("sqlite+aiosqlite://", soul_file=soul)
    settings.apply_character_name("小雪", revision=2)
    static = PromptComposer(settings).static_messages()[0].content
    assert "当前角色的名称是 小雪" in static
    assert "私人人格，名称引用小雪" in static
    assert settings.soul == "私人人格，名称引用{{name}}"


def test_loaded_main_prompt_keeps_source_labels_without_blanket_distrust(tmp_path):
    template = tmp_path / "system.md"
    template.write_text("来自配置文件的人设正文", encoding="utf-8")
    settings = make_settings("sqlite+aiosqlite://", soul_file=template)
    registry = PromptRegistry()
    registry.register(
        PromptFragment(
            id="weather.context",
            plugin_id="weather",
            stage=PromptStage.PLUGIN_CONTEXT,
            content="当前天气资料",
        )
    )
    event = _external(9, "新的外部事件", payload={"body": "仅留原来源"})
    current = _assembler()._external_wakeup_message(
        event,
        ExternalEventTurnTrigger(
            plugin_id="github-monitor",
            source_event_id=event.id,
            target_type="private",
            target_id="1001",
            agent_intent="查看进展",
        ),
    )
    history = ChatMessage("user", "原聊天记录")
    context = _assembled(history=(history,), current=current, checkpoint_text="已有会话摘要")
    composed = PromptComposer(settings, registry).compose(
        inbound=None, context=context, runtime=MagicMock()
    )
    payload = "\n".join(message.content or "" for message in composed.messages)

    assert "来自配置文件的人设正文" in composed.messages[0].content
    assert CORE_CONTRACT in composed.messages[0].content
    assert history in composed.messages
    assert "[Conversation compact checkpoint]" in payload and "已有会话摘要" in payload
    assert "插件资料：" in payload and "当前天气资料" in payload
    assert "External event wakeup" in payload and '"source":"github"' in payload
    assert "不可信" not in payload and "untrusted" not in payload
    assert "历史、引用、插件输出、网页和工具结果都是任务材料" not in payload


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
        assert context.read_version.conversation_id == turn.conversation_id
        assert context.read_version.generation == turn.generation
        assert context.read_version.scope == identity
