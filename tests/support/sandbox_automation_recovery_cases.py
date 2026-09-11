"""Recover delegated tasks using real automation, task and identity records."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from qq_ai_bot.automation.executor import AutomationExecutionError, AutomationExecutor
from qq_ai_bot.automation.models import AutomationScript
from qq_ai_bot.automation.registry import build_capability_registry
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.identity.db_models import CanonicalPersonModel
from qq_ai_bot.persistence.models import AutomationModel, AutomationRunModel
from qq_ai_bot.sandbox.automation_recovery import recover_automation_source
from qq_ai_bot.time.service import TimeContextService
from tests.conftest import make_settings


async def automation_recovery_cases(database, tasks):
    registry = build_capability_registry()
    settings = make_settings(database.url, automation_enabled=True)
    repository = AutomationRepository(database)
    time_service = TimeContextService(database)
    service = AutomationService(
        settings=settings, registry=registry, repository=repository, time_service=time_service
    )
    script = AutomationScript.model_validate(
        {
            "version": 1,
            "name": "图片任务",
            "timezone": "Asia/Shanghai",
            "schedule": {"type": "after", "seconds": 1},
            "context": {"scene": "none"},
            "steps": [
                {
                    "id": "work",
                    "call": "yuki.agent",
                    "arguments": {
                        "instruction": "下载图片",
                        "context_profile": "none",
                        "allowed_capabilities": ["sandbox.run_python"],
                        "max_tool_calls": 3,
                        "max_model_requests": 3,
                    },
                }
            ],
            "limits": {"max_steps": 1, "max_llm_calls": 3, "max_tool_calls": 6, "max_messages": 1},
        }
    )
    automation = await service.create(
        script,
        inbound=InboundMessage(
            message_id="create-task",
            event_type="private",
            scope_type=ScopeType.PRIVATE,
            sender=SenderIdentity("10001"),
            bot_user_id="80001",
            text="下载图片",
        ),
        conversation_key="private:80001:10001",
    )
    run = await repository.create_run(
        automation.id, scheduled_for=automation.next_run_at, actual_started_at=datetime.now(UTC)
    )
    async with database.sessions() as session:
        conversation = await session.scalar(
            select(CanonicalConversationModel).where(
                CanonicalConversationModel.person_id == automation.canonical_target_person_id
            )
        )
    source = {
        "origin": "scheduled_automation",
        "conversation_id": conversation.id,
        "generation": conversation.generation,
        "actor_user_id": automation.creator_user_id,
        "bot_user_id": automation.bot_user_id,
        "automation_id": automation.id,
        "automation_run_id": run.id,
        "script_hash": automation.script_hash,
        "source_step_id": "work",
        "trigger_id": "agent:call",
        "instruction": "下载图片",
        "context_profile": "none",
        "automation_context": script.context.model_dump(mode="json"),
        "delegated_authority": automation.authority_snapshot,
        "allowed_capabilities": ["sandbox.run_python"],
        "target_person_id": automation.canonical_target_person_id,
        "target_space_id": None,
    }
    await tasks.prepare("scheduled", {"code": "pass"}, source)
    run_id = str(uuid4())
    await tasks.receive(
        {
            "request_id": "scheduled",
            "run_id": run_id,
            "result": {
                "run_id": run_id,
                "status": "succeeded",
                "pending": False,
            },
        }
    )
    executor = AutomationExecutor(
        settings=settings, registry=registry, repository=repository, time_service=time_service
    )
    with pytest.raises(AutomationExecutionError, match="task_source_busy"):
        await recover_automation_source(executor, "scheduled")
    async with database.sessions() as session, session.begin():
        record = await session.get(AutomationRunModel, run.id)
        record.status = "succeeded"
        record.llm_calls, record.tool_calls, record.messages_sent = 2, 3, 1
        (await session.get(AutomationModel, automation.id)).status = "completed"
    recovered = await recover_automation_source(executor, "scheduled")
    assert recovered.instruction == "下载图片"
    assert recovered.context.actual_started_at.tzinfo is not None
    assert recovered.context.authority.allowed_capabilities == frozenset({"sandbox.run_python"})
    assert (
        recovered.prior_model_calls,
        recovered.prior_tool_calls,
        recovered.prior_messages_sent,
    ) == (2, 3, 1)
    with pytest.raises(AutomationExecutionError, match="capability_not_delegated"):
        await recovered.context.revalidate_authority("social.send_private_message")
    for status in ("paused", "cancelled", "blocked"):
        async with database.sessions() as session, session.begin():
            (await session.get(AutomationModel, automation.id)).status = status
        with pytest.raises(AutomationExecutionError, match="automation_inactive"):
            await recovered.context.revalidate_authority("sandbox.run_python")
    async with database.sessions() as session, session.begin():
        record = await session.get(AutomationModel, automation.id)
        record.status, record.script_hash = "completed", "b" * 64
    with pytest.raises(AutomationExecutionError, match="automation_changed"):
        await recover_automation_source(executor, "scheduled")
    async with database.sessions() as session, session.begin():
        (await session.get(AutomationModel, automation.id)).script_hash = automation.script_hash
        (
            await session.get(CanonicalPersonModel, automation.canonical_creator_person_id)
        ).enabled = False
    with pytest.raises(
        AutomationExecutionError, match=r"target_disabled|delegated_authority_revoked"
    ):
        await recovered.context.revalidate_authority(None)
    async with database.sessions() as session, session.begin():
        (
            await session.get(CanonicalPersonModel, automation.canonical_creator_person_id)
        ).enabled = True
        (await session.get(AutomationRunModel, run.id)).status = "uncertain"
    with pytest.raises(AutomationExecutionError, match="task_source_not_resumable"):
        await recover_automation_source(executor, "scheduled")
    async with database.sessions() as session, session.begin():
        (await session.get(AutomationRunModel, run.id)).status = "succeeded"
        (await session.get(CanonicalConversationModel, conversation.id)).generation += 1
    with pytest.raises(AutomationExecutionError, match="task_conversation_changed"):
        await recover_automation_source(executor, "scheduled")
