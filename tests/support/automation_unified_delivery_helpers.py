"""Exercise actual scheduled Main Agent sends, receipts and cursor recovery."""

import json
from types import SimpleNamespace

from tests.conftest import build_harness, make_settings
from tests.support.social_identity_cases import social_env

from qq_ai_bot.automation.executor import AutomationExecutor
from qq_ai_bot.automation.gateway import OneBotAutomationGateway
from qq_ai_bot.automation.handlers import AutomationCapabilityHandlers
from qq_ai_bot.automation.registry import build_capability_registry
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.social.automation import SocialAutomationAdapter
from qq_ai_bot.workspace.short_state import ShortState


async def setup_run(
    database,
    tmp_path,
    *,
    strategy="agentic",
    delivery="current_group",
    mode="send",
    principal="person",
    worker=None,
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
    if worker is not None:
        import hashlib

        from tests.support.codemode_cases import worker as pinned_worker

        settings.code_mode_launcher_path = pinned_worker().launcher_path
        settings.code_mode_launcher_sha256 = pinned_worker().launcher_sha256
        settings.code_mode_enabled = True
        settings.code_mode_worker_path = worker
        settings.code_mode_worker_sha256 = hashlib.sha256(worker.read_bytes()).hexdigest()
    harness = build_harness(database, settings, provider)
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    env.service.runtime_config = chat._runtime_config
    contract = MainAgentContract(
        chat, ShortState(env.store), code_enabled=settings.code_mode_enabled
    )
    chat.runtime.runner.main_contract = contract
    chat.runtime.runner.code_mode_settings = settings
    repository = AutomationRepository(database)

    def gateway(context):
        return OneBotAutomationGateway(
            bot_user_id=context.bot_user_id,
            automation_id=context.automation_id,
            automation_run_id=context.automation_run_id,
            router=env.router,
        )

    handlers = AutomationCapabilityHandlers(
        settings=settings,
        main_turns=chat.runtime.main_turns,
        main_contract=chat.runtime.runner.main_contract,
        runtime_config=chat._runtime_config,
        time_service=chat._time,
        ledger=harness.ledger,
        memories=chat._memories,
        web_provider=None,
        gateway_factory=gateway,
    )
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
    if principal == "self":
        from tests.integration.test_self_automation_delivery import self_actor

        actor = await self_actor(database, env)
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
        chat=chat,
    )


def sent(env):
    return [
        params
        for action, params in env.bot.calls
        if action in {"send_group_msg", "send_private_msg"}
    ]
