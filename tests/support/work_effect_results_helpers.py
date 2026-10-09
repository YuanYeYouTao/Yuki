"""Durable execution facts outlive presentation windows and result TTLs."""

from tests.support.social_identity_cases import social_env
from tests.support.work_session import WorkSession, invoke_tool

from qq_ai_bot.capabilities.results import ToolResultBudgeter
from qq_ai_bot.domain.messages import ChatMessage, ToolCall, ToolFunction
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.turn_transcript import TurnTranscript
from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository


async def owned_session(database, tmp_path):
    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)
    assert lease is not None

    async def valid():
        assert await repo.valid(lease)

    control = WorkControl(repo, lease, "result-test", {}, valid)
    control.current = await repo.accept(
        lease, source_key="result-test", source={}, goal="execute once"
    )
    session = WorkSession(control, "result-test")
    task = ChatMessage("user", "execute once")
    await session.restore(
        TurnTranscript((ChatMessage("system", "fixed"), task)), compaction_brief=task
    )
    store = ToolArtifactRepository(database, tmp_path / "results", retention_seconds=60)
    return control, session, store


async def execute(session, store, identity, outcome, *, side_effecting=True):
    call = ToolCall(
        id=identity, type="function", function=ToolFunction(outcome.tool_name or "test", "{}")
    )

    async def invoke():
        return (
            await ToolResultBudgeter(max_characters=24000, artifacts=store).render(outcome)
        ).text

    return await invoke_tool(session, call, invoke, side_effecting=side_effecting)
