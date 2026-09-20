"""Scheduled tools use the common backend and retain real execution anchors."""

from dataclasses import replace
from unittest.mock import patch

from qq_ai_bot.services.main_agent_backend import MainAgentBackend


async def automation_task_source_cases(handlers, context, provider):
    original = replace(
        context,
        automation_script_hash="a" * 64,
        source_step_id="original_step",
        conversation_generation=7,
    )
    captured = []

    def backend(chat, runtime):
        captured.append(runtime)
        return MainAgentBackend(chat, runtime)

    responder = provider._responder
    provider._responder = lambda request: "done"
    try:
        with patch("qq_ai_bot.services.main_agent_backend.MainAgentBackend", side_effect=backend):
            await handlers.agent(
                {
                    "instruction": "下载鲸鱼图片并保存产物",
                    "context_profile": "none",
                    "max_tool_calls": 2,
                    "max_model_requests": 2,
                },
                original,
            )
    finally:
        provider._responder = responder
    assert len(captured) == 1
    runtime = captured[0]
    assert runtime.inbound is None
    assert runtime.actor_context.event_id is None
    assert runtime.require_actor().user_id == context.creator_user_id
    assert runtime.allow_work_environment and runtime.allow_automation
    source = runtime.sandbox_source
    assert source["source_step_id"] == "original_step"
    assert source["script_hash"] == "a" * 64
    assert source["generation"] == 7
    assert source["automation_run_id"] == context.automation_run_id
    assert "allowed_capabilities" not in source
    assert "delegated_authority" not in source
