"""Invocation-bound SDK generation through the shared Yuki compiler and runner."""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import replace
from typing import TYPE_CHECKING, cast

from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.llm.base import LLMInvalidRequestError
from qq_ai_bot.services.agent_runner import AgentRunResult, AgentRuntime, AgentToolBackend
from qq_ai_bot.services.context_assembler import AssembledContext, ContextMetrics
from qq_ai_bot.services.main_agent_turns import MainAgentTurnService
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.permissions import PluginPermission

if TYPE_CHECKING:
    from qq_ai_bot.plugin_host.facades import HostPluginContext, PluginInvocation

_ACTIVE: ContextVar[bool] = ContextVar("plugin_main_generation_active", default=False)


async def run_plugin_main_turn(
    host: HostPluginContext,
    invocation: PluginInvocation,
    *,
    instruction: str,
    context_data: str,
    runtime: AgentRuntime,
    tools: AgentToolBackend | None,
    permission: PluginPermission,
) -> AgentRunResult:
    """Keep SDK reads/effects narrow; never synthesize a user or transport target."""
    if _ACTIVE.get():
        raise PluginPermissionError("recursive plugin Main Agent generation is not allowed")
    runner = host._services.agent_runner
    contract = runner.main_contract if runner is not None else None
    ledger = host._services.ledger
    inbound = invocation.inbound
    if contract is None or ledger is None:
        raise PluginPermissionError("Yuki Main Agent services are unavailable")
    if inbound is None or not invocation.conversation_id or not invocation.presence_id:
        raise PluginPermissionError(
            "Yuki generation requires a real Host-bound inbound Conversation and Presence; "
            "background callers must use the target-bound Main Agent wakeup API"
        )
    version, _ = await ledger.read_scope_context(inbound.scope(), limit=0)
    event = await ledger.find_by_platform_message(
        bot_user_id=invocation.bot_user_id, platform_message_id=inbound.message_id
    )
    if (
        version.conversation_id != invocation.conversation_id
        or event is None
        or event.canonical_conversation_id != version.conversation_id
        or event.direction != "inbound"
        or event.event_kind != "message"
        or event.sender_user_id != invocation.actor_user_id
        or event.author_person_id != invocation.person_id
        or event.ingress_presence_id != invocation.presence_id
        or (invocation.source_event_id is not None and event.id != invocation.source_event_id)
        or event.id <= version.starts_after_event_id
    ):
        raise PluginPermissionError(
            "Yuki generation source does not match the current Conversation"
        )

    async def validate() -> None:
        if host._require(permission) is not invocation:
            raise LLMInvalidRequestError("plugin invocation changed before model request")
        if not await ledger.read_version_matches(version):
            raise LLMInvalidRequestError("plugin Conversation changed before model request")
        if runtime.before_model_request is not None:
            await runtime.before_model_request()

    payload = {"plugin": {"id": host.plugin_id, "source_event_id": event.id}}
    if context_data:
        payload["plugin"]["requested_context"] = context_data
    content = json.dumps(
        {
            "origin": "plugin_request",
            "content_trust": "untrusted_plugin_input",
            "instruction": instruction,
        },
        ensure_ascii=False,
    )
    context = AssembledContext(
        metadata_payload=payload,
        history_messages=(),
        current_message=ChatMessage(role="user", content=content),
        recent_delivery=(),
        current_time=runtime.current_time,
        current_relationship=None,
        metrics=ContextMetrics(
            metadata_characters=len(json.dumps(payload, ensure_ascii=False)),
            history_characters=0,
            history_messages=0,
            current_message_characters=len(content),
            raw_history_window_shifted=False,
        ),
        read_version=version,
    )
    main = cast(MainAgentTurnService, contract.chat._main_turns)
    marker = _ACTIVE.set(True)
    try:
        async with contract.chat._turn_coordinator.hold(invocation.conversation_key):
            await validate()
            composition = await main.compose(
                inbound=None,
                context=context,
                runtime=runtime.runtime_config,
                visual_observation=None,
                visual_failure=False,
                scope_type=inbound.scope_type,
                include_plugin_context=False,
            )
            return await main.run(
                composition.messages,
                replace(
                    runtime,
                    conversation_key=invocation.conversation_key,
                    before_model_request=validate,
                ),
                tools,
            )
    finally:
        _ACTIVE.reset(marker)
