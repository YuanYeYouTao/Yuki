"""SELF ownership and one-shot Work signals retain the original execution."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from tests.conftest import make_settings
from tests.support.social_identity_cases import social_env
from tests.unit.test_automation_runtime import FakeClock, _script
from tests.unit.test_plugin_notification_idempotency import _PLUGIN_ID, _install, _request
from tests.unit.test_self_initiative_runtime import self_source

from qq_ai_bot.automation.models import AutomationScript
from qq_ai_bot.automation.registry import build_capability_registry
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.plugin_host.db_models import PluginBackgroundTurnJobModel
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import inputs
from qq_ai_bot.runtime.work_wait import WorkWaitRepository
from qq_ai_bot.runtime.work_wait_schema import waits
from qq_ai_bot.social.db_models import SocialOperationModel
from qq_ai_bot.time.service import TimeContextService


@pytest.mark.asyncio
async def test_self_uses_existing_automation_owner_and_cannot_be_borrowed(database):
    source, _repository, _binding = await self_source(database)
    actor = ToolActor(
        user_id="",
        bot_user_id=source["bot_user_id"],
        group_id=source["group_id"],
        origin=TurnOrigin.SELF_INITIATIVE,
        instruction="明天整理群工作区",
        execution_id="work",
        conversation_id=source["conversation_id"],
        presence_id=source["presence_id"],
        principal_kind="self",
        initiative_run_id=source["initiative_run_id"],
    )
    raw = _script().model_dump(mode="json")
    raw["name"] = "SELF 定时整理"
    raw["context"] = {"scene": "current_group"}
    raw["steps"] = [
        {
            "id": "work",
            "call": "yuki.agent",
            "arguments": {
                "instruction": "整理当前群工作区；有需要时自己决定是否发言",
                "context_profile": "current_group",
            },
        }
    ]
    raw["limits"].update(max_llm_calls=2, max_tool_calls=4, agent_budget_managed=True)
    script = AutomationScript.model_validate(raw)
    service = AutomationService(
        settings=make_settings(database.url, automation_enabled=True, runtime_work_enabled=True),
        repository=AutomationRepository(database),
        registry=build_capability_registry(),
        time_service=TimeContextService(
            database, clock=FakeClock(datetime(2026, 9, 24, tzinfo=UTC))
        ),
    )
    created = await service.create(script, actor=actor, conversation_key="group:2001")
    repeated = await service.create(script, actor=actor, conversation_key="group:2001")
    assert repeated.id == created.id
    assert created.creator_kind == "self"
    assert created.canonical_creator_person_id is None
    assert created.canonical_target_space_id == source["space_id"]
    assert created.authority_snapshot["canonical_conversation_id"] == source["conversation_id"]
    assert [row.id for row in await service.list_completed("")] == []
    assert (await service.require_manageable(created.id, actor)).id == created.id


@pytest.mark.asyncio
async def test_wait_all_partial_then_message_resumes_original_work_once(database, tmp_path):
    env = await social_env(database, tmp_path)
    original = WorkRepository(database)
    waiting = WorkWaitRepository(original)
    lease = await original.acquire(env.context.conversation_id, 1)
    assert lease is not None
    source = {
        "origin": "user_message",
        "principal_kind": "person",
        "actor_user_id": "10001",
        "actor_person_id": env.person,
    }
    item = await original.accept(lease, source_key="original", source=source, goal="继续原任务")
    await original.checkpoint(lease, item["id"], {"brief": "do not reset"}, models=2)
    binding = await waiting.register(
        lease,
        work_id=item["id"],
        source=source,
        call_key="wait-original",
        mode="all",
        conditions=[
            {"kind": "time_due", "at": "2000-01-01T00:00:00+00:00"},
            {"kind": "conversation"},
        ],
        deadline_at=None,
    )
    assert (
        await waiting.register(
            lease,
            work_id=item["id"],
            source=source,
            call_key="wait-original",
            mode="all",
            conditions=[
                {"kind": "time_due", "at": "2000-01-01T00:00:00+00:00"},
                {"kind": "conversation"},
            ],
            deadline_at=None,
        )
    )["id"] == binding["id"]
    with pytest.raises(WorkConflict, match="wait_call_conflict"):
        await waiting.register(
            lease,
            work_id=item["id"],
            source=source,
            call_key="wait-original",
            mode="any",
            conditions=[{"kind": "conversation"}],
            deadline_at=None,
        )
    await original.transition(lease, item["id"], item["revision"], "waiting_external")
    assert await waiting.deliver_due() == 0
    await env.service.writer.append(
        scope=ConversationScope.group("80001", "20001"),
        platform_message_id="second-real-message",
        sender_user_id="10001",
        direction="inbound",
        content="可以继续了",
    )
    from qq_ai_bot.persistence.models import ChatEventModel

    async with database.sessions() as session:
        event_id = await session.scalar(select(func.max(ChatEventModel.id)))
    input_id = await waiting.match_event(event_id=event_id, kind="conversation")
    assert input_id and input_id > 0
    assert await waiting.match_event(event_id=event_id, kind="conversation") is None
    resumed = await original.get(item["id"])
    assert resumed["state"] == "queued"
    assert resumed["model_requests"] == 2
    assert json.loads(resumed["checkpoint_json"]) == {"brief": "do not reset"}
    async with database.sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(inputs)
                .where(
                    inputs.c.work_id == item["id"], inputs.c.source_key == f"wait:{binding['id']}"
                )
            )
            == 1
        )
        assert (
            await session.scalar(select(waits.c.status).where(waits.c.id == binding["id"]))
            == "delivered"
        )


@pytest.mark.asyncio
async def test_waiting_user_requires_reply_to_the_original_works_question(database, tmp_path):
    env = await social_env(database, tmp_path)
    from qq_ai_bot.persistence.models import ChatEventModel

    async with database.sessions() as session:
        first_id = await session.scalar(select(func.min(ChatEventModel.id)))
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease is not None
    source = {
        "origin": "user_message",
        "principal_kind": "person",
        "actor_user_id": "10001",
        "actor_person_id": env.person,
        "trigger_event_id": first_id,
    }
    item = await repository.accept(lease, source_key="question", source=source, goal="等用户回答")
    question_context = replace(
        env.context,
        turn_id=f"{env.context.conversation_id}:event:{first_id}",
        call_id="question-call",
    )
    await env.service.execute("send_message", {"text": "请确认日期"}, question_context)
    await repository.transition(lease, item["id"], item["revision"], "waiting_user")
    async with database.sessions() as session:
        question_event_id = await session.scalar(
            select(SocialOperationModel.event_id).where(
                SocialOperationModel.source_turn_id == question_context.turn_id,
                SocialOperationModel.status == "succeeded",
            )
        )
    unrelated = await env.service.writer.append(
        scope=ConversationScope.group("80001", "20001"),
        platform_message_id="unrelated",
        sender_user_id="10001",
        direction="inbound",
        content="另一个话题",
    )
    assert await WorkWaitRepository(repository).match_user_reply(unrelated.event.id) is None
    reply = await env.service.writer.append(
        scope=ConversationScope.group("80001", "20001"),
        platform_message_id="answer",
        sender_user_id="10001",
        direction="inbound",
        content="明天",
        reply_to_event_id=question_event_id,
    )
    input_id = await WorkWaitRepository(repository).match_user_reply(reply.event.id)
    assert input_id is not None
    await repository.prepare_input(input_id, {"text": "明天"})
    resumed = await repository.get(item["id"])
    assert resumed["state"] == "queued"


@pytest.mark.asyncio
async def test_opted_in_plugin_event_resumes_work_instead_of_starting_another_agent(database):
    writer = ScopedEventLedgerUnitOfWork(database, config=RollupPolicyConfig())
    first = await writer.append(
        scope=ConversationScope.private("8000", "1001"),
        platform_message_id="plugin-wait-start",
        sender_user_id="1001",
        direction="inbound",
        content="等插件信号",
    )
    async with database.sessions() as session:
        from qq_ai_bot.persistence.models import ChatEventModel

        event = await session.get(ChatEventModel, first.event.id)
        conversation_id, person_id = event.canonical_conversation_id, event.author_person_id
    repository = WorkRepository(database)
    lease = await repository.acquire(conversation_id, 1)
    assert lease is not None
    source = {
        "origin": "user_message",
        "principal_kind": "person",
        "actor_user_id": "1001",
        "actor_person_id": person_id,
        "trigger_event_id": first.event.id,
    }
    item = await repository.accept(lease, source_key="plugin-wait", source=source, goal="等状态")
    notifications = await _install(database)
    binding = await WorkWaitRepository(repository).register(
        lease,
        work_id=item["id"],
        source=source,
        call_key="plugin-signal-call",
        mode="any",
        conditions=[
            {
                "kind": "plugin_event",
                "plugin_id": _PLUGIN_ID,
                "event_type": "fixture.created",
                "external_source": "fixture",
            }
        ],
        deadline_at=None,
    )
    await repository.transition(lease, item["id"], item["revision"], "waiting_external")
    receipt = await notifications.publish(
        plugin_id=_PLUGIN_ID, request=_request(resume_waiting_work=True)
    )
    assert receipt.event_created and not receipt.agent_turn_enqueued
    assert (await repository.get(item["id"]))["state"] == "queued"
    async with database.sessions() as session:
        job = await session.scalar(
            select(PluginBackgroundTurnJobModel).where(
                PluginBackgroundTurnJobModel.source_event_id == receipt.source_event_id
            )
        )
        assert job.status == "cancelled"
        assert (
            await session.scalar(select(waits.c.status).where(waits.c.id == binding["id"]))
            == "delivered"
        )
    duplicate = await notifications.publish(
        plugin_id=_PLUGIN_ID, request=_request(resume_waiting_work=True)
    )
    assert duplicate.deduplicated
