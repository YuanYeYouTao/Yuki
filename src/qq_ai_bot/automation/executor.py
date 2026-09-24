"""Sequential DSL executor with hard limits, audit, and uncertain-send handling."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select

from qq_ai_bot.automation.agent_delivery import inspect_agent_delivery
from qq_ai_bot.automation.authority import (
    AuthorityContext,
    DelegatedAuthority,
    PermissionLevel,
    permission_for_accounts,
)
from qq_ai_bot.automation.context import AutomationBindError, bind_automation_conversation
from qq_ai_bot.automation.gateway import ProactiveGatewayError
from qq_ai_bot.automation.model_delivery import classify_model_delivery
from qq_ai_bot.automation.models import (
    AutomationRecord,
    AutomationRunRecord,
    AutomationStatus,
    ExecutionResult,
    RetryPolicy,
    RiskClass,
    RunStatus,
    TurnOrigin,
)
from qq_ai_bot.automation.registry import (
    AutomationCapability,
    AutomationCapabilityRegistry,
    CapabilityExecutionContext,
    CapabilityResult,
)
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.templates import TemplateError, resolve_templates
from qq_ai_bot.config import Settings
from qq_ai_bot.control_plane.principal import ControlPrincipal, PrincipalSource
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    PersonActiveRouteModel,
    SpaceActiveRouteModel,
)
from qq_ai_bot.domain.identity import PersonId, PrincipalId
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.routing import PresenceRouter, RouteSendError
from qq_ai_bot.runtime.principal import SELF, PrincipalRef
from qq_ai_bot.time.service import TimeContextService

_SEND_CAPABILITIES = frozenset(
    {
        "social.send_message",
        "social.poke_person",
    }
)

logger = logging.getLogger(__name__)
_RUN_USAGE_FIELDS = ("steps_completed", "llm_calls", "tool_calls", "messages_sent")


def _canonical_identity(
    record: AutomationRecord,
) -> tuple[str, str | None, str | None, str | None]:
    return (
        record.creator_kind,
        record.canonical_creator_person_id,
        record.canonical_target_person_id,
        record.canonical_target_space_id,
    )


@dataclass(frozen=True, slots=True)
class _ExecutionSnapshot:
    record: AutomationRecord
    allowed: frozenset[str]
    actor_is_superuser: bool


class AutomationExecutionError(RuntimeError):
    def __init__(
        self,
        category: str,
        *,
        transient: bool = False,
        uncertain: bool = False,
        llm_calls: int = 0,
        tool_calls: int = 0,
        messages_sent: int = 0,
    ) -> None:
        super().__init__(category)
        self.category = category
        self.transient = transient
        self.uncertain = uncertain
        self.llm_calls = llm_calls
        self.tool_calls = tool_calls
        self.messages_sent = messages_sent


class AutomationExecutor:
    """Run one claimed script within its creator authority and resource quotas."""

    def __init__(
        self,
        *,
        settings: Settings,
        registry: AutomationCapabilityRegistry,
        repository: AutomationRepository,
        time_service: TimeContextService,
        gateway_factory: Callable[[CapabilityExecutionContext], object] | None = None,
        router: PresenceRouter | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._repository = repository
        self._time = time_service
        self._gateway_factory = gateway_factory
        self._router = router

    async def execute(
        self,
        automation: AutomationRecord,
        run: AutomationRunRecord,
        *,
        current_group_id: str | None = None,
    ) -> ExecutionResult:
        from qq_ai_bot.automation.work_cursor import load as load_cursor

        phase, cursor = await load_cursor(
            self._repository._database, run.id, automation.script_hash
        )
        usage = {
            field: max(getattr(run, field), int(cursor.get(field, 0)))
            for field in _RUN_USAGE_FIELDS
        }
        result = await self._execute(
            automation,
            run,
            phase=phase,
            cursor={**cursor, **usage},
            current_group_id=current_group_id,
        )
        # Preflight rejection is still the same run, including when its cursor
        # belongs to an older script. It must not erase committed usage/evidence.
        return result.model_copy(
            update={
                **{field: max(value, getattr(result, field)) for field, value in usage.items()},
                "summary": {**run.result_summary, **result.summary},
            }
        )

    async def _execute(
        self,
        automation: AutomationRecord,
        run: AutomationRunRecord,
        *,
        phase: str,
        cursor: dict[str, Any],
        current_group_id: str | None,
    ) -> ExecutionResult:
        snapshot = await self._begin_execution(automation)
        if isinstance(snapshot, ExecutionResult):
            return snapshot
        if snapshot.record.script_hash != automation.script_hash:
            phase = "changed"
        automation = snapshot.record
        if not self._settings.runtime_work_enabled and any(
            step.call in {"yuki.agent", "yuki.generate"} for step in automation.script.steps
        ):
            return ExecutionResult(
                status=RunStatus.BLOCKED, error_category="automation_runtime_required"
            )
        allowed = snapshot.allowed
        actor_is_superuser = snapshot.actor_is_superuser
        authority = DelegatedAuthority.model_validate(automation.authority_snapshot)
        if current_group_id is None:
            current_group_id = authority.current_group_id
        blocked_send = await self._revalidate_canonical_send(automation)
        if blocked_send is not None:
            return blocked_send
        local = self._time.at(run.actual_started_at, automation.timezone)
        authority_context = AuthorityContext(
            origin=TurnOrigin.SCHEDULED_AUTOMATION,
            actor_user_id=automation.creator_user_id,
            actor_is_superuser=actor_is_superuser,
            bot_user_id=automation.bot_user_id,
            principal_kind=automation.creator_kind,
            delegated_authority=authority,
            allowed_capabilities=allowed,
        )

        async def revalidate_authority(capability: str | None) -> None:
            fresh = await self._begin_execution(automation)
            if isinstance(fresh, ExecutionResult):
                raise AutomationExecutionError(
                    fresh.error_category or "delegated_authority_revoked"
                )
            if (
                fresh.record.script_hash != automation.script_hash
                or fresh.record.authority_snapshot != automation.authority_snapshot
            ):
                raise AutomationExecutionError("automation_changed")
            if fresh.actor_is_superuser != actor_is_superuser:
                raise AutomationExecutionError("actor_permission_changed")
            if capability is not None and (
                capability not in allowed or capability not in fresh.allowed
            ):
                raise AutomationExecutionError("capability_not_delegated")

        builtins: dict[str, Any] = {
            "creator_user_id": automation.creator_user_id,
            "bot_user_id": automation.bot_user_id,
            "automation_id": automation.id,
            "automation_run_id": run.id,
            "scheduled_for": run.scheduled_for.isoformat(),
            "actual_started_at": run.actual_started_at.isoformat(),
            "local_time": local.local.isoformat(),
            "current_group_id": current_group_id,
        }
        outputs: dict[str, Any] = {}
        steps_completed = llm_calls = tool_calls = messages_sent = 0
        from qq_ai_bot.automation.work_cursor import save as save_cursor

        if phase == "new":
            # Legacy orphan runs have no proof that dispatch never started. Keep
            # their identity and usage; an empty replacement cursor would authorize replay.
            return ExecutionResult(
                status=RunStatus.UNCERTAIN,
                error_category="missing_initial_run_cursor",
            )
        if phase in {"dispatching", "changed"}:
            return ExecutionResult(
                status=RunStatus.UNCERTAIN, error_category="step_outcome_requires_reconciliation"
            )
        outputs = cursor.get("outputs", {})
        steps_completed = int(cursor.get("steps_completed", 0))
        llm_calls = int(cursor.get("llm_calls", 0))
        tool_calls = int(cursor.get("tool_calls", 0))
        messages_sent = int(cursor.get("messages_sent", 0))
        next_step = int(cursor.get("next_step", 0))
        pending_usage = dict(cursor.get("pending_usage", {"models": 0, "tools": 0}))
        active_before = float(cursor.get("active_seconds", 0))
        activated_at = time.monotonic()

        async def checkpoint(phase: str, index: int, work_id: str | None = None) -> None:
            await save_cursor(
                self._repository._database,
                run.id,
                automation.script_hash,
                phase,
                {
                    "outputs": outputs,
                    "steps_completed": steps_completed,
                    "llm_calls": llm_calls,
                    "tool_calls": tool_calls,
                    "messages_sent": messages_sent,
                    "next_step": index,
                    "work_id": work_id,
                    "pending_usage": pending_usage,
                    "active_seconds": active_before + time.monotonic() - activated_at,
                },
                expected_owner=automation.claimed_by,
            )

        web_was_used = False
        conversation_key = f"automation:{automation.id}"
        conversation_id = None
        conversation_generation = None
        try:
            async with self._repository._database.sessions() as session:
                conversation_key, conversation_id = await bind_automation_conversation(
                    session, automation
                )
                if conversation_id is not None:
                    conversation = await session.get(CanonicalConversationModel, conversation_id)
                    if conversation is None:
                        raise AutomationBindError("conversation_not_found")
                    conversation_generation = conversation.generation
        except AutomationBindError as exc:
            return ExecutionResult(
                status=RunStatus.BLOCKED,
                error_category=exc.category,
                summary={"reason": "canonical conversation hydrate failed"},
            )
        if conversation_id is None and any(
            step.arguments.get("delivery_target") in {"self_private", "current_group"}
            for step in automation.script.steps
            if step.call == "yuki.agent"
        ):
            return ExecutionResult(
                status=RunStatus.BLOCKED, error_category="work_conversation_unavailable"
            )
        send_capabilities = {
            item.name for item in self._registry.list() if item.risk_class is RiskClass.SEND
        }
        model_deliveries = {
            index
            for index in range(len(automation.script.steps))
            if classify_model_delivery(
                automation.script, index, send_capabilities=send_capabilities
            )
        }
        try:
            async with asyncio.timeout(
                None
                if automation.script.uses_runtime_budget
                else max(0, automation.script.limits.timeout_seconds - active_before)
            ):
                for index, step in enumerate(automation.script.steps):
                    if index < next_step:
                        continue
                    if index in model_deliveries:
                        raise AutomationExecutionError("model_delivery_requires_agent_send")
                    if (
                        step.call == "yuki.generate"
                        and model_deliveries
                        and not (phase == "agent" and index == next_step)
                    ):
                        raise AutomationExecutionError("model_delivery_requires_agent_send")
                    definition = self._registry.require(step.call)
                    if step.call not in allowed:
                        raise AutomationExecutionError("capability_not_delegated")
                    try:
                        resolved = resolve_templates(
                            step.arguments,
                            builtins=builtins,
                            step_outputs=outputs,
                        )
                        arguments = definition.validate_arguments(resolved)
                    except (TemplateError, ValueError) as exc:
                        raise AutomationExecutionError("runtime_argument_validation") from exc
                    context = CapabilityExecutionContext(
                        authority=authority_context,
                        automation_id=automation.id,
                        automation_run_id=run.id,
                        step_id=step.id,
                        creator_user_id=automation.creator_user_id,
                        creator_kind=automation.creator_kind,
                        bot_user_id=automation.bot_user_id,
                        current_group_id=current_group_id,
                        scheduled_for=run.scheduled_for,
                        actual_started_at=run.actual_started_at,
                        local_time=local.local,
                        timezone=automation.timezone,
                        automation_context=automation.script.context,
                        conversation_key=conversation_key,
                        web_was_used=web_was_used,
                        canonical_creator_person_id=automation.canonical_creator_person_id,
                        canonical_target_person_id=automation.canonical_target_person_id,
                        canonical_target_space_id=automation.canonical_target_space_id,
                        canonical_presence_id=automation.canonical_presence_id,
                        canonical_conversation_id=conversation_id,
                        conversation_generation=conversation_generation,
                        automation_script_hash=automation.script_hash,
                        source_step_id=step.id,
                        revalidate_authority=revalidate_authority,
                    )
                    if self._gateway_factory is not None:
                        context = replace(
                            context,
                            gateway=self._gateway_factory(context),
                        )
                    started = self._time.clock.now()
                    resume_plugin = bool(
                        phase == "agent"
                        and index == next_step
                        and cursor.get("work_id")
                        and definition.provider_plugin_id is not None
                    )
                    await checkpoint(
                        "agent"
                        if resume_plugin or step.call in {"yuki.agent", "yuki.generate"}
                        else "dispatching",
                        index,
                        str(cursor["work_id"]) if resume_plugin else None,
                    )
                    try:
                        if (
                            not resume_plugin
                            and automation.script.uses_runtime_budget
                            and step.call
                            not in {
                                "yuki.agent",
                                "yuki.generate",
                            }
                        ):
                            from qq_ai_bot.runtime.work_budget import (
                                WorkBudgetExceeded,
                                charge_automation_run,
                            )

                            try:
                                async with (
                                    self._repository._database.immediate_session() as budget_session
                                ):
                                    await charge_automation_run(
                                        budget_session, run.id, models=0, tools=1
                                    )
                            except WorkBudgetExceeded as exc:
                                raise AutomationExecutionError("agent_work_blocked") from exc
                        if resume_plugin:
                            from qq_ai_bot.plugin_host.automation_adapter import (
                                resume_plugin_result,
                            )

                            result = await resume_plugin_result(
                                self._repository._database,
                                str(cursor["work_id"]),
                                definition,
                                context,
                                pending_usage,
                            )
                        else:
                            result = await self._execute_capability(definition, arguments, context)
                        delivery_target = arguments.get("delivery_target")
                        if (
                            step.call == "yuki.agent"
                            and delivery_target in {"self_private", "current_group"}
                            and result.pending_work_id is None
                        ):
                            delivery_state = await self._agent_delivery_state(
                                automation, run, step.id, conversation_id, str(delivery_target)
                            )
                            if delivery_state != "succeeded":
                                raise AutomationExecutionError(
                                    "agent_delivery_outcome_uncertain"
                                    if delivery_state == "uncertain"
                                    else "agent_delivery_unconfirmed",
                                    uncertain=delivery_state == "uncertain",
                                    llm_calls=result.llm_calls,
                                    tool_calls=result.tool_calls,
                                    messages_sent=result.messages_sent,
                                )
                    except AutomationExecutionError as exc:
                        if (
                            exc.category == "agent_work_blocked"
                            and arguments.get("delivery_target")
                            in {"self_private", "current_group"}
                            and await self._agent_delivery_state(
                                automation,
                                run,
                                step.id,
                                conversation_id,
                                str(arguments["delivery_target"]),
                            )
                            == "uncertain"
                        ):
                            exc.category = "agent_delivery_outcome_uncertain"
                            exc.uncertain = True
                        llm_calls += exc.llm_calls
                        tool_calls += exc.tool_calls
                        messages_sent += exc.messages_sent
                        finished = self._time.clock.now()
                        await self._repository.record_step(
                            run_id=run.id,
                            step_id=step.id,
                            capability=step.call,
                            status="uncertain" if exc.uncertain else "failed",
                            input_summary=_summary(arguments),
                            output_summary={},
                            started_at=started,
                            finished_at=finished,
                            error_category=exc.category,
                        )
                        self._log_step(
                            automation,
                            run,
                            capability=step.call,
                            step_id=step.id,
                            started=started,
                            finished=finished,
                            status="uncertain" if exc.uncertain else "failed",
                            error_category=exc.category,
                        )
                        raise
                    finished = self._time.clock.now()
                    if result.pending_work_id is not None:
                        pending_usage["models"] += result.llm_calls
                        pending_usage["tools"] += result.tool_calls
                        llm_calls += result.llm_calls
                        tool_calls += result.tool_calls
                        messages_sent += result.messages_sent
                        self._enforce_runtime_limits(
                            automation,
                            llm_calls=llm_calls,
                            tool_calls=tool_calls,
                            messages_sent=messages_sent,
                        )
                        await checkpoint("agent", index, result.pending_work_id)
                        return ExecutionResult(
                            status=RunStatus.RUNNING,
                            steps_completed=steps_completed,
                            llm_calls=llm_calls,
                            tool_calls=tool_calls,
                            messages_sent=messages_sent,
                            summary={"pending_work_id": result.pending_work_id},
                        )
                    await self._repository.record_step(
                        run_id=run.id,
                        step_id=step.id,
                        capability=step.call,
                        status="succeeded",
                        input_summary=_summary(arguments),
                        output_summary=_summary(result.data),
                        started_at=started,
                        finished_at=finished,
                        error_category=None,
                    )
                    self._log_step(
                        automation,
                        run,
                        capability=step.call,
                        step_id=step.id,
                        started=started,
                        finished=finished,
                        status="succeeded",
                        error_category=None,
                    )
                    outputs[step.id] = result.data
                    if step.save_as:
                        outputs[step.save_as] = result.data
                    steps_completed += 1
                    llm_calls += result.llm_calls
                    tool_calls += result.tool_calls
                    messages_sent += result.messages_sent
                    web_was_used = web_was_used or step.call in {"web.search", "web.read_page"}
                    self._enforce_runtime_limits(
                        automation,
                        llm_calls=llm_calls,
                        tool_calls=tool_calls,
                        messages_sent=messages_sent,
                    )
                    pending_usage = {"models": 0, "tools": 0}
                    await checkpoint("ready", index + 1)
        except TimeoutError:
            return ExecutionResult(
                status=RunStatus.FAILED,
                steps_completed=steps_completed,
                llm_calls=llm_calls,
                tool_calls=tool_calls,
                messages_sent=messages_sent,
                error_category="runtime_timeout",
            )
        except AutomationExecutionError as exc:
            if exc.category == "conversation_activation_busy":
                return ExecutionResult(
                    status=RunStatus.RUNNING,
                    steps_completed=steps_completed,
                    llm_calls=llm_calls,
                    tool_calls=tool_calls,
                    messages_sent=messages_sent,
                    error_category=exc.category,
                )
            if exc.category in {
                "paused",
                "ambiguous",
                "none",
                "disconnected",
                "no_connection",
                "capability",
                "bot_unavailable",
                "state_mismatch",
                "target_missing",
                "operation_unavailable",
                "agent_work_blocked",
                "agent_delivery_unconfirmed",
                "legacy_model_delivery_requires_update",
            }:
                return ExecutionResult(
                    status=RunStatus.BLOCKED,
                    steps_completed=steps_completed,
                    llm_calls=llm_calls,
                    tool_calls=tool_calls,
                    messages_sent=messages_sent,
                    error_category=exc.category,
                )
            return ExecutionResult(
                status=RunStatus.UNCERTAIN if exc.uncertain else RunStatus.FAILED,
                steps_completed=steps_completed,
                llm_calls=llm_calls,
                tool_calls=tool_calls,
                messages_sent=messages_sent,
                error_category=exc.category,
            )
        return ExecutionResult(
            status=RunStatus.SUCCEEDED,
            steps_completed=steps_completed,
            llm_calls=llm_calls,
            tool_calls=tool_calls,
            messages_sent=messages_sent,
            summary={"output_steps": list(outputs)},
        )

    async def _agent_delivery_state(
        self,
        automation: AutomationRecord,
        run: AutomationRunRecord,
        step_id: str,
        conversation_id: str | None,
        target: str,
    ) -> str:
        kind: Literal["person", "space"]
        kind, target_id = (
            ("space", automation.canonical_target_space_id)
            if target == "current_group"
            else ("person", automation.canonical_creator_person_id)
        )
        if conversation_id is None or target_id is None:
            return "none"
        outcome = await inspect_agent_delivery(
            self._repository._database,
            conversation_id=conversation_id,
            run_id=run.id,
            step_id=step_id,
            script_hash=automation.script_hash,
            target_kind=kind,
            target_id=target_id,
        )
        return outcome.state

    async def _begin_execution(
        self, claimed: AutomationRecord, *, allow_completed: bool = False
    ) -> _ExecutionSnapshot | ExecutionResult:
        async with self._repository._database.sessions() as session:
            current = await self._repository.get(claimed.id, session=session)
            if current is None:
                return ExecutionResult(
                    status=RunStatus.BLOCKED,
                    error_category="automation_inactive",
                    summary={"reason": "automation row is missing"},
                )
            if current.claimed_by != claimed.claimed_by:
                return ExecutionResult(
                    status=RunStatus.BLOCKED,
                    error_category="automation_lease_lost",
                    summary={"reason": "claimed execution owner is stale"},
                )
            if _canonical_identity(current) != _canonical_identity(claimed):
                return ExecutionResult(
                    status=RunStatus.BLOCKED,
                    error_category="state_mismatch",
                    summary={"reason": "claimed canonical identity is stale"},
                )
            if current.status is not AutomationStatus.ACTIVE and not (
                allow_completed and current.status is AutomationStatus.COMPLETED
            ):
                return ExecutionResult(
                    status=RunStatus.BLOCKED,
                    error_category="automation_inactive",
                    summary={"reason": "automation is not active"},
                )
            blocked = await self._validate_canonical_identity(session, current)
            if blocked is not None:
                return blocked
            loaded = await self._canonical_creator_principal(session, current)
            if isinstance(loaded, ExecutionResult):
                return loaded
            principal, current_permission = loaded
            if current.creator_kind != "self" and (
                not isinstance(principal, ControlPrincipal)
                or not principal.authenticated
                or not principal.active
            ):
                return ExecutionResult(
                    status=RunStatus.BLOCKED,
                    error_category="delegated_authority_revoked",
                    summary={"reason": "control principal is no longer active"},
                )
            allowed = frozenset(
                item.name for item in self._registry.list() if item.permits(current_permission)
            )
            if not {step.call for step in current.script.steps}.issubset(allowed):
                return ExecutionResult(
                    status=RunStatus.BLOCKED,
                    error_category="delegated_authority_revoked",
                    summary={"reason": "required capability is no longer delegated"},
                )
            return _ExecutionSnapshot(
                record=current,
                allowed=allowed,
                actor_is_superuser=current_permission is PermissionLevel.SUPERUSER,
            )

    async def _validate_canonical_identity(
        self, session: Any, automation: AutomationRecord
    ) -> ExecutionResult | None:
        person_id = automation.canonical_target_person_id
        space_id = automation.canonical_target_space_id
        if person_id and space_id:
            return ExecutionResult(
                status=RunStatus.BLOCKED,
                error_category="state_mismatch",
                summary={"reason": "canonical target is not XOR"},
            )
        if not person_id and not space_id:
            return ExecutionResult(
                status=RunStatus.BLOCKED,
                error_category="target_missing",
                summary={"reason": "automation has no canonical target"},
            )
        if person_id:
            person = await session.get(CanonicalPersonModel, person_id)
            if person is None or not person.enabled:
                return ExecutionResult(
                    status=RunStatus.BLOCKED,
                    error_category="target_disabled",
                    summary={"reason": "canonical person is missing or disabled"},
                )
            route = await session.get(PersonActiveRouteModel, person_id)
        else:
            space = await session.get(CanonicalSpaceModel, space_id or "")
            if space is None or not space.enabled:
                return ExecutionResult(
                    status=RunStatus.BLOCKED,
                    error_category="target_disabled",
                    summary={"reason": "canonical space is missing or disabled"},
                )
            route = await session.get(SpaceActiveRouteModel, space_id)
        if route is not None and route.paused:
            return ExecutionResult(
                status=RunStatus.BLOCKED,
                error_category="paused",
                summary={"reason": "active route is paused"},
            )
        return None

    async def _canonical_creator_principal(
        self, session: Any, automation: AutomationRecord
    ) -> tuple[ControlPrincipal | PrincipalRef, PermissionLevel] | ExecutionResult:
        if automation.creator_kind == "self":
            scene = automation.authority_snapshot
            if (
                automation.canonical_creator_person_id is not None
                or automation.creator_user_id
                or not automation.canonical_target_space_id
                or automation.canonical_target_person_id is not None
                or automation.authority_snapshot.get("principal_kind") != "self"
                or scene.get("canonical_space_id") != automation.canonical_target_space_id
                or scene.get("canonical_presence_id") != automation.canonical_presence_id
            ):
                return ExecutionResult(status=RunStatus.BLOCKED, error_category="state_mismatch")
            conversation = await session.get(
                CanonicalConversationModel, scene.get("canonical_conversation_id")
            )
            presence = await session.get(PresenceModel, automation.canonical_presence_id)
            bindings = (
                await session.scalars(
                    select(SpaceBindingModel)
                    .where(
                        SpaceBindingModel.space_id == automation.canonical_target_space_id,
                        SpaceBindingModel.platform == "qq",
                        SpaceBindingModel.status == "active",
                    )
                    .limit(2)
                )
            ).all()
            if (
                conversation is None
                or conversation.space_id != automation.canonical_target_space_id
                or conversation.generation != scene.get("conversation_generation")
                or presence is None
                or not presence.enabled
                or presence.platform != "qq"
                or presence.external_account_id != automation.bot_user_id
                or len(bindings) != 1
                or bindings[0].external_space_id != scene.get("current_group_id")
            ):
                return ExecutionResult(
                    status=RunStatus.BLOCKED, error_category="self_scene_changed"
                )
            return SELF, PermissionLevel.SELF
        creator_id = automation.canonical_creator_person_id
        if not creator_id:
            return ExecutionResult(
                status=RunStatus.BLOCKED,
                error_category="target_missing",
                summary={"reason": "automation has no canonical creator"},
            )
        person = await session.get(CanonicalPersonModel, creator_id)
        if person is None or not person.enabled:
            return ExecutionResult(
                status=RunStatus.BLOCKED,
                error_category="delegated_authority_revoked",
                summary={"reason": "canonical creator is missing or disabled"},
            )
        accounts = list(
            await session.scalars(
                select(IdentityBindingModel.external_account_id).where(
                    IdentityBindingModel.person_id == creator_id,
                    IdentityBindingModel.status == "active",
                )
            )
        )
        if not accounts:
            return ExecutionResult(
                status=RunStatus.BLOCKED,
                error_category="delegated_authority_revoked",
                summary={"reason": "canonical creator has no active binding"},
            )
        person_id = PersonId.parse(creator_id)
        if automation.creator_user_id not in accounts:
            return ExecutionResult(
                status=RunStatus.BLOCKED, error_category="actor_identity_changed"
            )
        current_permission = permission_for_accounts(self._settings, (automation.creator_user_id,))
        principal = ControlPrincipal(
            principal_id=PrincipalId.parse(person_id.text),
            person_id=person_id,
            source=PrincipalSource.QQ,
            roles=("superuser",) if current_permission is PermissionLevel.SUPERUSER else ("user",),
            granted_capabilities=(),
            authenticated=True,
            active=True,
        )
        return principal, current_permission

    async def _revalidate_canonical_send(
        self, automation: AutomationRecord
    ) -> ExecutionResult | None:
        needs_send = bool(_SEND_CAPABILITIES.intersection(automation.required_capabilities))
        if not needs_send:
            return None
        if self._router is None:
            return ExecutionResult(
                status=RunStatus.BLOCKED,
                error_category="operation_unavailable",
                summary={"reason": "canonical send requires PresenceRouter"},
            )
        person_id = automation.canonical_target_person_id
        space_id = automation.canonical_target_space_id
        try:
            if person_id:
                await self._router.resolve_send_for_person(person_id)
            elif space_id:
                await self._router.resolve_send_for_space(space_id)
        except RouteSendError as exc:
            return ExecutionResult(
                status=RunStatus.BLOCKED,
                error_category=exc.category,
                summary={"reason": "send route is not uniquely live"},
            )
        return None

    async def _execute_capability(
        self,
        definition: AutomationCapability,
        arguments: dict[str, Any],
        context: CapabilityExecutionContext,
    ) -> CapabilityResult:
        if definition.handler is None:
            raise AutomationExecutionError("capability_handler_unavailable")
        attempts = (
            2
            if definition.retry_policy is RetryPolicy.TRANSIENT_ONCE
            and definition.risk_class is RiskClass.READ
            and definition.name not in {"yuki.agent", "yuki.generate"}
            else 1
        )
        for attempt in range(attempts):
            try:
                if context.revalidate_authority is not None:
                    await context.revalidate_authority(definition.name)
                return await definition.handler(arguments, context)
            except ProactiveGatewayError as exc:
                raise AutomationExecutionError(exc.category, uncertain=exc.uncertain) from exc
            except AutomationExecutionError as exc:
                if not exc.transient or attempt + 1 >= attempts:
                    raise
            except Exception as exc:
                from qq_ai_bot.runtime.work_repository import WorkConflict

                if isinstance(exc, WorkConflict) and str(exc) == "conversation_activation_busy":
                    raise AutomationExecutionError("conversation_activation_busy") from exc
                logger.error(
                    "automation_capability_failed capability=%s category=%s",
                    definition.name,
                    type(exc).__name__,
                )
                raise AutomationExecutionError("capability_execution_failed") from exc
        raise AutomationExecutionError("capability_failed")

    @staticmethod
    def _enforce_runtime_limits(
        automation: AutomationRecord,
        *,
        llm_calls: int,
        tool_calls: int,
        messages_sent: int,
    ) -> None:
        limits = automation.script.limits
        if not automation.script.uses_runtime_budget and llm_calls > limits.max_llm_calls:
            raise AutomationExecutionError("llm_limit_exceeded")
        if not automation.script.uses_runtime_budget and tool_calls > limits.max_tool_calls:
            raise AutomationExecutionError("tool_limit_exceeded")
        if messages_sent > limits.max_messages:
            raise AutomationExecutionError("message_limit_exceeded")

    @staticmethod
    def _log_step(
        automation: AutomationRecord,
        run: AutomationRunRecord,
        *,
        capability: str,
        step_id: str,
        started: datetime,
        finished: datetime,
        status: str,
        error_category: str | None,
    ) -> None:
        logger.info(
            "automation_step_finished automation_id=%d run_id=%d creator_user_id=%s "
            "bot_user_id=%s schedule_type=%s capability=%s step_id=%s duration_seconds=%.3f "
            "status=%s error_category=%s",
            automation.id,
            run.id,
            automation.creator_user_id,
            automation.bot_user_id,
            automation.script.schedule.type,
            capability,
            step_id,
            max(0.0, (finished - started).total_seconds()),
            status,
            error_category,
        )


def _summary(value: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"keys": sorted(value)[:50]}
    for key in ("user_id", "group_id", "action", "status", "ok"):
        if key in value:
            summary[key] = value[key]
    for key in ("text", "content", "relevant_content"):
        item = value.get(key)
        if isinstance(item, str):
            summary[f"{key}_characters"] = len(item)
    return summary
