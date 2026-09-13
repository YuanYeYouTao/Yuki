"""Current canonical state gates for recovering a completed message task."""

from uuid import uuid4

import pytest
from sqlalchemy import select

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    PresenceModel,
    SpaceBindingModel,
)
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.sandbox.source_recovery import recover_message_source


async def message_source_cases(database, tasks, source, completion):
    recovered = await recover_message_source(database, "request")
    assert recovered.content == "hello"
    assert recovered.actor_user_id == "10001"
    assert recovered.bot_user_id == "80001"
    assert recovered.external_target_id == "20001"
    for model, identity, field, disabled, expected in (
        (CanonicalPersonModel, recovered.actor_person_id, "enabled", False, "task_actor_disabled"),
        (CanonicalSpaceModel, recovered.target_space_id, "enabled", False, "task_space_disabled"),
        (PresenceModel, recovered.presence_id, "enabled", False, "task_presence_disabled"),
        (
            SpaceBindingModel,
            recovered.binding_id,
            "status",
            "disabled",
            "task_space_binding_unavailable",
        ),
        (
            CanonicalConversationModel,
            recovered.conversation_id,
            "generation",
            2,
            "task_conversation_changed",
        ),
        (
            CanonicalConversationModel,
            recovered.conversation_id,
            "starts_after_event_id",
            recovered.event_id,
            "task_source_event_unavailable",
        ),
    ):
        async with database.sessions() as session, session.begin():
            row = await session.get(model, identity)
            previous = getattr(row, field)
            setattr(row, field, disabled)
            if field == "starts_after_event_id":
                covered = row.covered_through_event_id
                row.covered_through_event_id = max(covered, disabled)
        try:
            with pytest.raises(ValueError, match=expected):
                await recover_message_source(database, "request")
        finally:
            async with database.sessions() as session, session.begin():
                row = await session.get(model, identity)
                setattr(row, field, previous)
                if field == "starts_after_event_id":
                    row.covered_through_event_id = covered
    # A canonical conversation alone cannot authorize a different event actor or
    # a different bot account. Replayed metadata never becomes a fresh message.
    for change, expected in (
        ({"actor_user_id": "10002"}, "task_source_event_unavailable"),
        ({"presence_id": str(uuid4())}, "task_source_presence_changed"),
        ({"generation": None}, "task_conversation_changed"),
        (
            {
                "origin": "scheduled_automation",
                "delegated_authority": {"reference": "test"},
                "automation_run_id": 1,
                "step_id": "test-step",
            },
            "not_a_message_task",
        ),
    ):
        request_id, run_id = str(uuid4()), str(uuid4())
        await tasks.prepare(request_id, {"code": "pass"}, {**source, **change})
        await tasks.receive(
            {
                "request_id": request_id,
                "run_id": run_id,
                "result": {**completion["result"], "run_id": run_id},
            }
        )
        with pytest.raises(ValueError, match=expected):
            await recover_message_source(database, request_id)
    writer = ScopedEventLedgerUnitOfWork(database, config=RollupPolicyConfig())
    await writer.append(
        scope=ConversationScope.private("80001", "10001"),
        platform_message_id="private-task",
        sender_user_id="10001",
        direction="inbound",
        content="下载后把图片发过来",
    )
    async with database.sessions() as session:
        private_event = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.platform_message_id == "private-task")
        )
    private_source = {
        **source,
        "conversation_id": private_event.canonical_conversation_id,
        "trigger_id": "private-task",
        "trigger_event_id": private_event.id,
    }
    await tasks.prepare("private", {"code": "pass"}, private_source)
    private_run = str(uuid4())
    await tasks.receive(
        {
            "request_id": "private",
            "run_id": private_run,
            "result": {**completion["result"], "run_id": private_run},
        }
    )
    recovered_private = await recover_message_source(database, "private")
    assert recovered_private.target_person_id == recovered.actor_person_id
    assert recovered_private.target_space_id is None
    assert recovered_private.external_target_id == "10001"
    assert recovered_private.content == "下载后把图片发过来"
    await tasks.prepare("cancelled", {"code": "pass"}, private_source)
    cancelled_run = str(uuid4())
    await tasks.receive(
        {
            "request_id": "cancelled",
            "run_id": cancelled_run,
            "result": {"status": "cancelled", "pending": False, "run_id": cancelled_run},
        }
    )
    with pytest.raises(ValueError, match="task_cancelled"):
        await recover_message_source(database, "cancelled")
