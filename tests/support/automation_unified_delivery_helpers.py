"""Real due-slot -> common Main -> Social/Work regression fixtures."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from tests.conftest import build_harness, make_settings
from tests.support.automation_runtime_helpers import FakeClock
from tests.support.social_identity_cases import social_env

from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.automation.tools import AutomationToolService
from qq_ai_bot.automation.worker import AutomationWorker
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.workspace.short_state import ShortState


async def setup_run(
    database,
    tmp_path,
    *,
    delivery="current_group",
    mode="send",
    principal="person",
    trigger=None,
    max_runs=None,
    superusers_csv=None,
    goal="完成任务",
    constraints=(),
    work_enabled=True,
    self_memory_enabled=False,
    provider=None,
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

    provider = provider or FakeLLMProvider(responder=respond)
    settings = make_settings(
        database.url,
        automation_enabled=True,
        enabled_groups_csv="20001",
        runtime_work_enabled=work_enabled,
        self_memory_enabled=self_memory_enabled,
        **({"superusers_csv": superusers_csv} if superusers_csv is not None else {}),
    )
    harness = build_harness(database, settings, provider)
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    env.service.runtime_config = chat._runtime_config
    chat.runtime.runner.main_contract = MainAgentContract(chat, ShortState(env.store))
    chat.runtime.runner.code_mode_settings = settings
    repository = AutomationRepository(database)
    clock = FakeClock(datetime.now(UTC))
    chat._time._clock = clock
    service = AutomationService(settings=settings, repository=repository, time_service=chat._time)
    chat.set_automation_tools(AutomationToolService(service))
    # Freeze after all real Main providers have been bound.
    actor = ToolActor(
        user_id="10001",
        bot_user_id="80001",
        group_id="20001",
        origin=TurnOrigin.USER_MESSAGE,
        instruction="稍后完成任务并发送结果",
        event_id=1,
        person_id=env.person,
        conversation_id=env.context.conversation_id,
        presence_id=env.presence,
    )
    if principal == "self":
        from tests.integration.test_self_automation_delivery import self_actor

        actor = await self_actor(database, env)
    row = await service.create_task(
        {
            "name": "delivery regression",
            "goal": goal,
            "constraints": constraints,
            "trigger": trigger or {"type": "after", "seconds": 1},
            "delivery": {"target": delivery},
        },
        actor=actor,
        conversation_key="group:80001:20001",
        max_runs=max_runs,
    )
    works = WorkRepository(database)
    worker = AutomationWorker(
        settings=settings,
        repository=repository,
        time_service=chat._time,
        generate=chat.generate_origin_turn,
        runtime_config=chat._runtime_config,
        conversation_scopes=chat._conversation_scopes,
        turns=chat._turn_coordinator,
        router=env.router,
        work_repository=works,
        bindings=chat.runtime.bindings,
    )

    async def dispatch():
        now = clock.now()
        rows = await repository.claim_due(
            worker_id="fixture-owner",
            now=now,
            lease_seconds=settings.automation_lease_seconds,
            clock=clock.now,
        )
        for claimed in rows:
            await worker._process_guarded(claimed)
        history = await repository.run_history(row.id)
        case.run = history[0] if history else None
        return case.run

    case = SimpleNamespace(
        env=env,
        provider=provider,
        row=row,
        repository=repository,
        clock=clock,
        chat=chat,
        worker=worker,
        works=works,
        settings=settings,
        actor=actor,
        service=service,
        dispatch=dispatch,
        harness=harness,
        run=None,
    )
    return case


def sent(env):
    return [
        params
        for action, params in env.bot.calls
        if action in {"send_group_msg", "send_private_msg"}
    ]
