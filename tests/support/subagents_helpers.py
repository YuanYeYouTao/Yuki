"""Real SQLite worker isolation, recovery and shared-budget contracts."""

from tests.support.social_identity_cases import social_env

from qq_ai_bot.runtime.subagent_repository import SubagentRepository
from qq_ai_bot.runtime.work_repository import WorkRepository


async def stack(database, tmp_path):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    parent = await repository.accept(
        lease, source_key="parent", source={}, goal="draw", output_kind="answer"
    )
    workers = SubagentRepository(repository)
    identity = await workers.start(
        lease, parent["id"], "spawn", {"goal": "draw", "output_kind": "answer"}
    )
    return repository, workers, lease, parent, identity
