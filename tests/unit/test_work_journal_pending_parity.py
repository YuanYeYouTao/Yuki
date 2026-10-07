"""A saved provider response remains recoverable across a contract boundary."""

import json

import pytest
from tests.support.social_identity_cases import social_env
from tests.support.work_session import WorkSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.messages import ChatMessage, ToolCall, ToolFunction
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_journal import JournalUnavailable
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.turn_transcript import TurnTranscript


@pytest.mark.parametrize("count,id_length", [(1, 16), (33, 16), (1, 129)])
async def test_saved_pending_calls_survive_contract_change(database, tmp_path, count, id_length):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(repository, lease, "pending-parity", {}, validate)
    control.current = await repository.accept(
        lease, source_key="pending-parity", source={}, goal="Inspect the source."
    )
    session = WorkSession(control, "original-contract")
    control.session = session
    initial = TurnTranscript((ChatMessage("system", "contract"), ChatMessage("user", "Inspect.")))
    await session.restore(initial)
    calls = tuple(
        ToolCall(id=f"{index}:" + "x" * id_length, function=ToolFunction("read_probe", "{}"))
        for index in range(count)
    )
    initial.append(ChatMessage("assistant", tool_calls=calls))
    await session.save("response", calls)
    original = await session.journal.load(lease, control.current["id"], "original-contract")
    assert original.reason == "resume"
    changed = await session.journal.load(lease, control.current["id"], "changed-contract")
    assert changed.reason == "contract_changed"
    assert changed.pending_calls == tuple(
        {"id": call.id, "name": call.function.name, "arguments": call.function.arguments}
        for call in calls
    )
    # Recovery returns pending identities without dispatching or inventing success.
    for call in changed.pending_calls:
        receipt = json.loads(await session.journal.effect_result(session.call_key(call["id"])))
        assert receipt["executed"] is False
        assert receipt["replay_forbidden"] is True
    await repository.release(lease)


async def test_pending_call_identity_is_still_required(database, tmp_path):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    current = await repository.accept(
        lease, source_key="pending-invalid", source={}, goal="Inspect."
    )
    from qq_ai_bot.runtime.work_journal import WorkJournal

    journal = WorkJournal(repository)
    async with database.sessions() as reader:
        source = await reader.get(CanonicalConversationModel, lease.conversation_id)
        revision = source.prompt_source_revision
    await journal.save(
        lease,
        current["id"],
        "original-contract",
        TurnTranscript((ChatMessage("user", "Inspect."),)),
        phase="response",
        pending=[{"id": "", "name": "read_probe", "arguments": "{}"}],
        source_revision=revision,
        metadata={"sequence": 0},
    )
    with pytest.raises(JournalUnavailable, match="work_journal_corrupt"):
        await journal.load(lease, current["id"], "changed-contract")
    await repository.release(lease)
