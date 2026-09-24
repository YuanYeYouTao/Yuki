"""Exercise actual scheduled Main Agent sends, receipts and cursor recovery."""

import json
from types import SimpleNamespace

import pytest
from tests.conftest import build_harness, make_settings
from tests.support.social_identity_cases import social_env

from qq_ai_bot.automation.authority import DelegatedAuthority
from qq_ai_bot.automation.executor import AutomationExecutor
from qq_ai_bot.automation.gateway import OneBotAutomationGateway
from qq_ai_bot.automation.handlers import AutomationCapabilityHandlers
from qq_ai_bot.automation.models import AutomationScript, RunStatus
from qq_ai_bot.automation.registry import build_capability_registry
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.automation.validator import ValidatedAutomation, canonical_script_hash
from qq_ai_bot.automation.work_cursor import save as save_cursor
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.social.automation import SocialAutomationAdapter
from qq_ai_bot.social.db_models import SocialOperationModel
from qq_ai_bot.workspace.short_state import ShortState


async def setup_run(
    database, tmp_path, *, strategy="generated", delivery="current_group", mode="send"
):
    env = await social_env(database, tmp_path)
    if delivery == "self_private":
        await env.router.cas_takeover_person(env.person)
    turns = 0

    def respond(request):
        nonlocal turns
        turns += 1
        if turns == 1 and mode != "silent":
            args = {"text": "PUBLIC_RESULT"}
            if delivery == "self_private":
                args["target"] = {"kind": "person", "subject_ref": "current_speaker"}
            if mode == "reject":
                args = {"text": ""}
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="actual-delivery",
                        function=ToolFunction(name="send_message", arguments=json.dumps(args)),
                    ),
                ),
            )
        return "INTERNAL_FINAL_TEXT_MUST_NOT_DELIVER"

    provider = FakeLLMProvider(responder=respond)
    settings = make_settings(
        database.url, automation_enabled=True, enabled_groups_csv="20001", runtime_work_enabled=True
    )
    harness = build_harness(database, settings, provider)
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    env.service.runtime_config = chat._runtime_config
    contract = MainAgentContract(chat, ShortState(env.store))
    chat._agent_runner.main_contract = contract
    repository = AutomationRepository(database)

    def gateway(context):
        return OneBotAutomationGateway(
            bot_user_id=context.bot_user_id,
            automation_id=context.automation_id,
            automation_run_id=context.automation_run_id,
            router=env.router,
        )

    handlers = object.__new__(AutomationCapabilityHandlers)
    handlers._settings = settings
    handlers._runtime_config = chat._runtime_config
    handlers._ledger = harness.ledger
    handlers._memories = chat._memories
    handlers._relationships = harness.relationships
    handlers._time = chat._time
    handlers._agent_runner = chat._agent_runner
    handlers._gateway_factory = gateway
    registry = build_capability_registry(
        {**handlers.mapping(), **SocialAutomationAdapter(env.service, None, None).mapping()}
    )
    service = AutomationService(
        settings=settings, repository=repository, registry=registry, time_service=chat._time
    )
    actor = ToolActor(
        user_id="10001",
        bot_user_id="80001",
        group_id="20001",
        origin=TurnOrigin.USER_MESSAGE,
        instruction="稍后完成任务并发送结果",
        event_id=1,
        person_id=env.person,
        conversation_id=env.context.conversation_id,
    )
    row, plan = await service.create_task(
        {
            "name": "delivery regression",
            "goal": "完成任务",
            "strategy": strategy,
            "trigger": {"type": "after", "seconds": 60},
            "delivery": {"target": delivery},
        },
        actor=actor,
        conversation_key="group:80001:20001",
    )
    run = await repository.create_run(
        row.id, scheduled_for=row.next_run_at, actual_started_at=chat._time.clock.now()
    )
    executor = AutomationExecutor(
        settings=settings,
        registry=registry,
        repository=repository,
        time_service=chat._time,
        router=env.router,
        gateway_factory=gateway,
    )
    return SimpleNamespace(
        env=env,
        provider=provider,
        row=row,
        run=run,
        executor=executor,
        repository=repository,
        plan=plan,
        clock=chat._time.clock,
    )


