"""Keep all recovery phases while updating immutable media and refs once."""

import json
from hashlib import sha256

import pytest
from sqlalchemy import event, insert, select, update
from tests.unit.test_work_protocol_continuity import _control

from qq_ai_bot.domain.messages import ChatImage, ChatMessage
from qq_ai_bot.runtime.subagent_schema import media, media_refs
from qq_ai_bot.runtime.work_schema_v1 import inputs, journal
from qq_ai_bot.runtime.work_session import WorkSession
from qq_ai_bot.services.turn_transcript import TurnTranscript


@pytest.mark.asyncio
async def test_repeated_recovery_phases_do_not_reinsert_existing_media_or_refs(database, tmp_path):
    control = await _control(database, tmp_path)
    session = WorkSession(control, "fixed")
    original = TurnTranscript(
        (
            ChatMessage(
                "user",
                "inspect",
                images=tuple(ChatImage(f"data:image/png;base64,image-{i}") for i in range(32)),
            ),
        )
    )
    transcript = await session.restore(original)
    media_writes = []
    journal_writes = []

    def sql(connection, cursor, statement, parameters, context, many):
        lowered = statement.lower()
        if lowered.startswith(("insert", "delete")) and "runtime_work_media" in lowered:
            media_writes.append((lowered.split()[0], many))
        if lowered.startswith("insert") and "runtime_work_journal" in lowered:
            journal_writes.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", sql)
    try:
        for phase in ("dispatched", "response", "paired"):
            await session.save(phase)
            async with database.sessions() as reader:
                assert (
                    await reader.scalar(
                        select(journal.c.phase).where(journal.c.work_id == control.current["id"])
                    )
                    == phase
                )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", sql)
    assert media_writes == [("insert", True), ("insert", True)]
    assert len(journal_writes) == 3
    restored = await WorkSession(control, "fixed").restore(TurnTranscript(()))
    assert restored.request() == transcript.request()


@pytest.mark.asyncio
@pytest.mark.parametrize("input_state", ["pending", "staged"])
async def test_journal_delta_retains_unpaired_input_and_other_work_media(
    database, tmp_path, input_state
):
    control = await _control(database, tmp_path)
    session = WorkSession(control, "fixed")
    await session.restore(TurnTranscript((ChatMessage("user", "no image yet"),)))
    other = await control.repository.accept(
        control.lease, source_key="other-work", source={}, goal="other"
    )
    content = b"data:image/png;base64,unpaired-input"
    digest = sha256(content).hexdigest()
    async with database.immediate_session() as writer:
        await writer.execute(insert(media).values(sha256=digest, content=content))
        await writer.execute(
            insert(media_refs),
            [
                {"work_id": identity, "sha256": digest}
                for identity in (control.current["id"], other["id"])
            ],
        )
        input_id = await writer.scalar(
            insert(inputs)
            .values(
                conversation_id=control.lease.conversation_id,
                generation=control.lease.generation,
                source_key="unpaired-image",
                work_id=control.current["id"],
                kind="user",
                state=input_state,
                payload_json=json.dumps({"images": [{"$work_media": digest}]}),
                created=1,
            )
            .returning(inputs.c.id)
        )
    await session.save("response")
    async with database.sessions() as reader:
        assert set(await reader.scalars(select(media_refs.c.work_id))) == {
            control.current["id"],
            other["id"],
        }
        assert await reader.scalar(select(media.c.content)) == content
    # Once paired/consumed, a transcript without that image may release this
    # work's ref. A different work's immutable blob must remain recoverable.
    async with database.immediate_session() as writer:
        await writer.execute(update(inputs).where(inputs.c.id == input_id).values(state="consumed"))
    await session.save("paired")
    async with database.sessions() as reader:
        assert tuple(await reader.scalars(select(media_refs.c.work_id))) == (other["id"],)
        assert await reader.scalar(select(media.c.content)) == content
