"""Index maintenance preserves original facts and shares their commit boundary."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, text
from tests.support.event_seed import seed_scoped_event

from yuki.domain.audio import AudioTranscript, serialize_transcripts
from yuki.domain.conversations import ConversationScope
from yuki.identity.canonical_repository import find_identity_binding
from yuki.persistence.history_search import compile_match, prepare_query
from yuki.persistence.models import ChatEventModel
from yuki.persistence.people_repository import PeopleRepository
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


async def indexed(database, conversation_id, keyword):
    async with database.read_session() as reader:
        return tuple(
            await reader.scalars(
                text("SELECT rowid FROM chat_history_fts WHERE chat_history_fts MATCH :match"),
                {"match": compile_match(conversation_id, prepare_query(keyword))},
            )
        )


async def test_outbound_prepare_is_outside_writer_and_append_rollback_is_atomic(
    database, monkeypatch
):
    import yuki.persistence.scoped_event_uow as module

    original = module.prepare_terms
    inside_writer = False
    immediate = database.immediate_session

    @asynccontextmanager
    async def observed_writer():
        nonlocal inside_writer
        async with immediate() as session:
            inside_writer = True
            try:
                yield session
            finally:
                inside_writer = False

    def prepare(*args):
        assert not inside_writer
        return original(*args)

    monkeypatch.setattr(database, "immediate_session", observed_writer)
    monkeypatch.setattr(module, "prepare_terms", prepare)
    writer = ScopedEventLedgerUnitOfWork(database)
    result = await seed_scoped_event(
        writer,
        scope=ConversationScope.private("8000", "1001"),
        origin_key="new-outbound",
        sender_user_id="8000",
        direction="outbound",
        content="繁體與乾坤",
    )
    assert await indexed(database, result.event.canonical_conversation_id, "繁体") == (
        result.event.id,
    )
    from yuki.persistence.history_search_index import replace_event

    with pytest.raises(RuntimeError, match="rollback"):
        async with database.immediate_session() as session:
            await session.execute(
                delete(ChatEventModel).where(ChatEventModel.id == result.event.id)
            )
            await replace_event(
                session, result.event.id, result.event.canonical_conversation_id, ()
            )
            raise RuntimeError("rollback")
    assert await indexed(database, result.event.canonical_conversation_id, "乾") == (
        result.event.id,
    )


async def test_asr_indexes_only_current_speech_and_delete_removes_it(database):
    writer = ScopedEventLedgerUnitOfWork(database)
    result = await seed_scoped_event(
        writer,
        scope=ConversationScope.group("8000", "2001"),
        origin_key="asr-index",
        sender_user_id="1001",
        direction="inbound",
        content="",
        segments=({"type": "record", "data": {"file": "voice"}},),
    )
    transcript = serialize_transcripts(
        (
            AudioTranscript("current", 0, "自己的語音"),
            AudioTranscript("reply", 1, "引用別人的話"),
        )
    )
    assert await writer.set_audio_transcript(
        result.event.id, transcript, generation=result.scope.generation
    )
    conversation_id = result.event.canonical_conversation_id
    assert await indexed(database, conversation_id, "自己的语音") == (result.event.id,)
    assert await indexed(database, conversation_id, "引用") == ()
    assert await indexed(database, conversation_id, "segment_index") == ()
    async with database.immediate_session() as session:
        await session.execute(delete(ChatEventModel).where(ChatEventModel.id == result.event.id))
    assert await indexed(database, conversation_id, "自己的语音") == ()


async def test_external_event_indexes_actual_summary_without_payload_json(database):
    writer = ScopedEventLedgerUnitOfWork(database)
    original = await seed_scoped_event(
        writer,
        scope=ConversationScope.group("8000", "2001"),
        origin_key="external-entry",
        sender_user_id="1001",
        direction="inbound",
        content="入口",
    )
    conversation_id = original.event.canonical_conversation_id
    external = await writer.append_external(
        scope=ConversationScope.group(
            "8000",
            "2001",
            presence_id=original.event.ingress_presence_id,
            conversation_id=conversation_id,
        ),
        origin_key="external-summary",
        source_plugin_id="test.history",
        external_source="fixture",
        external_event_key="external-summary",
        external_event_type="fixture.created",
        external_payload={"hidden_key": "秘密負載"},
        external_target_id="2001",
        content="外部事件摘要",
        occurred_at=datetime.now(UTC),
    )
    assert await indexed(database, conversation_id, "外部事件") == (external.event.id,)
    assert await indexed(database, conversation_id, "hidden_key") == ()
    assert await indexed(database, conversation_id, "秘密负载") == ()


async def test_person_forget_removes_owned_index_and_redacts_mentions(database):
    writer = ScopedEventLedgerUnitOfWork(database)
    first = await seed_scoped_event(
        writer,
        scope=ConversationScope.group("8000", "2001"),
        origin_key="forget-original",
        sender_user_id="1001",
        direction="inbound",
        content="秘密記錄",
    )
    other = await seed_scoped_event(
        writer,
        scope=ConversationScope.group("8000", "2001"),
        origin_key="forget-mention",
        sender_user_id="1002",
        direction="inbound",
        content=f"引用 {first.event.author_person_id} 的记录",
        segments=(
            {"type": "at", "data": {"qq": "1001", "person_id": first.event.author_person_id}},
        ),
    )
    people = PeopleRepository(database)
    assert await people.delete_person_id(first.event.author_person_id)
    assert await indexed(database, first.event.canonical_conversation_id, "秘密") == ()
    assert await indexed(database, other.event.canonical_conversation_id, "已删除用户") == (
        other.event.id,
    )


@pytest.mark.parametrize("mention_only", [False, True])
async def test_asr_stale_preparation_cannot_restore_forgotten_content(
    database, monkeypatch, mention_only
):
    import yuki.persistence.scoped_event_uow as module

    writer = ScopedEventLedgerUnitOfWork(database)
    async with database.read_session() as reader:
        forgotten_person = (await find_identity_binding(reader, "1001")).person_id
    content = forgotten_person if mention_only else "旧正文"
    result = await seed_scoped_event(
        writer,
        scope=ConversationScope.group("8000", "2001"),
        origin_key="stale-asr",
        sender_user_id="1002" if mention_only else "1001",
        direction="inbound",
        content=content,
        segments=({"type": "at", "data": {"qq": "1001", "person_id": forgotten_person}},)
        if mention_only
        else (),
    )
    original = module.prepare_terms
    preparing, resume = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()

    def blocked_prepare(*args):
        loop.call_soon_threadsafe(preparing.set)
        asyncio.run_coroutine_threadsafe(resume.wait(), loop).result()
        return original(*args)

    monkeypatch.setattr(module, "prepare_terms", blocked_prepare)
    saving = asyncio.create_task(
        writer.set_audio_transcript(
            result.event.id,
            serialize_transcripts((AudioTranscript("current", 0, "过期转写"),)),
            generation=result.scope.generation,
        )
    )
    await preparing.wait()
    try:
        assert await PeopleRepository(database).delete_person_id(forgotten_person)
    finally:
        resume.set()
    assert not await saving
    assert await indexed(database, result.event.canonical_conversation_id, content) == ()
    assert await indexed(database, result.event.canonical_conversation_id, "过期转写") == ()
