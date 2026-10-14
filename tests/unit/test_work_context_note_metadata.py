"""Optional private notes keep source checks without summary-format gates."""

from tests.support.social_identity_cases import social_env

from qq_ai_bot.runtime.work_context_note import visible_context_note
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.tool_results.access import ArtifactAccess


async def test_optional_note_keeps_repeated_references_and_future_metadata(database, tmp_path):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(repository, lease, "optional-note", {"trigger_event_id": 1}, validate)
    control.bind_context_access(ArtifactAccess(lease.conversation_id, 1, env.person))
    control.current = await repository.accept(
        lease,
        source_key=control.source_key,
        source=control.source,
        goal="finish original audit",
    )
    await repository.checkpoint(lease, control.current["id"], None, models=3, tools=1)
    control.current = await repository.get(control.current["id"])
    await control.update_context_note(
        {"version": 2, "facts": [{"text": "", "refs": ["goal", "goal"]}], "future": True},
        "original-note",
    )
    note = await visible_context_note(control)
    assert note["facts"] == [{"text": "", "refs": ["goal", "goal"]}]
    assert note["version"] == 2
    assert (await repository.get(control.current["id"]))["model_requests"] == 3
    await repository.release(lease)
