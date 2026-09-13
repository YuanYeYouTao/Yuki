"""Read-only core tool bridge for invocation-bound plugin Main Agent runs."""

from __future__ import annotations

import json
from typing import Any, cast

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.messages import (
    ChatTool,
    InboundMessage,
    ToolCall,
)
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.services.agent_tools import AgentToolService, OneBotToolGateway, ToolRuntime
from yuki_plugin_sdk.errors import PluginPermissionError


class PluginAgentToolBackend:
    """Expose a runtime-approved subset of existing read-only Agent tools.

    Plugin code never receives the core service or a transport object.  The Host
    binds the real inbound message to a fresh backend per invocation and validates
    every requested scope before delegating. The shared service keeps no actor.
    """

    def __init__(self, service: AgentToolService, *, inbound: InboundMessage | None = None) -> None:
        self._service = service
        self._inbound = inbound

    def bind_source(self, inbound: InboundMessage) -> PluginAgentToolBackend:
        """Return an independent execution view without mutating the shared backend."""
        return PluginAgentToolBackend(self._service, inbound=inbound)

    def definitions(
        self,
        runtime: AgentRuntime,
        *,
        web_was_used: bool,
    ) -> tuple[ChatTool, ...]:
        del web_was_used
        tool_runtime = self._tool_runtime(runtime)
        return tuple(
            tool
            for tool in self._service.definitions(tool_runtime)
            if tool.name in runtime.allowed_capabilities
        )

    def begin_batch(self, calls: tuple[ToolCall, ...], runtime: AgentRuntime) -> None:
        del calls, runtime

    def did_use_web(self) -> bool:
        """Plugin sessions cannot invoke the host web tools after web isolation."""

        return False

    def parallel_safe(self, name: str, runtime: AgentRuntime) -> bool:
        """Plugin sessions expose read-only core tools, so calls may overlap."""

        del runtime
        return name != "update_short_state"

    def is_side_effecting(
        self,
        name: str,
        arguments_json: str,
        runtime: AgentRuntime,
    ) -> bool:
        del arguments_json, runtime
        return name == "update_short_state"

    async def execute(
        self,
        name: str,
        arguments_json: str,
        runtime: AgentRuntime,
    ) -> str:
        if runtime.before_model_request is not None:
            await runtime.before_model_request()
        if name == "update_short_state" and self._service.short_state is not None:
            return cast(str, await self._service.short_state.execute(arguments_json))
        if name not in runtime.allowed_capabilities:
            return _error("capability_not_allowed", "插件 Agent 未获准使用该能力")
        scoped_arguments, scope_error = self._scope_arguments(
            name,
            arguments_json,
            runtime,
        )
        if scope_error is not None:
            return scope_error
        return await self._service.execute(
            name,
            scoped_arguments,
            self._tool_runtime(runtime),
        )

    def finalize(self, content: str, runtime: AgentRuntime) -> str:
        del runtime
        return content

    def exhausted(self, runtime: AgentRuntime) -> str:
        del runtime
        return "插件 Agent 已达到本轮工具或模型请求上限。"

    def post_commit_recovery_text(self) -> str | None:
        return None

    def _tool_runtime(self, runtime: AgentRuntime) -> ToolRuntime:
        inbound = self._inbound
        if (
            inbound is None
            or not inbound.conversation_id
            or not inbound.presence_id
            or inbound.conversation_id != runtime.canonical_conversation_id
            or inbound.sender.user_id != runtime.actor_user_id
            or inbound.bot_user_id != runtime.bot_user_id
            or inbound.group_id != runtime.current_group_id
        ):
            raise PluginPermissionError("plugin tool source does not match the Main Agent runtime")
        return ToolRuntime(
            inbound=inbound,
            gateway=cast(OneBotToolGateway | None, runtime.gateway),
            allow_generic_onebot=False,
            allow_admin_actions=False,
            allow_automation=False,
            conversation_key=runtime.conversation_key,
            execution_id=runtime.execution_id or "",
            trigger_message_id=inbound.message_id,
            trigger_event_id=(
                runtime.work_control.source.get("trigger_event_id")
                if runtime.work_control is not None
                else None
            ),
            actor_user_id=runtime.actor_user_id,
            actor_is_superuser=runtime.actor_is_superuser,
            current_group_id=runtime.current_group_id,
            runtime_config=runtime.runtime_config,
            origin=TurnOrigin.PLUGIN_SESSION,
            read_only=True,
            conversation_id=inbound.conversation_id,
            presence_id=inbound.presence_id,
            person_id=inbound.person_id,
            space_id=inbound.space_id,
            bot_user_id=inbound.bot_user_id,
            scope_type=inbound.scope_type,
            before_model_request=runtime.before_model_request,
        )

    @staticmethod
    def _scope_arguments(
        name: str,
        arguments_json: str,
        runtime: AgentRuntime,
    ) -> tuple[str, str | None]:
        try:
            arguments = json.loads(arguments_json)
        except json.JSONDecodeError:
            return arguments_json, None
        if not isinstance(arguments, dict):
            return arguments_json, None
        if runtime.actor_is_superuser:
            return arguments_json, None
        if name == "get_person_memories":
            if _text(arguments.get("user_id")) != runtime.actor_user_id:
                return arguments_json, _error(
                    "scope_denied",
                    "普通用户只能读取自己的个人记忆",
                )
        elif name == "get_group_memories":
            if not runtime.current_group_id:
                return arguments_json, _error(
                    "scope_denied",
                    "私聊中的插件 Agent 没有当前群作用域",
                )
            if _text(arguments.get("group_id")) != runtime.current_group_id:
                return arguments_json, _error(
                    "scope_denied",
                    "普通用户只能读取当前群的共同记忆",
                )
        elif name == "search_chat_history":
            requested_group = _text(arguments.get("group_id"))
            requested_user = _text(arguments.get("user_id"))
            if runtime.current_group_id:
                if requested_group and requested_group != runtime.current_group_id:
                    return arguments_json, _error(
                        "scope_denied",
                        "普通用户只能搜索当前群历史",
                    )
                arguments["group_id"] = runtime.current_group_id
            elif requested_group or (requested_user and requested_user != runtime.actor_user_id):
                return arguments_json, _error(
                    "scope_denied",
                    "普通用户只能搜索自己的当前私聊历史",
                )
            else:
                arguments["user_id"] = runtime.actor_user_id
            return json.dumps(arguments, ensure_ascii=False), None
        return arguments_json, None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _error(code: str, detail: str) -> str:
    return json.dumps({"ok": False, "error": code, "detail": detail}, ensure_ascii=False)


__all__ = ["PluginAgentToolBackend"]