def sent(env):
    return [
        params
        for action, params in env.bot.calls
        if action in {"send_group_msg", "send_private_msg"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["current_group", "self_private"])
async def test_static_person_reminder_uses_social_receipt(database, tmp_path, delivery):
    from sqlalchemy import select

    case = await setup_run(database, tmp_path, strategy="static", delivery=delivery)
    assert case.row.required_capabilities == ("social.send_message",)
    result = await case.executor.execute(case.row, case.run)
    assert result.status is RunStatus.SUCCEEDED, result
    assert result.messages_sent == 1
    assert len(sent(case.env)) == 1
    async with database.sessions() as session:
        receipt = await session.scalar(
            select(SocialOperationModel).where(
                SocialOperationModel.source_turn_id == f"automation:{case.run.id}"
            )
        )
    assert receipt is not None
    assert receipt.status == "succeeded"
    replay = await case.executor.execute(case.row, case.run)
    assert replay.status is RunStatus.SUCCEEDED
    assert len(sent(case.env)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strategy", "delivery"),
    [("generated", "current_group"), ("agentic", "current_group"), ("generated", "self_private")],
)
async def test_main_agent_owns_scheduled_delivery_and_replay(
    database, tmp_path, strategy, delivery
):
    case = await setup_run(database, tmp_path, strategy=strategy, delivery=delivery)
    case.executor._settings.runtime_work_enabled = False
    disabled = await case.executor.execute(case.row, case.run)
    assert disabled.status is RunStatus.BLOCKED
    assert disabled.error_category == "automation_runtime_required"
    assert not case.provider.requests and not sent(case.env)
    case.executor._settings.runtime_work_enabled = True
    result = await case.executor.execute(case.row, case.run)
    assert result.status is RunStatus.SUCCEEDED, result
    assert len(sent(case.env)) == 1
    assert "PUBLIC_RESULT" in str(sent(case.env))
    assert "INTERNAL_FINAL_TEXT" not in str(sent(case.env))
    assert [step.call for step in case.row.script.steps] == ["yuki.agent"]
    declarations = [request.tools for request in case.provider.requests]
    assert all(value == declarations[0] for value in declarations)
    assert any(tool.name == "send_message" for tool in declarations[0])
    before = len(case.provider.requests)
    # Re-enter the original Agent dispatch after its work completed. The new
    # backend has no counters: only durable work/transport receipts can prove send.
    await save_cursor(database, case.run.id, case.row.script_hash, "agent", {"next_step": 0})
    replay = await case.executor.execute(case.row, case.run)
    assert replay.status is RunStatus.SUCCEEDED, replay
    assert len(case.provider.requests) == before
    assert len(sent(case.env)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delivery", "mode", "expected"),
    [
        ("none", "silent", RunStatus.SUCCEEDED),
        ("current_group", "silent", RunStatus.BLOCKED),
        ("current_group", "reject", RunStatus.BLOCKED),
    ],
)
async def test_silence_is_allowed_but_unconfirmed_delivery_is_not_success(
    database, tmp_path, delivery, mode, expected
):
    case = await setup_run(database, tmp_path, delivery=delivery, mode=mode)
    result = await case.executor.execute(case.row, case.run)
    assert result.status is expected, result
    assert not sent(case.env)
    if expected is RunStatus.BLOCKED:
        assert result.error_category == "agent_delivery_unconfirmed"


async def legacy_run(database, case, *, call="yuki.generate"):
    payload = case.row.script.model_dump(mode="json")
    payload["steps"] = [
        {
            "id": "generate",
            "call": call,
            "arguments": {"instruction": "old task", "context_profile": "none"},
            "save_as": "old",
        },
        {
            "id": "old_delivery",
            "call": "onebot.send_group_message",
            "arguments": {"group_id": "$current_group_id", "text": "${old.text}"},
        },
    ]
    payload["limits"]["max_steps"] = 2
    payload["limits"]["max_tool_calls"] = 2
    script = AutomationScript.model_validate(payload)
    row = await case.repository.create(
        ValidatedAutomation(
            script=script,
            script_hash=canonical_script_hash(script),
            required_capabilities=(call, "onebot.send_group_message"),
            next_run_at=case.row.next_run_at,
        ),
        DelegatedAuthority.model_validate(case.row.authority_snapshot),
        creator_person_id=case.env.person,
        max_runs=1,
        misfire_grace_seconds=300,
        now=case.clock.now(),
    )
    run = await case.repository.create_run(
        row.id, scheduled_for=row.next_run_at, actual_started_at=case.clock.now()
    )
    return row, run


@pytest.mark.asyncio
async def test_stored_generated_tails_never_send_internal_text(database, tmp_path):
    case = await setup_run(database, tmp_path)
    row, run = await legacy_run(database, case)
    first = await case.executor.execute(row, run)
    assert first.status is RunStatus.BLOCKED
    assert first.error_category == "delegated_authority_revoked"
    assert not case.provider.requests
    assert not sent(case.env)
    assert (await case.executor.execute(row, run)).status is RunStatus.BLOCKED
    persisted = await case.repository.get(row.id)
    assert persisted.script_hash == row.script_hash
    assert persisted.script == row.script


@pytest.mark.asyncio
async def test_started_legacy_agent_retires_tail_by_original_receipts(database, tmp_path):
    case = await setup_run(database, tmp_path)
    row, run = await legacy_run(database, case, call="yuki.agent")
    result = await case.executor.execute(row, run)
    assert result.status is RunStatus.BLOCKED, result
    assert result.error_category == "delegated_authority_revoked"
    assert not sent(case.env)
    assert not case.provider.requests
