"""Capture the effective Main Agent task scope at the actual sandbox adapter."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from qq_ai_bot.automation.authority import DelegatedAuthority, PermissionLevel
from qq_ai_bot.automation.handlers import _AutomationAgentBackend
from qq_ai_bot.social.automation import SocialAutomationAdapter


async def automation_task_source_cases(handlers, context, provider):
    allowed = frozenset({"sandbox.run_python", "social.send_private_message"})
    grant = DelegatedAuthority(
        creator_user_id=context.creator_user_id,
        bot_user_id=context.bot_user_id,
        created_from_message_id="source",
        created_at="2026-09-11T00:00:00Z",
        permission_level=PermissionLevel.USER,
        granted_capabilities=tuple(sorted(allowed)),
        capability_schema_versions={name: 1 for name in allowed},
    )
    original = replace(
        context,
        automation_script_hash="a" * 64,
        source_step_id="original_step",
        conversation_generation=7,
        authority=context.authority.model_copy(
            update={
                "allowed_capabilities": allowed,
                "delegated_authority": grant,
            }
        ),
    )
    captured = []

    def backend(registry, scoped):
        captured.append(scoped)
        return _AutomationAgentBackend(registry, scoped)

    responder = provider._responder
    provider._responder = lambda request: "done"
    try:
        with patch("qq_ai_bot.automation.handlers._AutomationAgentBackend", side_effect=backend):
            await handlers.agent(
                {
                    "instruction": "下载鲸鱼图片并保存产物",
                    "context_profile": "none",
                    "allowed_capabilities": ["sandbox.run_python"],
                    "max_tool_calls": 2,
                    "max_model_requests": 2,
                },
                original,
            )
    finally:
        provider._responder = responder
    assert len(captured) == 1
    scoped = captured[0]
    assert original.agent_instruction is None
    assert original.authority.allowed_capabilities == allowed
    assert scoped.authority.allowed_capabilities == frozenset({"sandbox.run_python"})
    sandbox = SimpleNamespace(execute=AsyncMock(return_value={"pending": True}))
    adapter = SocialAutomationAdapter(None, None, sandbox)
    await adapter.mapping()["sandbox.run_python"](
        {"code": "print('download')"}, replace(scoped, step_id="agent:unique-call")
    )
    source = sandbox.execute.call_args.kwargs["source"]
    assert source["source_step_id"] == "original_step"
    assert source["trigger_id"] == "agent:unique-call"
    assert source["script_hash"] == "a" * 64
    assert source["generation"] == 7
    assert source["instruction"] == "下载鲸鱼图片并保存产物"
    assert source["allowed_capabilities"] == ["sandbox.run_python"]
    assert source["context_profile"] == "none"
    assert source["automation_context"] == original.automation_context.model_dump(mode="json")
    assert source["delegated_authority"] == grant.model_dump(mode="json")
