"""Invocation-bound SDK generation through the shared Yuki compiler and runner."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextvars import ContextVar
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from qq_ai_bot.llm.base import LLMInvalidRequestError
from qq_ai_bot.runtime.activation_outcome import ContextBoundaryChanged
from qq_ai_bot.services.agent_runner import AgentRunResult, AgentRuntime, AgentToolBackend
from qq_ai_bot.services.agent_tools import OneBotToolGateway, ToolRuntime
from qq_ai_bot.services.main_agent_backend import MainAgentBackend
from qq_ai_bot.services.main_agent_turns import MainAgentTurnService
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.permissions import PluginPermission

if TYPE_CHECKING:
    from qq_ai_bot.plugin_host.facades import HostPluginContext, PluginInvocation

_ACTIVE: ContextVar[bool] = ContextVar("plugin_main_generation_active", default=False)
_RUNNING: dict[str, asyncio.Task[AgentRunResult]] = {}
CALLBACK_WAIT_SECONDS = 5.0


async def close_plugin_main_tasks(plugin_id: str) -> None:
    tasks = [
        task for task in tuple(_RUNNING.values()) if task.get_name() == f"plugin-main-{plugin_id}"
    ]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def run_plugin_main_turn(
    host: HostPluginContext,
    invocation: PluginInvocation,
    *,
    instruction: str,
    context_data: str,
    runtime: AgentRuntime,
    tools: AgentToolBackend | None,
    permission: PluginPermission,
    context_profile: str = "none",
) -> AgentRunResult:
    """Bound the callback wait while the Host retains an accepted activation."""
    from qq_ai_bot.runtime.work_activation import current_work_control
    from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
    from qq_ai_bot.services.main_agent_turns import invocation_boundary

    if _ACTIVE.get() or current_work_control.get() is not None:
        raise PluginPermissionError("recursive Yuki Main Agent generation is not allowed")
    execution_id = (
        f"plugin:{host.plugin_id}:{invocation.source_event_id}:"
        + hashlib.sha256((permission.value + instruction + context_data).encode()).hexdigest()
    )
    runtime = replace(runtime, execution_id=execution_id)
    key = invocation_boundary(runtime)
    ledger = host._services.ledger
    if ledger is not None:
        previous = await WorkRepository(ledger._database).by_source(f"invocation:{key}")
        if previous is not None:
            prior_source = json.loads(previous["source_json"])
            if prior_source.get("owner") == "plugin_invocation" and (
                prior_source.get("approval_revision") != host._services.approval_revision
                or prior_source.get("plugin_id") != host.plugin_id
            ):
                raise WorkConflict("plugin_work_authority_changed")
    task = _RUNNING.get(key)
    if task is not None and task.done():
        # A completed task may still be present before its done callback runs.
        # Reusing it would skip the durable work and authority checks below.
        _RUNNING.pop(key, None)
        task = None
    if task is None:
        if len(_RUNNING) >= 8:
            raise PluginPermissionError("plugin main Agent admission is busy; no work accepted")
        task = asyncio.create_task(
            _execute_plugin_main_turn(
                host,
                invocation,
                instruction=instruction,
                context_data=context_data,
                runtime=runtime,
                tools=tools,
                permission=permission,
                context_profile=context_profile,
            ),
            name=f"plugin-main-{host.plugin_id}",
        )
        _RUNNING[key] = task

        def finished(completed: asyncio.Task[AgentRunResult]) -> None:
            if _RUNNING.get(key) is completed:
                _RUNNING.pop(key, None)
            if not completed.cancelled():
                completed.exception()  # Durable work state is the recovery authority.

        task.add_done_callback(finished)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=CALLBACK_WAIT_SECONDS)
    except TimeoutError:
        ledger = host._services.ledger
        if ledger is not None:
            row = await WorkRepository(ledger._database).by_source(f"invocation:{key}")
            if row is not None:
                return AgentRunResult(
                    text="",
                    tool_calls_used=0,
                    model_requests=0,
                    web_was_used=False,
                    work_id=row["id"],
                    work_state=row["state"],
                    suppress_delivery=True,
                )
        # No durable owner exists yet. Stop preparation instead of leaving an
        # unqueryable activation behind a timed-out plugin callback.
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if ledger is not None:
            row = await WorkRepository(ledger._database).by_source(f"invocation:{key}")
            if row is not None:  # Admission raced with cancellation; keep its original ID.
                return AgentRunResult(
                    text="",
                    tool_calls_used=0,
                    model_requests=0,
                    web_was_used=False,
                    work_id=row["id"],
                    work_state=row["state"],
                    suppress_delivery=True,
                )
        raise PluginPermissionError(
            "plugin main Agent preparation timed out; no work accepted"
        ) from None
    except asyncio.CancelledError:
        ledger = host._services.ledger
        row = (
            await WorkRepository(ledger._database).by_source(f"invocation:{key}")
            if ledger is not None
            else None
        )
        if row is None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # Accepted work belongs to the Host; callback cancellation is not a user cancel.
        raise


async def _execute_plugin_main_turn(
    host: HostPluginContext,
    invocation: PluginInvocation,
    *,
    instruction: str,
    context_data: str,
    runtime: AgentRuntime,
    tools: AgentToolBackend | None,
    permission: PluginPermission,
    context_profile: str = "none",
) -> AgentRunResult:
    """Keep SDK reads/effects narrow; never synthesize a user or transport target."""
    if _ACTIVE.get():
        raise PluginPermissionError("recursive plugin Main Agent generation is not allowed")
    from qq_ai_bot.runtime.work_activation import current_work_control

    if current_work_control.get() is not None:
        raise PluginPermissionError(
            "recursive Yuki Main Agent generation is not allowed; use delegation"
        )
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
    can_read_history = PluginPermission.MESSAGE_HISTORY_READ in host._approved_permissions
    version, _ = await ledger.read_scope_context(inbound.scope(), limit=0)
    if invocation.source_event_id is None:
        raise PluginPermissionError("Yuki generation requires a ledger event anchor")
    event = await ledger.get_event(invocation.source_event_id)
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
        from qq_ai_bot.identity.canonical_repository import active_person_id_for
        from qq_ai_bot.identity.db_models import CanonicalPersonModel, PresenceModel

        async with ledger._database.sessions() as session:
            person = await session.get(CanonicalPersonModel, invocation.person_id)
            presence = await session.get(PresenceModel, invocation.presence_id)
            active_person = await active_person_id_for(session, invocation.actor_user_id)
        if (
            person is None
            or not person.enabled
            or active_person != invocation.person_id
            or presence is None
            or not presence.enabled
            or presence.external_account_id != invocation.bot_user_id
        ):
            raise PluginPermissionError("plugin source identity is no longer active")
        if not await ledger.read_version_matches(version):
            raise ContextBoundaryChanged("plugin Conversation changed before model request")
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
    context = await contract.chat._context_assembler.assemble_plugin(
        inbound=inbound,
        content=content,
        metadata=payload,
        current_time=runtime.current_time,
        read_history=can_read_history,
        projection_scope=json.dumps(
            [
                "plugin-sdk",
                host.plugin_id,
                invocation.actor_user_id,
                permission.value,
                context_profile,
                sorted(runtime.allowed_capabilities),
            ],
            separators=(",", ":"),
        ),
    )
    if context.read_version != version:
        raise ContextBoundaryChanged("plugin source changed during context preparation")
    main = cast(MainAgentTurnService, contract.chat._main_turns)
    execution_id = (
        f"plugin:{host.plugin_id}:{invocation.source_event_id}:"
        + hashlib.sha256((permission.value + instruction + context_data).encode()).hexdigest()
    )
    tools = MainAgentBackend(
        contract.chat,
        ToolRuntime(
            inbound=inbound,
            gateway=cast(OneBotToolGateway | None, runtime.gateway),
            allow_generic_onebot=False,
            allow_admin_actions=False,
            allow_automation=False,
            conversation_key=invocation.conversation_key,
            execution_id=execution_id,
            allow_work_environment=True,
            read_scope=inbound.scope(),
            read_target_id=inbound.space_id or inbound.person_id,
            trigger_event_id=invocation.source_event_id,
            actor_user_id=runtime.actor_user_id,
            actor_is_superuser=runtime.actor_is_superuser,
            current_group_id=runtime.current_group_id,
            runtime_config=runtime.runtime_config,
            origin=runtime.origin,
            conversation_id=inbound.conversation_id,
            presence_id=inbound.presence_id,
            person_id=inbound.person_id,
            space_id=inbound.space_id,
            bot_user_id=inbound.bot_user_id,
            scope_type=inbound.scope_type,
            before_model_request=validate,
        ),
        allowed_tools=runtime.allowed_capabilities
        | {"update_short_state", "request_tools", "read_tool_artifact"},
    )
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

            async def validate_and_commit() -> None:
                await validate()
                if composition.commit_projection is not None:
                    await composition.commit_projection()

            result = await main.run(
                composition.messages,
                replace(
                    runtime,
                    conversation_key=invocation.conversation_key,
                    execution_id=execution_id,
                    invocation_goal=instruction,
                    invocation_source={
                        "owner": "plugin_invocation",
                        "plugin_id": host.plugin_id,
                        "approval_revision": host._services.approval_revision,
                        "trigger_event_id": invocation.source_event_id,
                        "instruction": instruction,
                        "context_data": context_data,
                        "context_profile": context_profile,
                        "conversation_key": invocation.conversation_key,
                        "permission": permission.value,
                        "allowed_tools": sorted(runtime.allowed_capabilities),
                        "max_model_requests": runtime.max_model_requests,
                        "max_tool_calls": runtime.max_tool_calls,
                        "person_id": inbound.person_id,
                        "space_id": inbound.space_id,
                        "presence_id": inbound.presence_id,
                        "bot_user_id": inbound.bot_user_id,
                        "actor_is_superuser": runtime.actor_is_superuser,
                    },
                    before_model_request=validate_and_commit,
                ),
                tools,
            )
            return result
    finally:
        _ACTIVE.reset(marker)


async def resume_plugin_work(app: object, work: dict[str, Any], source: dict[str, Any]) -> None:
    """Host-owned continuation after the original SDK callback has returned."""
    from dataclasses import replace
    from typing import Any

    from qq_ai_bot.automation.models import TurnOrigin
    from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
    from qq_ai_bot.plugin_host.facades import PluginInvocation, _agent_dependencies

    container = cast(Any, app)
    host = container._plugin_contexts.get(source.get("plugin_id"))
    if host is None:
        raise PluginPermissionError("plugin work owner is no longer enabled")
    if source.get("approval_revision") != host._services.approval_revision:
        raise PluginPermissionError("plugin approved version changed")
    permission = PluginPermission(source["permission"])
    if permission not in host._approved_permissions:
        raise PluginPermissionError("plugin work permission was revoked")
    allowed = frozenset(source["allowed_tools"])
    if not allowed <= host._services.agent_capabilities:
        raise PluginPermissionError("plugin work capabilities changed")
    event = await container.ledger.get_event(source["trigger_event_id"])
    if event is None or event.canonical_conversation_id != work["conversation_id"]:
        raise PluginPermissionError("plugin work source is unavailable")
    inbound = InboundMessage(
        message_id=event.platform_message_id,
        source_event_id=event.id,
        event_type="message",
        scope_type=event.scope_type,
        sender=SenderIdentity(source["actor_user_id"]),
        text=event.content,
        bot_user_id=source["bot_user_id"],
        group_id=event.group_id,
        received_at=event.occurred_at,
        person_id=source.get("person_id"),
        space_id=source.get("space_id"),
        presence_id=source.get("presence_id"),
        conversation_id=work["conversation_id"],
        legacy_conversation_key=source["conversation_key"],
    )
    invocation = PluginInvocation(
        plugin_id=source["plugin_id"],
        origin=TurnOrigin.PLUGIN_SESSION,
        actor_user_id=source["actor_user_id"],
        bot_user_id=source["bot_user_id"],
        inbound=inbound,
        source_event_id=event.id,
        person_id=inbound.person_id,
        space_id=inbound.space_id,
        presence_id=inbound.presence_id,
        conversation_id=inbound.conversation_id,
    )
    async with host.bind(invocation):
        _, runtime = await _agent_dependencies(host, invocation)
        if runtime.actor_is_superuser != source.get("actor_is_superuser", False):
            raise PluginPermissionError("plugin work actor authority changed")
        await run_plugin_main_turn(
            host,
            invocation,
            instruction=source["instruction"],
            context_data=source["context_data"],
            context_profile=source["context_profile"],
            permission=permission,
            runtime=replace(
                runtime,
                allowed_capabilities=allowed,
                max_model_requests=min(runtime.max_model_requests, source["max_model_requests"]),
                max_tool_calls=min(runtime.max_tool_calls, source["max_tool_calls"]),
            ),
            tools=None,
        )
