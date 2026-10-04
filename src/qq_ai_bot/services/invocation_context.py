"""Host-only projection of an already bound tool source into an Agent invocation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from qq_ai_bot.services.agent_runner import AgentRuntime

if TYPE_CHECKING:
    from qq_ai_bot.services.agent_tools import ToolRuntime
    from qq_ai_bot.time.models import TimeContext


class InvocationContextFactory:
    """Reuse the entrypoint's trusted identity; never look up a replacement source."""

    @staticmethod
    def from_tools(
        source: ToolRuntime,
        *,
        current_time: TimeContext,
        allowed_capabilities: frozenset[str],
        max_tool_calls: int,
        max_model_requests: int,
    ) -> AgentRuntime:
        config = source.runtime_config
        bot = source.effective_bot_user_id
        if config is None or not bot:
            raise ValueError("incomplete_host_invocation_context")
        return AgentRuntime(
            origin=source.origin,
            actor_user_id=source.actor_user_id,
            actor_is_superuser=source.actor_is_superuser,
            delegated_authority=None,
            conversation_key=source.conversation_key,
            current_group_id=source.current_group_id,
            bot_user_id=bot,
            gateway=source.gateway,
            runtime_config=config,
            current_time=current_time,
            allowed_capabilities=allowed_capabilities,
            max_tool_calls=max_tool_calls,
            max_model_requests=max_model_requests,
            prompt_diagnostics=source.prompt_diagnostics,
            before_model_request=source.before_model_request,
            observation_boundary=source.observation_boundary,
            canonical_conversation_id=source.effective_conversation_id,
            execution_id=source.effective_execution_id,
            source_event_id=source.effective_trigger_event_id,
            visible_event_ids=source.visible_event_ids,
            preparation_model_requests=source.prompt_diagnostics.preparation_model_requests
            if source.prompt_diagnostics is not None
            else 0,
        )
