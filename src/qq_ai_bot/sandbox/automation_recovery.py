"""Recover a particular delegated Agent task, never a replacement automation script."""

from __future__ import annotations

import json
from dataclasses import dataclass

from qq_ai_bot.automation.authority import AuthorityContext, DelegatedAuthority
from qq_ai_bot.automation.context import bind_automation_conversation
from qq_ai_bot.automation.executor import AutomationExecutionError, AutomationExecutor
from qq_ai_bot.automation.models import (
    AutomationContext,
    AutomationRecord,
    ExecutionResult,
    TurnOrigin,
)
from qq_ai_bot.automation.registry import CapabilityExecutionContext
from qq_ai_bot.automation.repository import _run_record
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.persistence.models import AutomationRunModel
from qq_ai_bot.sandbox.db_models import SandboxTaskRunModel


@dataclass(frozen=True)
class AutomationTaskSource:
    context: CapabilityExecutionContext
    instruction: str
    context_profile: str
    automation: AutomationRecord
    prior_model_calls: int
    prior_tool_calls: int
    prior_messages_sent: int


async def recover_automation_source(
    executor: AutomationExecutor, request_id: str
) -> AutomationTaskSource:
    database = executor._repository._database
    async with database.sessions() as session:
        task = await session.get(SandboxTaskRunModel, request_id)
        if task is None or task.status != "completed" or not task.completion_json:
            raise AutomationExecutionError("task_not_completed")
        if json.loads(task.completion_json).get("status") == "cancelled":
            raise AutomationExecutionError("task_cancelled")
        source = json.loads(task.source_json)
        if source.get("origin") != TurnOrigin.SCHEDULED_AUTOMATION.value:
            raise AutomationExecutionError("not_an_automation_task")
        automation_id, run_id = source.get("automation_id"), source.get("automation_run_id")
        if type(automation_id) is not int or type(run_id) is not int:
            raise AutomationExecutionError("invalid_automation_task_anchor")
        automation = await executor._repository.get(automation_id, session=session)
        run = await session.get(AutomationRunModel, run_id)
        if automation is None or run is None or run.automation_id != automation.id:
            raise AutomationExecutionError("automation_task_source_unavailable")
        if run.status == "running":
            raise AutomationExecutionError("task_source_busy")
        if run.status not in {"succeeded", "failed"}:
            raise AutomationExecutionError("task_source_not_resumable")
        run_record = _run_record(run)
        if (
            source.get("script_hash") != automation.script_hash
            or source.get("actor_user_id") != automation.creator_user_id
            or source.get("bot_user_id") != automation.bot_user_id
            or source.get("target_person_id") != automation.canonical_target_person_id
            or source.get("target_space_id") != automation.canonical_target_space_id
        ):
            raise AutomationExecutionError("automation_changed")
        grant = DelegatedAuthority.model_validate(source.get("delegated_authority"))
        if grant != DelegatedAuthority.model_validate(automation.authority_snapshot):
            raise AutomationExecutionError("delegated_authority_revoked")
        step = next(
            (item for item in automation.script.steps if item.id == source.get("source_step_id")),
            None,
        )
        if step is None or step.call != "yuki.agent":
            raise AutomationExecutionError("task_source_not_agent")
        instruction, profile = source.get("instruction"), source.get("context_profile")
        if not isinstance(instruction, str) or not instruction or len(instruction) > 4000:
            raise AutomationExecutionError("task_instruction_unavailable")
        if profile not in {"none", "creator_private", "current_group"}:
            raise AutomationExecutionError("invalid_task_context_profile")
        declared_context = AutomationContext.model_validate(source.get("automation_context"))
        if declared_context != automation.script.context or (
            profile != "none" and profile != declared_context.scene
        ):
            raise AutomationExecutionError("task_context_changed")
        key, conversation_id = await bind_automation_conversation(session, automation)
        conversation = await session.get(CanonicalConversationModel, task.source_conversation_id)
        generation = source.get("generation")
        if (
            conversation is None
            or conversation_id != task.source_conversation_id
            or type(generation) is not int
            or conversation.generation != generation
        ):
            raise AutomationExecutionError("task_conversation_changed")
    # Completed schedules may still own unfinished work from this exact terminal
    # run. Paused/cancelled/blocked schedules and live permission revocations fail.
    validated = await executor._begin_execution(automation, allow_completed=True)
    if isinstance(validated, ExecutionResult):
        raise AutomationExecutionError(validated.error_category or "delegated_authority_revoked")
    if (
        validated.record.script_hash != automation.script_hash
        or validated.record.authority_snapshot != automation.authority_snapshot
    ):
        raise AutomationExecutionError("automation_changed")
    selected = source.get("allowed_capabilities")
    if not isinstance(selected, list) or any(not isinstance(name, str) for name in selected):
        raise AutomationExecutionError("invalid_task_capabilities")
    allowed = validated.allowed.intersection(selected)

    async def revalidate(capability: str | None) -> None:
        current = await recover_automation_source(executor, request_id)
        if capability is not None and (
            capability not in allowed
            or capability not in current.context.authority.allowed_capabilities
        ):
            raise AutomationExecutionError("capability_not_delegated")

    local = executor._time.at(run_record.actual_started_at, automation.timezone)
    return AutomationTaskSource(
        context=CapabilityExecutionContext(
            authority=AuthorityContext(
                origin=TurnOrigin.SCHEDULED_AUTOMATION,
                actor_user_id=automation.creator_user_id,
                actor_is_superuser=validated.actor_is_superuser,
                bot_user_id=automation.bot_user_id,
                delegated_authority=grant,
                allowed_capabilities=allowed,
            ),
            automation_id=automation.id,
            automation_run_id=run.id,
            step_id=source["trigger_id"],
            creator_user_id=automation.creator_user_id,
            bot_user_id=automation.bot_user_id,
            current_group_id=grant.current_group_id,
            scheduled_for=run_record.scheduled_for,
            actual_started_at=run_record.actual_started_at,
            local_time=local.local,
            timezone=automation.timezone,
            automation_context=declared_context,
            conversation_key=key,
            canonical_target_person_id=automation.canonical_target_person_id,
            canonical_target_space_id=automation.canonical_target_space_id,
            canonical_conversation_id=conversation_id,
            conversation_generation=generation,
            automation_script_hash=automation.script_hash,
            source_step_id=step.id,
            agent_instruction=instruction,
            agent_context_profile=profile,
            revalidate_authority=revalidate,
        ),
        instruction=instruction,
        context_profile=profile,
        automation=automation,
        prior_model_calls=run.llm_calls,
        prior_tool_calls=run.tool_calls,
        prior_messages_sent=run.messages_sent,
    )
