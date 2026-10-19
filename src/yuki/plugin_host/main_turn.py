"""Invocation-bound SDK generation through the shared Yuki compiler and runner."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from contextlib import AsyncExitStack
from contextvars import copy_context
from dataclasses import asdict, replace
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, select

from yuki.domain.conversations import ConversationScope
from yuki.domain.tool_actor import ToolActor
from yuki.llm.base import LLMInvalidRequestError
from yuki.memory.enums import MemoryScopeType
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.runtime.activation_outcome import ContextBoundaryChanged
from yuki.runtime.context_preparation import prepare_context
from yuki.services.agent_runner import AgentRunResult, AgentRuntime, AgentToolBackend
from yuki.services.agent_tools import ToolRuntime
from yuki.services.durable_invocations import project_committed_result
from yuki.services.invocation_context import InvocationContextFactory
from yuki.services.main_agent_backend import MainAgentBackend
from yuki.services.main_agent_turns import MainAgentTurnService
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.permissions import PluginPermission

if TYPE_CHECKING:
    from yuki.plugin_host.facades import HostPluginContext, PluginInvocation
    from yuki.runtime.work_control import WorkControl

_RUNNING: dict[str, asyncio.Task[AgentRunResult]] = {}
CALLBACK_WAIT_SECONDS = 5.0


async def close_plugin_main_tasks(plugin_id: str) -> None:
    tasks = [
        task for task in tuple(_RUNNING.values()) if task.get_name() == f"plugin-main-{plugin_id}"
    ]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _original_plugin_work(
    ledger: EventLedgerRepository,
    conversation_id: str | None,
    plugin_id: str,
    execution_id: str | None,
) -> dict[str, Any] | None:
    """Resolve the original durable invocation, independent of retired tool names."""
    from yuki.runtime.work_repository import WorkConflict
    from yuki.runtime.work_schema_v1 import work

    async with ledger._database.sessions() as session:
        originals = (
            (
                await session.execute(
                    select(work).where(
                        work.c.conversation_id == conversation_id,
                        work.c.parent_work_id.is_(None),
                        func.json_extract(work.c.source_json, "$.owner") == "plugin_invocation",
                        func.json_extract(work.c.source_json, "$.plugin_id") == plugin_id,
                        func.json_extract(work.c.source_json, "$.parent_execution_id")
                        == execution_id,
                    )
                )
            )
            .mappings()
            .all()
        )
    if len(originals) > 1:
        candidates = ",".join(str(item["id"]) for item in originals)
        raise WorkConflict(f"plugin_invocation_ambiguous:{candidates}")
    return dict(originals[0]) if originals else None


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
    from yuki.plugin_host.facades import _invocation_actor
    from yuki.runtime.work_activation import current_work_control
    from yuki.services.durable_invocations import invocation_boundary

    anchor: int | str | None = invocation.source_event_id
    if anchor is None:
        anchor = _invocation_actor(invocation).source_key
    execution_id = (
        f"plugin:{host.plugin_id}:{anchor}:"
        + hashlib.sha256((permission.value + instruction + context_data).encode()).hexdigest()
    )
    runtime = replace(runtime, execution_id=execution_id, work_control=None)
    key = invocation_boundary(runtime)
    execute = partial(
        _execute_plugin_main_turn,
        host,
        invocation,
        instruction=instruction,
        context_data=context_data,
        runtime=runtime,
        tools=tools,
        permission=permission,
        context_profile=context_profile,
    )
    task = _RUNNING.get(key)
    if task is not None and task.done():
        # A completed task may still be present before its done callback runs.
        # Reusing it would skip the durable work and authority checks below.
        _RUNNING.pop(key, None)
        task = None
    if task is None:
        # A separate invocation owns its original durable boundary. Inheriting
        # the caller's Work would reuse its journal and model loop. The existing
        # Conversation lease still prevents concurrent entry in the same scope.
        context = copy_context()
        context.run(current_work_control.set, None)
        task = asyncio.create_task(
            execute(),
            name=f"plugin-main-{host.plugin_id}",
            context=context,
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
            row = await _original_plugin_work(
                ledger, runtime.canonical_conversation_id, host.plugin_id, execution_id
            )
            if row is not None:
                return AgentRunResult(
                    text="",
                    tool_calls_used=0,
                    model_requests=0,
                    work_id=row["id"],
                    work_state=row["state"],
                    suppress_delivery=True,
                )
        # No durable owner exists yet. Stop preparation instead of leaving an
        # unqueryable activation behind a timed-out plugin callback.
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if ledger is not None:
            row = await _original_plugin_work(
                ledger, runtime.canonical_conversation_id, host.plugin_id, execution_id
            )
            if row is not None:  # Admission raced with cancellation; keep its original ID.
                return AgentRunResult(
                    text="",
                    tool_calls_used=0,
                    model_requests=0,
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
            await _original_plugin_work(
                ledger, runtime.canonical_conversation_id, host.plugin_id, execution_id
            )
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
    from yuki.runtime.work_activation import current_work_control

    runner = host._services.agent_runner
    contract = runner.main_contract if runner is not None else None
    ledger = host._services.ledger
    inbound = invocation.inbound
    if contract is None or ledger is None:
        raise PluginPermissionError("Yuki Main Agent services are unavailable")
    with contract.chat.runtime.executions.track():
        from yuki.plugin_host.facades import _invocation_actor

        actor = _invocation_actor(invocation)
        scope = (
            ConversationScope.group(
                actor.bot_user_id,
                actor.group_id,
                platform=actor.platform,
                conversation_id=actor.conversation_id,
                presence_id=actor.presence_id,
            )
            if actor.group_id is not None
            else ConversationScope.private(
                actor.bot_user_id,
                actor.user_id,
                platform=actor.platform,
                conversation_id=actor.conversation_id,
                presence_id=actor.presence_id,
            )
        )
        can_read_history = PluginPermission.MESSAGE_HISTORY_READ in host._approved_permissions
        version, _ = await ledger.read_scope_context(scope, limit=0)
        if version.conversation_id != invocation.conversation_id:
            raise PluginPermissionError(
                "Yuki generation source does not match the current Conversation"
            )
        execution_source = invocation.execution_source
        if execution_source is None and actor.principal_kind == "self":
            execution_source = {
                "origin": actor.origin.value,
                "principal_kind": actor.principal_kind,
                "initiative_run_id": actor.initiative_run_id,
                "conversation_id": actor.conversation_id,
                "generation": version.generation,
                "space_id": invocation.space_id,
                "presence_id": actor.presence_id,
                "bot_user_id": actor.bot_user_id,
                "group_id": actor.group_id,
                "instruction": actor.instruction,
            }

        async def validate() -> None:
            if host._require(permission) is not invocation:
                raise LLMInvalidRequestError("plugin invocation changed before model request")
            from yuki.identity.db_models import (
                CanonicalPersonModel,
                PresenceModel,
            )
            from yuki.identity.person_identity import current_person_id

            async with ledger._database.sessions() as session:
                person = (
                    await session.get(CanonicalPersonModel, actor.person_id)
                    if actor.person_id is not None
                    else None
                )
                presence = await session.get(PresenceModel, invocation.presence_id)
                current_person = (
                    await session.get(
                        CanonicalPersonModel, await current_person_id(session, actor.person_id)
                    )
                    if person is not None and actor.person_id is not None
                    else None
                )
            if (
                presence is None
                or presence.platform != actor.platform
                or presence.external_account_id != invocation.bot_user_id
                or (
                    actor.principal_kind == "person"
                    and (
                        person is None
                        or not person.enabled
                        or current_person is None
                        or not current_person.enabled
                    )
                )
            ):
                raise PluginPermissionError("plugin source identity is no longer active")
            if actor.event_id is not None:
                event = await ledger.get_event(actor.event_id)
                if (
                    event is None
                    or event.canonical_conversation_id != version.conversation_id
                    or event.direction != "inbound"
                    or event.event_kind != "message"
                    or event.sender_user_id != actor.user_id
                    or event.author_person_id != actor.person_id
                    or event.ingress_presence_id != actor.presence_id
                    or event.bot_user_id != actor.bot_user_id
                    or event.id <= version.starts_after_event_id
                ):
                    raise PluginPermissionError("plugin event source changed")
            elif execution_source is not None:
                from yuki.services.execution_sources import (
                    AutomationTaskSource,
                    SelfTaskSource,
                    recover_execution_source,
                )

                recovered = await recover_execution_source(
                    ledger._database,
                    cast(str, version.conversation_id),
                    execution_source,
                    request_id=actor.execution_id,
                )
                recovered_actor = cast(SelfTaskSource | AutomationTaskSource, recovered).actor(
                    actor.execution_id
                )
                if recovered_actor != actor:
                    raise PluginPermissionError("plugin execution source changed")
            if not await ledger.read_version_matches(version):
                raise ContextBoundaryChanged("plugin Conversation changed before model request")
            if runtime.before_model_request is not None:
                await runtime.before_model_request()

        control = current_work_control.get()
        if control is not None:
            control.validate = validate
        main = cast(MainAgentTurnService, contract.chat.runtime.main_turns)
        previous = None
        if control is None:
            previous = await _original_plugin_work(
                ledger, version.conversation_id, host.plugin_id, runtime.execution_id
            )
        result = await main.read_result(
            replace(
                runtime,
                invocation_source={
                    "owner": "plugin_invocation",
                    "plugin_id": host.plugin_id,
                    "approval_revision": host._services.approval_revision,
                },
                before_model_request=validate,
            ),
            previous=previous,
        )
        if result is not None:
            return result

        from yuki.runtime.work_activation import activate_work
        from yuki.runtime.work_repository import WorkRepository
        from yuki.services.durable_invocations import invocation_boundary

        invocation_source = {
            "owner": "plugin_invocation",
            "actor_person_id": invocation.actor_person_id,
            "principal_kind": actor.principal_kind,
            "actor_source": {**asdict(actor), "origin": actor.origin.value},
            "execution_source": execution_source,
            "generation": version.generation,
            "plugin_id": host.plugin_id,
            "approval_revision": host._services.approval_revision,
            "trigger_event_id": invocation.source_event_id,
            "instruction": instruction,
            "context_data": context_data,
            "context_profile": context_profile,
            "conversation_key": invocation.conversation_key,
            "permission": permission.value,
            "max_model_requests": runtime.max_model_requests,
            "max_tool_calls": runtime.max_tool_calls,
            "person_id": invocation.person_id,
            "space_id": invocation.space_id,
            "presence_id": invocation.presence_id,
            "bot_user_id": invocation.bot_user_id,
            "actor_is_superuser": runtime.actor_is_superuser,
        }
        repository = WorkRepository(ledger._database)
        boundary = (
            json.loads(previous["source_json"])["execution_boundary"]
            if previous is not None
            else invocation_boundary(runtime)
        )
        owned = None
        async with AsyncExitStack() as activation:
            if control is None:
                if previous is None:
                    previous = await repository.by_source(f"invocation:{boundary}")
                if contract.chat._settings.runtime_work_enabled or previous is not None:
                    source = (
                        json.loads(previous["source_json"])
                        if previous is not None
                        else {
                            **invocation_source,
                            "origin": runtime.origin.value,
                            "actor_user_id": runtime.actor_user_id,
                            "execution_boundary": boundary,
                            "parent_execution_id": runtime.execution_id,
                            "delivery_contract": "return_to_caller",
                        }
                    )
                    owned = await activation.enter_async_context(
                        activate_work(
                            repository,
                            cast(str, version.conversation_id),
                            version.generation,
                            f"invocation:{boundary}",
                            source,
                            validate,
                            bindings=contract.chat.runtime.bindings,
                            scope_key=invocation.conversation_key,
                        )
                    )
                    if owned.current is None:
                        await owned.execute(
                            "task_control",
                            {"action": "accept", "goal": instruction},
                            "host-invocation-admission",
                        )
            control = current_work_control.get()
            payload = {"plugin": {"id": host.plugin_id, "source_key": actor.source_key}}
            if context_data:
                payload["plugin"]["requested_context"] = context_data
            content = json.dumps(
                {
                    "origin": "plugin_request",
                    "instruction": instruction,
                },
                ensure_ascii=False,
            )
            from yuki.runtime.work_control import WorkInputsPreparing

            try:
                context = await prepare_context(
                    partial(
                        contract.chat._context_assembler.assemble_plugin,
                        identity=scope,
                        content=content,
                        metadata=payload,
                        read_history=can_read_history,
                        runtime=runtime.runtime_config,
                    ),
                    current_work_control.get(),
                    recovery_contract=await main.recovery_contract(
                        runtime.runtime_config, runtime.canonical_conversation_id
                    ),
                )
            except WorkInputsPreparing:
                active = cast("WorkControl", current_work_control.get())
                return AgentRunResult(
                    text="",
                    tool_calls_used=0,
                    model_requests=0,
                    work_id=cast(dict[str, Any], active.current)["id"],
                    work_state="waiting_external",
                    suppress_delivery=True,
                    outcome=active.outcome,
                )
            if context.read_version != version:
                raise ContextBoundaryChanged("plugin source changed during context preparation")
            execution_id = cast(str, runtime.execution_id)
            tool_runtime = ToolRuntime(
                inbound=inbound,
                actor_context=(
                    replace(actor, execution_id=execution_id, instruction=instruction)
                    if inbound is None
                    else None
                ),
                conversation_key=invocation.conversation_key,
                execution_id=execution_id,
                read_scope=scope,
                read_target_id=invocation.space_id or invocation.person_id,
                trigger_event_id=invocation.source_event_id,
                actor_is_superuser=runtime.actor_is_superuser,
                runtime_config=runtime.runtime_config,
                origin=actor.origin if inbound is None else runtime.origin,
                initiative_run_id=actor.initiative_run_id,
                sandbox_source=execution_source,
                memory_allowed_scopes=(
                    (
                        (MemoryScopeType.PERSON, MemoryScopeType.PERSON_GROUP)
                        if PluginPermission.MEMORY_PERSON_READ in host._approved_permissions
                        else ()
                    )
                    + (
                        (MemoryScopeType.GROUP,)
                        if PluginPermission.MEMORY_GROUP_READ in host._approved_permissions
                        else ()
                    )
                ),
                context_plugin_id=host.plugin_id,
                context_read_contract=hashlib.sha256(
                    json.dumps(
                        [permission.value, context_profile, control.source["allowed_tools"]]
                        if control is not None and "allowed_tools" in control.source
                        else [permission.value, context_profile],
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                conversation_id=invocation.conversation_id,
                person_id=invocation.person_id,
                space_id=invocation.space_id,
                scope_type=scope.scope_type,
                external_target_id=actor.group_id or actor.user_id,
                target_presence_id=actor.presence_id,
                before_model_request=validate,
            )
            tools = MainAgentBackend(
                contract.chat,
                tool_runtime,
                plugin_host=host,
            )
            async with contract.chat._turn_coordinator.hold(invocation.conversation_key):
                await validate()
                composition = await main.compose(
                    inbound=inbound,
                    context=context,
                    runtime=runtime.runtime_config,
                    include_plugin_context=False,
                    allowed_capabilities=runtime.allowed_capabilities,
                    before_preparation=validate,
                )

                async def validate_and_commit() -> None:
                    await validate()
                    if composition.commit_projection is not None:
                        await composition.commit_projection()

                result = await main.run(
                    composition.messages,
                    replace(
                        InvocationContextFactory.from_tools(
                            tool_runtime,
                            allowed_capabilities=runtime.allowed_capabilities,
                            max_tool_calls=runtime.max_tool_calls
                            if runtime.max_tool_calls is not None
                            else runtime.runtime_config.agent.max_tool_calls,
                            max_model_requests=runtime.max_model_requests
                            if runtime.max_model_requests is not None
                            else runtime.runtime_config.agent.max_model_requests,
                        ),
                        origin=runtime.origin,
                        delegated_authority=runtime.delegated_authority,
                        invocation_goal=instruction,
                        invocation_source=invocation_source,
                        before_model_request=validate_and_commit,
                        public_compaction=composition.public_compaction,
                    ),
                    tools,
                )
            if owned is not None and owned.current is not None:
                result = replace(result, work_id=owned.current["id"])
        if owned is not None and result.work_id is not None:
            settled = await repository.get(result.work_id)
            if settled is not None:
                result = project_committed_result(result, settled)
            result = replace(result, outcome=owned.outcome)
        return result


async def resume_plugin_work(
    lookup_host: Callable[[str], HostPluginContext | None],
    ledger: EventLedgerRepository,
    work: dict[str, Any],
    source: dict[str, Any],
) -> None:
    """Host-owned continuation after the original SDK callback has returned."""
    from dataclasses import replace

    from yuki.automation.models import TurnOrigin
    from yuki.domain.messages import InboundMessage, SenderIdentity
    from yuki.plugin_host.facades import PluginInvocation, _agent_dependencies
    from yuki.runtime.work_activation import activate_work
    from yuki.runtime.work_repository import WorkConflict, WorkRepository

    host = lookup_host(str(source.get("plugin_id", "")))
    if host is None:
        raise PluginPermissionError("plugin work owner is no longer enabled")
    if source.get("approval_revision") != host._services.approval_revision:
        raise PluginPermissionError("plugin approved version changed")
    permission = PluginPermission(source["permission"])
    inbound = None
    if source.get("trigger_event_id") is not None:
        event = await ledger.get_event(source["trigger_event_id"])
        if event is None or event.canonical_conversation_id != work["conversation_id"]:
            raise PluginPermissionError("plugin work source is unavailable")
        from yuki.identity.db_models import PresenceModel

        async with ledger._database.sessions() as session:
            presence = await session.get(PresenceModel, event.ingress_presence_id)
        if presence is None:
            raise PluginPermissionError("plugin work source is unavailable")

        inbound = InboundMessage(
            message_id=event.platform_message_id,
            source_event_id=event.id,
            author_kind=event.author_kind,
            author_account_id=event.author_account_id,
            segments=event.segments,
            occurred_at=event.occurred_at,
            event_type="message",
            scope_type=event.scope_type,
            sender=SenderIdentity(source["actor_user_id"]),
            text=event.content,
            bot_user_id=source["bot_user_id"],
            platform=presence.platform,
            mentioned_person_ids=event.mentioned_person_ids,
            thread_id=event.thread_id,
            group_id=event.group_id,
            received_at=event.occurred_at,
            person_id=source.get("person_id"),
            space_id=source.get("space_id"),
            presence_id=source.get("presence_id"),
            conversation_id=work["conversation_id"],
            legacy_conversation_key=source["conversation_key"],
        )
    saved_actor = source.get("actor_source")
    actor = (
        ToolActor(
            **{
                **saved_actor,
                "origin": TurnOrigin(saved_actor["origin"]),
                "mentioned_user_ids": tuple(saved_actor["mentioned_user_ids"]),
                "mentioned_person_ids": tuple(saved_actor.get("mentioned_person_ids", ())),
                **(
                    {"platform": inbound.platform, "space_id": inbound.space_id}
                    if inbound is not None
                    else {}
                ),
            }
        )
        if saved_actor is not None
        else ToolActor.from_inbound(inbound)
        if inbound is not None
        else None
    )
    if actor is not None and source.get("execution_source") is not None:
        from yuki.services.execution_sources import (
            AutomationTaskSource,
            SelfTaskSource,
            recover_execution_source,
        )

        recovered = await recover_execution_source(
            ledger._database,
            work["conversation_id"],
            source["execution_source"],
            request_id=actor.execution_id,
        )
        actor = cast(SelfTaskSource | AutomationTaskSource, recovered).actor(actor.execution_id)
    invocation = PluginInvocation(
        plugin_id=source["plugin_id"],
        origin=TurnOrigin.PLUGIN_SESSION,
        actor_user_id=actor.user_id if actor is not None else source["actor_user_id"],
        bot_user_id=source["bot_user_id"],
        actor_person_id=actor.person_id if actor is not None else source.get("actor_person_id"),
        actor=actor,
        execution_source=source.get("execution_source"),
        inbound=inbound,
        source_event_id=source.get("trigger_event_id"),
        person_id=source.get("person_id"),
        space_id=source.get("space_id"),
        presence_id=source.get("presence_id"),
        conversation_id=work["conversation_id"],
        legacy_conversation_key=source["conversation_key"],
    )
    async with host.bind(invocation):
        runner, runtime = await _agent_dependencies(host, invocation)

        async def validate() -> None:
            host._require(permission)

        contract = runner.main_contract
        if contract is None:
            raise PluginPermissionError("Yuki Main Agent services are unavailable")
        async with activate_work(
            WorkRepository(ledger._database),
            work["conversation_id"],
            work["generation"],
            work["source_key"],
            source,
            validate,
            work_id=work["id"],
            bindings=contract.chat.runtime.bindings,
            scope_key=invocation.conversation_key,
        ) as control:
            if control.current is None or control.current["id"] != work["id"]:
                raise WorkConflict("work_schedule_target_changed")
            await _execute_plugin_main_turn(
                host,
                invocation,
                instruction=work["goal"],
                context_data=source["context_data"],
                context_profile=source["context_profile"],
                permission=permission,
                runtime=replace(
                    runtime,
                    execution_id=source["parent_execution_id"],
                    max_model_requests=min(
                        (
                            limit
                            for limit in (
                                runtime.runtime_config.agent.max_model_requests,
                                source["max_model_requests"],
                            )
                            if limit is not None
                        ),
                        default=None,
                    ),
                    max_tool_calls=min(
                        (
                            limit
                            for limit in (
                                runtime.runtime_config.agent.max_tool_calls,
                                source["max_tool_calls"],
                            )
                            if limit is not None
                        ),
                        default=None,
                    ),
                ),
                tools=None,
            )
