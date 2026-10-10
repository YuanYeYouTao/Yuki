"""Capacity replacement preserves original input and paired execution facts."""

import json

from tests.conftest import build_harness, make_settings

# P10: fixed typed backend fixture, original assertions retained.
from tests.support.social_identity_cases import social_env
from tests.support.work_session import WorkSession

from qq_ai_bot.domain.messages import (
    ChatMessage,
)
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.services.turn_transcript import TurnTranscript
from qq_ai_bot.workspace.short_state import ShortState
from qq_ai_bot.workspace.store import WorkspaceStore


async def _session(database, tmp_path, *, worker=False):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(repository, lease, "capacity-test", {"trigger_event_id": 1}, validate)
    control.current = await repository.accept(
        lease, source_key="capacity-test", source=control.source, goal="prepare an artifact"
    )
    if worker:
        from qq_ai_bot.runtime.subagent_repository import SubagentRepository

        children = SubagentRepository(repository)
        identity = await children.start(
            lease,
            control.current["id"],
            "capacity-child",
            {"goal": "prepare an artifact"},
        )
        await repository.release(lease)
        child_lease = await children.acquire(identity)
        assert child_lease is not None

        async def validate_child():
            assert await repository.valid(child_lease)

        current = await repository.get(identity)
        control = WorkControl(
            repository,
            child_lease,
            current["source_key"],
            json.loads(current["source_json"]),
            validate_child,
        )
        control.current = current
    task = ChatMessage("user", "Prepare the artifact and preserve the original instructions.")
    initial = (ChatMessage("system", "fixed contract"), task)
    session = WorkSession(control, "capacity-contract")
    control.session = session
    await session.restore(TurnTranscript(initial), compaction_brief=task)
    return control, session, initial


def _grow(transcript):
    # Old, replaceable history must sit outside the retained recent raw suffix.
    for _ in range(20):
        transcript.append(ChatMessage("assistant", "Completed investigation. " * 500))
    for _ in range(16):
        transcript.append(ChatMessage("assistant", "Recent completed check."))


async def _runtime(database, control, initial, provider, *, contract_workspace=None, **settings):
    harness = build_harness(database, make_settings(database.url, **settings), provider)
    chat = harness.processor._chat
    if contract_workspace is not None:
        chat.runtime.runner.main_contract = MainAgentContract(
            chat, ShortState(WorkspaceStore(contract_workspace))
        )
    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="10001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="capacity-test",
        current_group_id=None,
        bot_user_id="80001",
        gateway=None,
        runtime_config=await chat._runtime_config.snapshot(),
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=8,
        max_model_requests=1,
        work_control=control,
        compaction_brief=initial[-1],
    )
    return chat.runtime.runner, runtime
