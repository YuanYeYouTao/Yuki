"""Frozen legacy task publication preserves the original private execution facts."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import insert, select, text, update
from tests.conftest import build_harness, make_settings
from tests.support.runtime_execution import make_work_resumer
from tests.support.social_identity_cases import social_env

from qq_ai_bot.automation.models import AutomationStatus, RunStatus
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.automation.task_spec import TaskSpec, canonical_task_hash
from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatMessage, ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelSearchMode,
    ModelTask,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import AutomationModel, AutomationVersionModel
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.runtime.protocol_store import ProtocolStore
from qq_ai_bot.runtime.work_budget_schema import budgets
from qq_ai_bot.runtime.work_recovery_schema import invocations
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_scheduler import WorkScheduler
from qq_ai_bot.runtime.work_schema_v1 import effects, journal, work
from qq_ai_bot.services.execution_sources import recover_automation_source
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.time.service import TimeContextService
from qq_ai_bot.web.models import WebMode
from qq_ai_bot.workspace.short_state import ShortState


def bind_native_model(chat, provider):
    profile = ModelProfile(
        id="original-native-model",
        provider="anthropic",
        protocol=ModelProtocol.ANTHROPIC_MESSAGES,
        base_url="https://wire.invalid/v1",
        api_key_env="UNUSED",
        model="synthetic",
        timeout_seconds=1,
        max_retries=0,
        default_max_output_tokens=8192,
        default_temperature=0.5,
        search_mode=ModelSearchMode.NATIVE,
        capabilities=frozenset(
            {ModelCapability.TOOLS, ModelCapability.NATIVE_WEB_SEARCH, ModelCapability.REASONING}
        ),
    )
    executor = TaskModelExecutor(
        router=ModelRouter(
            ModelProfileCatalog(
                profiles={profile.id: profile},
                routes={task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask},
            )
        ),
        pool=ModelClientPool(injected_profiles={profile.id: provider}),
    )
    chat._models = executor
    chat.runtime.runner._models = executor


@pytest.mark.parametrize(
    "read_scope", ["current_group", "creator_private", "creator_private_pause"]
)
def test_legacy_publication_keeps_hash_receipts_and_resumes_original_main_work(
    tmp_path, monkeypatch, read_scope
):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'legacy.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0109")
    reminder = "完整旧提醒：" + "文字资料" * 1800 + "ORIGINAL_REMINDER_TAIL"
    now = datetime.now(UTC)
    goal = "原始高层目标" + "完整要求" * 1500
    instruction = json.dumps(
        {"goal": goal, "constraints": ["完整约束尾部"], "delivery": "none"}, ensure_ascii=False
    )
    json_instruction = json.dumps(
        {
            "goal": "完整 JSON 指令",
            "constraints": ["保留正文中的全部字段"],
            "delivery": {"target": "none"},
            "additional_requirement": "必须保留的附加要求",
        },
        ensure_ascii=False,
    )
    private_text = "ORIGINAL_PRIVATE_READSET_ONLY"
    private_read = read_scope.startswith("creator_private")
    paused_read = read_scope == "creator_private_pause"

    async def seed():
        database = Database(url)
        client = None
        try:
            env = await social_env(database, tmp_path)
            settings = make_settings(
                url,
                automation_enabled=True,
                runtime_work_enabled=True,
                enabled_groups_csv="20001",
                agent_max_model_requests=2,
                **({"web_enabled": True, "web_mode": WebMode.NATIVE} if paused_read else {}),
            )
            repo = AutomationRepository(database)
            service = AutomationService(
                settings=settings, repository=repo, time_service=TimeContextService(database)
            )
            records = []
            scripts = []
            for index, step in enumerate(
                (
                    {
                        "id": "original_agent",
                        "call": "yuki.agent",
                        "arguments": {
                            "instruction": instruction,
                            "delivery_target": "none",
                            "context_profile": "creator_private",
                        },
                    },
                    {
                        "id": "literal_reminder",
                        "call": "social.send_message",
                        "arguments": {"text": reminder},
                    },
                    {
                        "id": "unknown_plugin",
                        "call": "plugin.original_action",
                        "arguments": {"text": "${prior.output}"},
                    },
                    {
                        "id": "literal_json_instruction",
                        "call": "yuki.agent",
                        "arguments": {"instruction": json_instruction, "delivery_target": "none"},
                    },
                    {
                        "id": "opaque_template",
                        "call": "yuki.agent",
                        "arguments": {"instruction": "${missing.output}"},
                    },
                    {
                        "id": "send_with_extra_effect",
                        "call": "social.send_message",
                        "arguments": {"text": "提醒", "mentions": ["current_speaker"]},
                    },
                )
            ):
                row = await service.administer_create(
                    {
                        "name": f"original-{index}",
                        "goal": "seed",
                        "trigger": {"type": "interval", "seconds": 60},
                        "delivery": {"target": "current_group"},
                    },
                    owner_id=env.person,
                    conversation_id=env.context.conversation_id,
                    creation_source_key=f"original:{index}",
                )
                script = {
                    "version": 1,
                    "name": row.name,
                    "timezone": "UTC",
                    "schedule": {"type": "daily", "hour": 9, "minute": 0, "timezone": None}
                    if index == 1
                    else {"type": "interval", "seconds": 60},
                    "context": {"scene": "creator_private", "history_limit": 1},
                    "steps": [step],
                    "limits": {"max_steps": 1, "max_llm_calls": 2, "timeout_seconds": 600},
                }
                raw = json.dumps(script, ensure_ascii=False)
                saved_hash = f"original-frozen-hash-{index}"
                async with database.immediate_session() as writer:
                    version = await writer.get(AutomationVersionModel, row.version_id)
                    authority = json.loads(version.authority_snapshot_json)
                    authority["granted_capabilities"] = [step["call"]]
                    authority["capability_schema_versions"] = {step["call"]: 1}
                    values = dict(
                        script_json=raw,
                        script_hash=saved_hash,
                        authority_snapshot_json=json.dumps(authority),
                        required_capabilities_json=json.dumps([step["call"]]),
                        timezone="UTC",
                        schedule_json=json.dumps(script["schedule"]),
                    )
                    await writer.execute(
                        update(AutomationModel).where(AutomationModel.id == row.id).values(**values)
                    )
                    await writer.execute(
                        update(AutomationVersionModel)
                        .where(AutomationVersionModel.id == row.version_id)
                        .values(
                            script_json=raw,
                            script_hash=saved_hash,
                            authority_snapshot_json=json.dumps(authority),
                        )
                    )
                records.append(await repo.get(row.id))
                scripts.append(raw)
            original = records[0]
            run = await repo.create_run(
                original.id, scheduled_for=original.next_run_at, actual_started_at=now
            )
            source_key = f"automation:{run.id}:original_agent:{original.script_hash}"
            source = {
                "owner": "automation",
                "origin": "scheduled_automation",
                "automation_id": original.id,
                "automation_run_id": run.id,
                "step_id": "original_agent",
                "parent_execution_id": source_key,
                "conversation_id": env.context.conversation_id,
                "generation": 1,
                "principal_kind": "person",
                "actor_person_id": env.person,
                "actor_user_id": "10001",
                "presence_id": env.presence,
                "space_id": env.space,
                "bot_user_id": "80001",
                "current_group_id": "20001",
                "instruction": instruction,
                "context_profile": "creator_private",
                "delivery_target": "none",
            }
            works = WorkRepository(database)
            lease = await works.acquire(env.context.conversation_id, 1)
            accepted = await works.accept(
                lease, source_key=source_key, source=source, goal=instruction
            )
            await works.release(lease)
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(work).where(work.c.id == accepted["id"]).values(state="queued")
                )
            responses = iter(
                (
                    ChatResponse(
                        "",
                        0,
                        tool_calls=(
                            ToolCall(
                                "original-send",
                                ToolFunction("send_message", '{"text":"原先已发送"}'),
                            ),
                        ),
                    ),
                    ChatResponse(
                        "",
                        0,
                        tool_calls=(
                            ToolCall(
                                "original-wait",
                                ToolFunction(
                                    "task_control",
                                    '{"action":"wait","conditions":[{"kind":"time_due","after_seconds":0}]}',
                                ),
                            ),
                        ),
                    ),
                )
            )
            provider = FakeLLMProvider(lambda _: next(responses))
            wires = []
            if paused_read:

                def transport(request):
                    wires.append(json.loads(request.content))
                    if len(wires) == 1:
                        blocks = [
                            {
                                "type": "tool_use",
                                "id": "original-send",
                                "name": "send_message",
                                "input": {"text": "原先已发送"},
                            }
                        ]
                    else:
                        blocks = [
                            {
                                "type": "server_tool_use",
                                "id": "original-paid-server-tool",
                                "name": "web_search",
                                "input": {"query": "原先的付费查询"},
                            }
                        ]
                    return httpx.Response(
                        200,
                        json={
                            "id": f"original-response-{len(wires)}",
                            "stop_reason": "tool_use" if len(wires) == 1 else "pause_turn",
                            "content": blocks,
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                        },
                    )

                client = httpx.AsyncClient(
                    base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
                )
                provider = AnthropicMessagesProvider(
                    base_url="https://wire.invalid/v1",
                    api_key="synthetic-key",
                    timeout_seconds=1,
                    max_retries=0,
                    client=client,
                )
            harness = build_harness(database, settings, provider)
            chat = harness.processor._chat
            if paused_read:
                bind_native_model(chat, provider)
            chat._tools.social_service = env.service
            chat.runtime.runner.main_contract = MainAgentContract(chat, ShortState(env.store))
            if private_read:
                private_scope = ConversationScope.private(env.bot.self_id, "10001")
                private = await ScopedEventLedgerUnitOfWork(
                    database, config=RollupPolicyConfig()
                ).append(
                    scope=private_scope,
                    platform_message_id="original-private",
                    sender_user_id="10001",
                    direction="inbound",
                    content=private_text,
                )
                private_version, _ = await chat._ledger.read_scope_context(private_scope, limit=1)
                private_version = replace(private_version, visible_event_ids=(private.event.id,))
                assemble = chat._context_assembler.assemble

                async def original_private_read(**kwargs):
                    # Reproduce the already selected private read set at its first
                    # paid dispatch. Online recovery does not re-run an old loader.
                    context = await assemble(**kwargs)
                    history = ChatMessage("user", private_text)
                    return replace(
                        context,
                        history_messages=(history,),
                        visible_event_ids=frozenset({private.event.id}),
                        read_version=private_version,
                        history_fragments=(((private.event.id,), history),),
                        history_event_fragments=(((private.event.id,), history),),
                        prompt_conversation_id=private_version.conversation_id,
                        prompt_scope_key=private_scope.key,
                        prompt_generation=private_version.generation,
                        projection_scope="",
                    )

                monkeypatch.setattr(chat._context_assembler, "assemble", original_private_read)
            resumer = make_work_resumer(
                works,
                ledger=chat._ledger,
                scopes=chat._conversation_scopes,
                turns=chat._turn_coordinator,
                router=env.router,
                config=chat._runtime_config,
                generate_self=chat.generate_self_initiative,
                generate_wakeup=chat.generate_main_agent_wakeup,
                bindings=chat.runtime.bindings,
                settings=settings,
                generate_origin=chat.generate_origin_turn,
            )
            await WorkScheduler(works, resumer.resume, chat_admission_enabled=True).drain_once()
            actual = await works.get(accepted["id"])
            assert actual["state"] == ("queued" if paused_read else "waiting_external")
            assert actual["model_requests"] == 2
            requests = wires if paused_read else provider.requests
            assert len(requests) == 2
            if paused_read:
                assert all(
                    any(tool.get("type") == "web_search_20250305" for tool in wire["tools"])
                    for wire in wires
                )
            if private_read:
                assert private_text in repr(requests[0])
            async with database.sessions() as reader:
                saved_journal = dict(
                    (
                        await reader.execute(
                            select(journal).where(journal.c.work_id == accepted["id"])
                        )
                    )
                    .mappings()
                    .one()
                )
                private_payload = await ProtocolStore(database).hydrate(
                    json.loads(saved_journal["payload_json"])
                )
                selected_scope = private_payload["metadata"]["source_guard"]["version"]["scope"]
                assert selected_scope["scope_type"] == ("private" if private_read else "group")
                if private_read:
                    assert (
                        private_payload["metadata"]["source_guard"]["version"]["conversation_id"]
                        != actual["conversation_id"]
                    )
                if paused_read:
                    assert private_payload["metadata"]["progress"]["provider_pause_replay"]
                    assert "original-paid-server-tool" in repr(private_payload["transcript"])
                original_effect_key, receipt = (
                    await reader.execute(
                        select(effects.c.effect_key, effects.c.receipt_json)
                        .where(effects.c.work_id == accepted["id"])
                        .order_by(effects.c.created)
                    )
                ).first()
            # The original caller checkpointed the Work it was waiting for.
            async with database.immediate_session() as writer:
                await writer.execute(
                    insert(invocations).values(
                        run_id=run.id,
                        script_hash=original.script_hash,
                        phase="agent",
                        payload_json=json.dumps(
                            {
                                "outputs": {},
                                "steps_completed": 0,
                                "llm_calls": actual["model_requests"],
                                "tool_calls": actual["tool_calls"],
                                "messages_sent": actual["sent_messages"],
                                "next_step": 0,
                                "work_id": accepted["id"],
                                "active_seconds": actual["active_seconds"],
                            }
                        ),
                        updated=actual["updated"],
                    )
                )
            async with database.sessions() as reader:
                versions = [
                    dict(
                        (
                            await reader.execute(
                                text("SELECT * FROM automation_versions WHERE id=:id"),
                                {"id": row.version_id},
                            )
                        )
                        .mappings()
                        .one()
                    )
                    for row in records
                ]
                saved_run = dict(
                    (
                        await reader.execute(
                            text("SELECT * FROM automation_runs WHERE id=:id"), {"id": run.id}
                        )
                    )
                    .mappings()
                    .one()
                )
                saved_work = dict(
                    (await reader.execute(select(work).where(work.c.id == accepted["id"])))
                    .mappings()
                    .one()
                )
                saved_cursor = dict(
                    (
                        await reader.execute(
                            select(invocations).where(invocations.c.run_id == run.id)
                        )
                    )
                    .mappings()
                    .one()
                )
                return (
                    records,
                    scripts,
                    versions,
                    run.id,
                    saved_run,
                    accepted["id"],
                    source,
                    saved_work,
                    saved_cursor,
                    saved_journal,
                    receipt,
                    original_effect_key,
                    env,
                )
        finally:
            if client is not None:
                await client.aclose()
            await database.close()

    (
        records,
        scripts,
        versions,
        run_id,
        saved_run,
        work_id,
        source,
        saved_work,
        saved_cursor,
        saved_journal,
        receipt,
        original_effect_key,
        env,
    ) = asyncio.run(seed())
    command.upgrade(config, "head")
    command.upgrade(config, "head")

    async def verify():
        database = Database(url)
        client = None
        try:
            repo = AutomationRepository(database)
            current = [await repo.get(row.id) for row in records]
            assert current[0].script["goal"] == goal
            assert current[0].script["constraints"] == ["完整约束尾部"]
            assert current[0].script["delivery"] == {"target": "none"}
            assert current[1].script["goal"] == "在安排到期时发送以下提醒原文：\n" + reminder
            assert current[1].canonical_target_space_id == records[1].canonical_target_space_id
            assert current[2].status is AutomationStatus.BLOCKED and current[2].next_run_at is None
            assert current[2].script == json.loads(scripts[2])
            assert current[3].script["goal"] == json_instruction
            assert current[3].script["constraints"] == []
            assert current[3].script["delivery"] == {"target": "none"}
            assert current[4].status is AutomationStatus.BLOCKED and current[4].next_run_at is None
            assert current[4].script == json.loads(scripts[4])
            assert current[5].status is AutomationStatus.BLOCKED and current[5].next_run_at is None
            assert current[5].script == json.loads(scripts[5])
            for index in (0, 1, 3):
                assert current[index].version_id != records[index].version_id
                assert current[index].script_hash == canonical_task_hash(
                    TaskSpec.model_validate(current[index].script)
                )
            async with database.sessions() as reader:
                for original in versions:
                    assert (
                        dict(
                            (
                                await reader.execute(
                                    text("SELECT * FROM automation_versions WHERE id=:id"),
                                    {"id": original["id"]},
                                )
                            )
                            .mappings()
                            .one()
                        )
                        == original
                    )
                assert (
                    dict(
                        (
                            await reader.execute(
                                text("SELECT * FROM automation_runs WHERE id=:id"), {"id": run_id}
                            )
                        )
                        .mappings()
                        .one()
                    )
                    == saved_run
                )
                assert (
                    dict(
                        (await reader.execute(select(work).where(work.c.id == work_id)))
                        .mappings()
                        .one()
                    )
                    == saved_work
                )
                assert (
                    dict(
                        (
                            await reader.execute(
                                select(invocations).where(invocations.c.run_id == run_id)
                            )
                        )
                        .mappings()
                        .one()
                    )
                    == saved_cursor
                )
                assert (
                    dict(
                        (await reader.execute(select(journal).where(journal.c.work_id == work_id)))
                        .mappings()
                        .one()
                    )
                    == saved_journal
                )
                assert (
                    await reader.scalar(
                        select(effects.c.receipt_json).where(
                            effects.c.effect_key == original_effect_key
                        )
                    )
                    == receipt
                )
                assert await reader.scalar(text("SELECT COUNT(*) FROM automation_versions")) == 9
            original = await repo.get_for_run(run_id)
            assert (
                original.script == json.loads(scripts[0])
                and original.script_hash == records[0].script_hash
            )
            assert await repo.initial_script_hash(records[0].id) == records[0].script_hash
            settings = make_settings(
                url,
                automation_enabled=True,
                runtime_work_enabled=True,
                enabled_groups_csv="20001",
                agent_max_model_requests=2,
                **({"web_enabled": True, "web_mode": WebMode.NATIVE} if paused_read else {}),
            )
            service = AutomationService(
                settings=settings, repository=repo, time_service=TimeContextService(database)
            )
            for action in ("resume", "run_now"):
                with pytest.raises(ValidationError):
                    await service.administer_transition(records[2].id, action=action)
            assert (await repo.get(records[2].id)).status is AutomationStatus.BLOCKED
            assert await repo.run_history(records[2].id) == ()
            recovered = await recover_automation_source(
                database, source["conversation_id"], source, request_id=work_id, settings=settings
            )
            assert (
                recovered.content == instruction
                and recovered.actor_person_id == source["actor_person_id"]
            )
            provider = FakeLLMProvider(
                lambda _: ChatResponse(
                    "",
                    0,
                    tool_calls=(
                        ToolCall(
                            "complete-original",
                            ToolFunction(
                                "task_control", '{"action":"complete","result":"原执行完成"}'
                            ),
                        ),
                    ),
                )
            )
            wires = []
            if paused_read:

                def transport(request):
                    wires.append(json.loads(request.content))
                    return httpx.Response(
                        200,
                        json={
                            "id": "complete-original-response",
                            "stop_reason": "tool_use",
                            "content": [
                                {
                                    "type": "web_search_tool_result",
                                    "tool_use_id": "original-paid-server-tool",
                                    "content": [],
                                },
                                {
                                    "type": "tool_use",
                                    "id": "complete-original",
                                    "name": "task_control",
                                    "input": {"action": "complete", "result": "原执行完成"},
                                },
                            ],
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                        },
                    )

                client = httpx.AsyncClient(
                    base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
                )
                provider = AnthropicMessagesProvider(
                    base_url="https://wire.invalid/v1",
                    api_key="synthetic-key",
                    timeout_seconds=1,
                    max_retries=0,
                    client=client,
                )
            harness = build_harness(database, settings, provider)
            chat = harness.processor._chat
            if paused_read:
                bind_native_model(chat, provider)
            chat._tools.social_service = env.service
            chat.runtime.runner.main_contract = MainAgentContract(chat, ShortState(env.store))
            works = WorkRepository(database)
            resumer = make_work_resumer(
                works,
                ledger=chat._ledger,
                scopes=chat._conversation_scopes,
                turns=chat._turn_coordinator,
                router=env.router,
                config=chat._runtime_config,
                generate_self=chat.generate_self_initiative,
                generate_wakeup=chat.generate_main_agent_wakeup,
                bindings=chat.runtime.bindings,
                settings=settings,
                generate_origin=chat.generate_origin_turn,
            )
            if not paused_read:
                from qq_ai_bot.runtime.work_wait import WorkWaitRepository

                waits = WorkWaitRepository(works)
                waiting = await waits.describe(work_id)
                await waits.deliver_due(now=waiting["registered_at"] + 1)
            await WorkScheduler(works, resumer.resume, chat_admission_enabled=True).drain_once()
            finished = await works.get(work_id)
            assert finished["state"] == "completed" and finished["model_requests"] == 3
            assert (
                finished["source_json"] == saved_work["source_json"]
                and finished["goal"] == instruction
            )
            run = (await repo.run_history(records[0].id))[0]
            assert (
                run.id == run_id
                and run.version_id == records[0].version_id
                and run.status is RunStatus.SUCCEEDED
            )
            requests = wires if paused_read else provider.requests
            assert run.llm_calls == 3 and len(requests) == 1
            if paused_read:
                assert private_text in repr(requests[0])
                assert "original-paid-server-tool" in repr(requests[0])
            elif private_read:
                assert private_text not in repr(requests[0])
            async with database.sessions() as reader:
                assert (
                    dict(
                        (
                            await reader.execute(
                                select(invocations).where(invocations.c.run_id == run_id)
                            )
                        )
                        .mappings()
                        .one()
                    )
                    == saved_cursor
                )
                assert all(
                    private_text not in payload
                    for payload in await reader.scalars(
                        text("SELECT payload_json FROM prompt_projections")
                    )
                )
                assert (
                    await reader.scalar(
                        select(effects.c.receipt_json).where(
                            effects.c.effect_key == original_effect_key
                        )
                    )
                    == receipt
                )
                assert (
                    await reader.scalar(
                        select(budgets.c.models).where(budgets.c.root_id == work_id)
                    )
                    == 3
                )
            assert [action for action, _ in env.bot.calls].count("send_group_msg") == 1
            published = await service.administer_update(
                records[2].id,
                {
                    "name": "明确的新要求",
                    "goal": "用户明确重新提供的完整任务目标",
                    "trigger": {"type": "after", "seconds": 1},
                    "delivery": {"target": "current_group"},
                },
            )
            assert published.id == records[2].id and published.version_id != records[2].version_id
            assert published.script["goal"] == "用户明确重新提供的完整任务目标"
            resumed = await service.administer_transition(published.id, action="resume")
            assert resumed.status is AutomationStatus.ACTIVE
            assert await repo.initial_script_hash(published.id) == records[2].script_hash
            assert len(requests) == 1
        finally:
            if client is not None:
                await client.aclose()
            await database.close()

    asyncio.run(verify())
