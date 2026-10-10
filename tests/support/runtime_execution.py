"""Explicit execution dependencies for runtime behavior fixtures."""

from unittest.mock import AsyncMock

from qq_ai_bot.config import Settings
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.runtime.activation_bindings import ActiveWorkBindings
from qq_ai_bot.runtime.subagent_repository import SubagentRepository
from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository
from qq_ai_bot.services.main_agent_backend import MainAgentBackend
from qq_ai_bot.services.subagent_execution import SubagentExecution, SubagentExecutionDependencies
from qq_ai_bot.services.work_resume import WorkResumeDependencies, WorkResumer


def make_work_resumer(
    repository,
    *,
    ledger,
    scopes,
    turns,
    router,
    config,
    generate_self,
    generate_wakeup,
    validate_snapshot,
    bindings=None,
    sandbox_tasks=None,
):
    return WorkResumer(
        repository,
        WorkResumeDependencies(
            settings=Settings(_env_file=None),
            resume_automation=AsyncMock(
                side_effect=AssertionError("unexpected automation recovery")
            ),
            ledger=ledger,
            conversation_scopes=scopes,
            turn_coordinator=turns,
            presence_router=router,
            runtime_config=config,
            sandbox_tasks=sandbox_tasks or SandboxTaskRepository(repository.database),
            active_bindings=bindings or ActiveWorkBindings(),
            generate_self=generate_self,
            generate_wakeup=generate_wakeup,
            validate_snapshot=validate_snapshot,
            resume_plugin=AsyncMock(side_effect=AssertionError("unexpected plugin recovery")),
        ),
    )


def make_child_executor(
    repository,
    *,
    chat,
    config,
    runner,
    load_tools,
    ledger=None,
    sandbox_tasks=None,
    sandbox_client=None,
):
    return SubagentExecution(
        repository,
        SubagentRepository(repository),
        SubagentExecutionDependencies(
            active_bindings=chat.runtime.bindings
            if hasattr(chat, "runtime")
            else ActiveWorkBindings(),
            ledger=ledger or EventLedgerRepository(repository.database),
            runtime_config=config,
            sandbox_tasks=sandbox_tasks or SandboxTaskRepository(repository.database),
            sandbox_client=sandbox_client or AsyncMock(),
            runner=runner,
            load_tools=load_tools,
            open_memory=chat.open_memory_session,
            open_self_memory=chat.open_self_memory_session,
            backend_factory=lambda runtime, allowed: MainAgentBackend(
                chat, runtime, allowed_tools=allowed
            ),
            web_capabilities=chat.web_capabilities,
        ),
    )
