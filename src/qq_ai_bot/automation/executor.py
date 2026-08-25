"""Sequential DSL executor with hard limits, audit, and uncertain-send handling."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Any

from qq_ai_bot.automation.authority import (
    AuthorityContext,
    DelegatedAuthority,
    PermissionLevel,
    effective_delegated_capabilities,
    permission_for,
)
from qq_ai_bot.automation.gateway import ProactiveGatewayError
from qq_ai_bot.automation.models import (
    AutomationRecord,
    AutomationRunRecord,
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
from qq_ai_bot.domain.identity import PersonId, PrincipalId
from qq_ai_bot.time.service import TimeContextService

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._repository = repository
        self._time = time_service
        self._gateway_factory = gateway_factory

    async def execute(
        self,
        automation: AutomationRecord,
        run: AutomationRunRecord,
        *,
        current_group_id: str | None = None,
    ) -> ExecutionResult:
        authority = DelegatedAuthority.model_validate(automation.authority_snapshot)
        if current_group_id is None:
            current_group_id = authority.current_group_id
        allowed = effective_delegated_capabilities(
            authority,
            settings=self._settings,
            registry=self._registry,
        )
        if not set(automation.required_capabilities).issubset(allowed):
            return ExecutionResult(
                status=RunStatus.BLOCKED,
                error_category="delegated_authority_revoked",
                summary={"reason": "required capability is no longer delegated"},
            )
        creator_person = None
        if automation.canonical_creator_person_id:
            creator_person = PersonId.parse(automation.canonical_creator_person_id)
        principal = ControlPrincipal(
            principal_id=PrincipalId.new(),
            person_id=creator_person,
            source=PrincipalSource.QQ if creator_person is not None else PrincipalSource.SYSTEM,
            roles=(
                ("superuser",)
                if permission_for(self._settings, automation.creator_user_id)
                is PermissionLevel.SUPERUSER
                else ("user",)
            ),
            granted_capabilities=(),
            authenticated=True,
            active=True,
        )
        if not principal.authenticated or not principal.active:
            return ExecutionResult(
                status=RunStatus.BLOCKED,
                error_category="delegated_authority_revoked",
                summary={"reason": "control principal is no longer active"},
            )
        local = self._time.at(run.actual_started_at, automation.timezone)
        authority_context = AuthorityContext(
            origin=TurnOrigin.SCHEDULED_AUTOMATION,
            actor_user_id=automation.creator_user_id,
            actor_is_superuser=(automation.creator_user_id in self._settings.superusers),
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
                        conversation_key=f"automation:{automation.id}",
                        web_was_used=web_was_used,
                        canonical_target_person_id=automation.canonical_target_person_id,
                        canonical_target_space_id=automation.canonical_target_space_id,
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
