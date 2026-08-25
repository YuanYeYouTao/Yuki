"""Sequential DSL executor with hard limits, audit, and uncertain-send handling."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from sqlalchemy import select

from qq_ai_bot.automation.authority import (
    AuthorityContext,
    DelegatedAuthority,
    PermissionLevel,
    effective_delegated_capabilities,
    permission_for,
    permission_for_accounts,
)
from qq_ai_bot.automation.context import AutomationBindError, bind_automation_conversation
from qq_ai_bot.automation.gateway import ProactiveGatewayError
from qq_ai_bot.automation.models import (
    AutomationRecord,
    AutomationRunRecord,
    AutomationStatus,
    ExecutionResult,
    RetryPolicy,
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
    PersonActiveRouteModel,
    SpaceActiveRouteModel,
)
from qq_ai_bot.domain.identity import PersonId, PrincipalId
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
)
from qq_ai_bot.identity.routing import PresenceRouter, RouteSendError
from qq_ai_bot.identity.runtime import identity_runtime_is_complete_v2
from qq_ai_bot.time.service import TimeContextService

_SEND_CAPABILITIES = frozenset(
    {
        "onebot.send_private_message",
        "onebot.send_group_message",
        "speech.send_private",
        "speech.send_group",
        "emoji.send",
        "emoji.send_by_id",
    }
)

logger = logging.getLogger(__name__)


def _canonical_identity(
    record: AutomationRecord,
) -> tuple[str | None, str | None, str | None]:
    return (
        record.canonical_creator_person_id,
        record.canonical_target_person_id,
        record.canonical_target_space_id,
    )


@dataclass(frozen=True, slots=True)
class _ExecutionSnapshot:
    record: AutomationRecord
    complete_v2: bool
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
        snapshot = await self._begin_execution(automation)
        if isinstance(snapshot, ExecutionResult):
            return snapshot
        automation = snapshot.record
        allowed = snapshot.allowed
        actor_is_superuser = snapshot.actor_is_superuser
        authority = DelegatedAuthority.model_validate(automation.authority_snapshot)
        if current_group_id is None:
            current_group_id = authority.current_group_id
        if snapshot.complete_v2:
            blocked_send = await self._revalidate_v2_send(automation)
            if blocked_send is not None:
                return blocked_send
        local = self._time.at(run.actual_started_at, automation.timezone)
        authority_context = AuthorityContext(
            origin=TurnOrigin.SCHEDULED_AUTOMATION,
            actor_user_id=automation.creator_user_id,
            actor_is_superuser=actor_is_superuser,
            bot_user_id=automation.bot_user_id,
            delegated_authority=authority,
            allowed_capabilities=allowed,
        )
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
        web_was_used = False
        conversation_key = f"automation:{automation.id}"
        conversation_id = None
        try:
            async with self._repository._database.sessions() as session:
                conversation_key, conversation_id = await bind_automation_conversation(
                    session, automation
                )
        except AutomationBindError as exc:
            return ExecutionResult(
                status=RunStatus.BLOCKED,
                error_category=exc.category,
                summary={"reason": "canonical conversation hydrate failed"},
            )
        try:
            async with asyncio.timeout(automation.script.limits.timeout_seconds):
                for step in automation.script.steps:
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
                        bot_user_id=automation.bot_user_id,
                        current_group_id=current_group_id,
                        scheduled_for=run.scheduled_for,
                        actual_started_at=run.actual_started_at,
                        local_time=local.local,
                        timezone=automation.timezone,
                        automation_context=automation.script.context,
                        conversation_key=conversation_key,
                        web_was_used=web_was_used,
                        canonical_target_person_id=automation.canonical_target_person_id,
                        canonical_target_space_id=automation.canonical_target_space_id,
                        canonical_conversation_id=conversation_id,
                    )
                    if self._gateway_factory is not None:
                        context = replace(
                            context,
                            gateway=self._gateway_factory(context),
                        )
                    started = self._time.clock.now()
                    try:
                        result = await self._execute_capability(definition, arguments, context)
                    except AutomationExecutionError as exc:
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

    async def _begin_execution(
        self, claimed: AutomationRecord
    ) -> _ExecutionSnapshot | ExecutionResult:
        async with self._repository._database.sessions() as session:
            if not await identity_runtime_is_complete_v2(session):
                if claimed.status is not AutomationStatus.ACTIVE:
                    return ExecutionResult(
                        status=RunStatus.BLOCKED,
                        error_category="automation_inactive",
                        summary={"reason": "automation is not active"},
                    )
                authority = DelegatedAuthority.model_validate(claimed.authority_snapshot)
                allowed = effective_delegated_capabilities(
                    authority,
                    settings=self._settings,
                    registry=self._registry,
                )
                if not set(claimed.required_capabilities).issubset(allowed):
                    return ExecutionResult(
                        status=RunStatus.BLOCKED,
                        error_category="delegated_authority_revoked",
                        summary={"reason": "required capability is no longer delegated"},
                    )
                permission = permission_for(self._settings, claimed.creator_user_id)
                return _ExecutionSnapshot(
                    record=claimed,
                    complete_v2=False,
                    allowed=allowed,
                    actor_is_superuser=permission is PermissionLevel.SUPERUSER,
                )
            current = await self._repository.get(claimed.id, session=session)
            if current is None:
                return ExecutionResult(
                    status=RunStatus.BLOCKED,
                    error_category="automation_inactive",
                    summary={"reason": "automation row is missing"},
                )
            if _canonical_identity(current) != _canonical_identity(claimed):
                return ExecutionResult(
                    status=RunStatus.BLOCKED,
                    error_category="state_mismatch",
                    summary={"reason": "claimed canonical identity is stale"},
                )
            if current.status is not AutomationStatus.ACTIVE:
                return ExecutionResult(
                    status=RunStatus.BLOCKED,
                    error_category="automation_inactive",
                    summary={"reason": "automation is not active"},
                )
            blocked = await self._validate_v2_identity(session, current)
            if blocked is not None:
                return blocked
            loaded = await self._v2_creator_principal(session, current)
            if isinstance(loaded, ExecutionResult):
                return loaded
            principal, current_permission = loaded
            if not principal.authenticated or not principal.active:
                return ExecutionResult(
                    status=RunStatus.BLOCKED,
                    error_category="delegated_authority_revoked",
                    summary={"reason": "control principal is no longer active"},
                )
            authority = DelegatedAuthority.model_validate(current.authority_snapshot)
            allowed = effective_delegated_capabilities(
                authority,
                settings=self._settings,
                registry=self._registry,
                current_permission=current_permission,
            )
            if not set(current.required_capabilities).issubset(allowed):
                return ExecutionResult(
                    status=RunStatus.BLOCKED,
                    error_category="delegated_authority_revoked",
                    summary={"reason": "required capability is no longer delegated"},
                )
            return _ExecutionSnapshot(
                record=current,
                complete_v2=True,
                allowed=allowed,
                actor_is_superuser=current_permission is PermissionLevel.SUPERUSER,
            )

    async def _validate_v2_identity(
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
                summary={"reason": "complete-v2 automation has no canonical target"},
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

    async def _v2_creator_principal(
        self, session: Any, automation: AutomationRecord
    ) -> tuple[ControlPrincipal, PermissionLevel] | ExecutionResult:
        creator_id = automation.canonical_creator_person_id
        if not creator_id:
            return ExecutionResult(
                status=RunStatus.BLOCKED,
                error_category="target_missing",
                summary={"reason": "complete-v2 automation has no canonical creator"},
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
        current_permission = permission_for_accounts(
            self._settings, (str(item) for item in accounts)
        )
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

    async def _revalidate_v2_send(self, automation: AutomationRecord) -> ExecutionResult | None:
        needs_send = bool(_SEND_CAPABILITIES.intersection(automation.required_capabilities))
        if not needs_send:
            return None
        if self._router is None:
            return ExecutionResult(
                status=RunStatus.BLOCKED,
                error_category="operation_unavailable",
                summary={"reason": "complete-v2 send requires PresenceRouter"},
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
        attempts = 2 if definition.retry_policy is RetryPolicy.TRANSIENT_ONCE else 1
        for attempt in range(attempts):
            try:
                return await definition.handler(arguments, context)
            except ProactiveGatewayError as exc:
                raise AutomationExecutionError(exc.category, uncertain=exc.uncertain) from exc
            except AutomationExecutionError as exc:
                if not exc.transient or attempt + 1 >= attempts:
                    raise
            except Exception as exc:
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
        if llm_calls > limits.max_llm_calls:
            raise AutomationExecutionError("llm_limit_exceeded")
        if tool_calls > limits.max_tool_calls:
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
