"""History pages preserve original identity and exact ID continuation semantics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, event, text, update
from tests.support.event_seed import seed_event

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.audio import AudioTranscript, serialize_transcripts
from yuki.domain.conversations import ScopeType
from yuki.identity.db_models import CanonicalPersonModel
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.persistence.event_view import event_view
from yuki.persistence.models import ChatEventModel, ConversationMediaItemModel
from yuki.services.history_query import HistoryQuery


async def _event(database, ledger, key, content, **fields):
    row, _ = await seed_event(
        ledger,
        bot_user_id="8000",
        scope_type=ScopeType.GROUP,
        group_id=fields.pop("group_id", "2001"),
        sender_user_id=fields.pop("sender_user_id", "10001"),
        direction="inbound",
        platform_message_id=key,
        content=content,
        **fields,
    )
    return row


@pytest.mark.asyncio
async def test_id_pages_continue_after_deleted_tail_and_native_time_is_not_order(database):
    ledger = EventLedgerRepository(database)
    stamp = datetime(2026, 9, 1, tzinfo=UTC)
    rows = [
        await _event(database, ledger, f"id-{i}", str(i), occurred_at=stamp - timedelta(days=i))
        for i in range(5)
    ]
    conversation = rows[0].canonical_conversation_id
    page = await ledger.query_history(conversation_id=conversation, limit=2)
    assert [row.id for row in page.events] == [rows[4].id, rows[3].id]
    assert page.has_more and page.next_cursor == rows[3].id
    assert all(row.observed_at is not None for row in page.events)
    async with database.immediate_session() as writer:
        await writer.execute(delete(ChatEventModel).where(ChatEventModel.id == rows[3].id))
    following = await ledger.query_history(
        conversation_id=conversation, limit=2, cursor=page.next_cursor
    )
    assert [row.id for row in following.events] == [rows[2].id, rows[1].id]
    assert following.has_more
    last = await ledger.query_history(
        conversation_id=conversation, limit=2, cursor=following.next_cursor
    )
    assert [row.id for row in last.events] == [rows[0].id]
    assert not last.has_more
    oldest = await ledger.query_history(
        conversation_id=conversation,
        order="oldest",
        limit=2,
        after_event_id=rows[1].id,
    )
    assert [row.id for row in oldest.events] == [rows[2].id, rows[4].id]
    assert not oldest.has_more


@pytest.mark.asyncio
async def test_fts_false_positives_do_not_truncate_or_duplicate_matches(database):
    ledger = EventLedgerRepository(database)
    first = await _event(database, ledger, "true-old", "abcd 京 电动车")
    await _event(database, ledger, "false-one", "abc ... bcd")
    await _event(database, ledger, "false-two", "abc ... bcd")
    last = await _event(database, ledger, "true-new", "abcd")
    conversation = first.canonical_conversation_id
    page = await ledger.query_history(conversation_id=conversation, keyword="abcd", limit=1)
    assert [row.id for row in page.events] == [last.id]
    assert page.has_more and page.matched_fields == {last.id: ("content",)}
    following = await ledger.query_history(
        conversation_id=conversation, keyword="abcd", limit=1, cursor=page.next_cursor
    )
    assert [row.id for row in following.events] == [first.id]
    assert not following.has_more
    any_page = await ledger.query_history(
        conversation_id=conversation, keyword="无匹配 京", match="any", limit=1
    )
    assert [row.id for row in any_page.events] == [first.id]
    assert not any_page.has_more
    excluded = await ledger.query_history(
        conversation_id=conversation, exclude=("abcd",), order="oldest"
    )
    assert [row.content for row in excluded.events] == ["abc ... bcd", "abc ... bcd"]


async def test_multi_conversation_pages_merge_matches_and_keep_original_authors(database):
    ledger = EventLedgerRepository(database)
    first = await _event(database, ledger, "multi-a-old", "電動車 archive")
    second = await _event(database, ledger, "multi-b-old", "電動車 archive", group_id="2002")
    outside, _ = await seed_event(
        ledger,
        bot_user_id="8000",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="10001",
        direction="inbound",
        platform_message_id="multi-outside",
        content="電動車 archive",
    )
    wrong_author = await _event(
        database,
        ledger,
        "multi-wrong-author",
        "電動車 archive",
        group_id="2002",
        sender_user_id="10002",
    )
    third = await _event(database, ledger, "multi-b-new", "電動車 archive", group_id="2002")
    last = await _event(database, ledger, "multi-a-new", "電動車 archive")
    conversations = (first.canonical_conversation_id, second.canonical_conversation_id)
    statements = []

    def observe(_c, _cur, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", observe)
    try:
        page = await ledger.query_history(
            conversation_id=conversations,
            keyword="电动车",
            person_id=first.author_person_id,
            limit=2,
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", observe)
    assert [row.id for row in page.events] == [last.id, third.id]
    assert page.has_more and page.next_cursor == third.id
    assert sum("chat_history_fts MATCH" in statement for statement in statements) == 1
    following = await ledger.query_history(
        conversation_id=conversations,
        keyword="电动车",
        person_id=first.author_person_id,
        limit=2,
        cursor=page.next_cursor,
    )
    assert [row.id for row in following.events] == [second.id, first.id]
    assert not following.has_more and following.next_cursor is None
    assert all(row.author_person_id == first.author_person_id for row in following.events)
    assert not {outside.id, wrong_author.id} & {row.id for row in (*page.events, *following.events)}
    oldest = await ledger.query_history(
        conversation_id=conversations,
        keyword="电动车",
        person_id=first.author_person_id,
        order="oldest",
        limit=2,
        cursor=first.id,
    )
    assert [row.id for row in oldest.events] == [second.id, third.id]
    assert oldest.has_more and oldest.next_cursor == third.id
    assert not (await ledger.query_history(conversation_id=(), keyword="电动车")).events


async def test_multi_conversation_media_hits_keep_each_neighbors_in_its_original_scene(database):
    ledger = EventLedgerRepository(database)
    first_context = await _event(database, ledger, "scene-a-before", "first context")
    second_context = await _event(
        database, ledger, "scene-b-before", "second context", group_id="2002"
    )
    first = await _event(
        database,
        ledger,
        "scene-a-hit",
        "电动车",
        segments=({"type": "image", "data": {}},),
    )
    outside, _ = await seed_event(
        ledger,
        bot_user_id="8000",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="10001",
        direction="inbound",
        platform_message_id="scene-outside",
        content="电动车",
        segments=({"type": "image", "data": {}},),
    )
    second = await _event(
        database,
        ledger,
        "scene-b-hit",
        "电动车",
        group_id="2002",
        segments=({"type": "image", "data": {}},),
    )
    first_tail = await _event(database, ledger, "scene-a-after", "first tail")
    second_tail = await _event(database, ledger, "scene-b-after", "second tail", group_id="2002")
    page = await ledger.query_history(
        conversation_id=(first.canonical_conversation_id, second.canonical_conversation_id),
        keyword="电动车",
        has=("image",),
        context_before=1,
        context_after=1,
    )
    assert [row.id for row in page.events] == [second.id, first.id]
    assert [row.id for row in page.context] == [
        first_context.id,
        second_context.id,
        first_tail.id,
        second_tail.id,
    ]
    assert outside.id not in {row.id for row in (*page.events, *page.context)}


@pytest.mark.asyncio
async def test_asr_only_matches_return_original_segment_and_context_has_no_hit_filters(database):
    ledger = EventLedgerRepository(database)
    first = await _event(database, ledger, "context-old", "ordinary context")
    transcript = serialize_transcripts(
        (AudioTranscript("current", 3, "電動車"), AudioTranscript("reply", 1, "引用独有"))
    )
    center = await _event(database, ledger, "asr-center", "", audio_transcript=transcript)
    last = await _event(database, ledger, "context-new", "other context")
    page = await ledger.query_history(
        conversation_id=center.canonical_conversation_id,
        keyword="电动车",
        context_before=1,
        context_after=1,
    )
    assert [row.id for row in page.events] == [center.id]
    assert page.events[0].audio_transcript == transcript
    assert page.matched_fields == {center.id: ("audio_transcript:3",)}
    assert [row.id for row in page.context] == [first.id, last.id]
    assert not (
        await ledger.query_history(
            conversation_id=center.canonical_conversation_id, keyword="引用独有"
        )
    ).events


@pytest.mark.asyncio
async def test_metadata_uses_original_author_current_family_and_own_media(database):
    ledger = EventLedgerRepository(database)
    early = datetime(2026, 9, 1, tzinfo=UTC)
    first = await _event(
        database,
        ledger,
        "metadata-a",
        "a",
        occurred_at=early,
        segments=({"type": "record", "data": {}},),
    )
    last = await _event(
        database,
        ledger,
        "metadata-b",
        "b",
        sender_user_id="10002",
        occurred_at=early + timedelta(hours=1),
        segments=({"type": "audio", "data": {}},),
    )
    async with database.immediate_session() as writer:
        family = await writer.get(CanonicalPersonModel, last.author_person_id)
        family.merged_into_person_id = first.author_person_id
        writer.add(
            ConversationMediaItemModel(
                source_event_id=first.id,
                attachment_index=3,
                conversation_id=first.canonical_conversation_id,
                generation=1,
                segment_index=2,
                kind="image",
                source_snapshot_json='{"type":"image"}',
                display_name="quoted",
                created_at=early,
                cache_status="expired",
            )
        )
    page = await ledger.query_history(
        conversation_id=first.canonical_conversation_id,
        person_id=first.author_person_id,
        has=("audio",),
        keyword="a b",
        match="any",
    )
    assert [row.id for row in page.events] == [last.id, first.id]
    assert page.events[0].author_person_id == family.id
    assert not (
        await ledger.query_history(conversation_id=first.canonical_conversation_id, has=("image",))
    ).events
    dated = await ledger.query_history(
        conversation_id=first.canonical_conversation_id,
        start_at=datetime(2026, 9, 1, 8),
        end_at=datetime(2026, 9, 1, 9),
        keyword="a",
    )
    assert [row.id for row in dated.events] == [first.id]


@pytest.mark.asyncio
async def test_around_reads_permanent_neighbors_and_queries_remain_readonly(database):
    ledger = EventLedgerRepository(database)
    rows = [await _event(database, ledger, f"around-{i}", str(i)) for i in range(4)]
    conversation = rows[0].canonical_conversation_id
    async with database.immediate_session() as writer:
        source = await writer.get(CanonicalConversationModel, conversation)
        source.starts_after_event_id = rows[1].id
        source.generation += 1
    center, before, after = await ledger.history_around(
        conversation_id=conversation,
        event_id=rows[0].id,
        before=8,
        after=8,
    )
    assert center.id == rows[0].id and not before
    assert [row.id for row in after] == [rows[1].id, rows[2].id, rows[3].id]
    assert (
        await ledger.history_around(
            conversation_id=conversation,
            event_id=rows[1].id,
            before=0,
            after=0,
        )
    )[0].id == rows[1].id
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.strip().upper())

    event.listen(database.engine.sync_engine, "before_cursor_execute", record)
    try:
        async with database.read_session() as session:
            person = await session.get(CanonicalPersonModel, rows[0].author_person_id)
            person.display_name = "must not flush"
            await ledger.query_history(session=session, conversation_id=conversation, limit=1)
        assert not any(
            sql.startswith(("INSERT", "UPDATE", "DELETE", "BEGIN IMMEDIATE")) for sql in statements
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", record)


@pytest.mark.asyncio
async def test_cancelling_actual_sqlite_work_releases_reader_and_allows_writer(database):
    import asyncio
    import threading

    ledger = EventLedgerRepository(database)
    original = await _event(database, ledger, "cancel-source", "source")
    started = threading.Event()

    async def long_query():
        async with database.read_session() as session:
            connection = await session.connection()
            raw = await connection.get_raw_connection()
            driver = raw.driver_connection

            def signal():
                started.set()
                return 1

            await driver.create_function("history_query_started", 0, signal)
            await session.execute(text("BEGIN"))
            await ledger.query_history(
                session=session,
                conversation_id=original.canonical_conversation_id,
                predicates=(
                    text(
                        "(WITH RECURSIVE long_read(x) AS ("
                        "SELECT history_query_started() UNION ALL "
                        "SELECT x+1 FROM long_read WHERE x<1000000000) "
                        "SELECT sum(x) FROM long_read)>0"
                    ),
                ),
            )

    query = asyncio.create_task(long_query())
    assert await asyncio.to_thread(started.wait, 5), "real SQLite query did not start"
    query.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(query, 2)
    # Completing the cancelled coroutine also closed its read snapshot. The
    # next writer can commit and the same pool can execute another real read.
    async with database.immediate_session() as writer:
        await writer.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == original.id)
            .values(sender_nickname="after cancel")
        )
    page = await ledger.query_history(conversation_id=original.canonical_conversation_id)
    assert page.events[0].sender_nickname == "after cancel"


@pytest.mark.asyncio
async def test_oldest_cursor_never_misses_new_event_after_deleted_maximum(database):
    ledger = EventLedgerRepository(database)
    first = await _event(database, ledger, "immutable-first", "first")
    removed = await _event(database, ledger, "immutable-removed", "removed")
    async with database.immediate_session() as writer:
        await writer.execute(delete(ChatEventModel).where(ChatEventModel.id == removed.id))
    newest = await _event(database, ledger, "immutable-new", "new")
    assert newest.id > removed.id
    page = await ledger.query_history(
        conversation_id=first.canonical_conversation_id,
        cursor=removed.id,
        order="oldest",
    )
    assert [row.id for row in page.events] == [newest.id]


@pytest.mark.asyncio
async def test_merged_family_text_query_keeps_original_authors_and_separate_conversations(database):
    from tests.support.person_merge import merge_persons

    ledger = EventLedgerRepository(database)
    rows = [
        await _event(database, ledger, f"family-{i}", "同族稀有证据", sender_user_id=sender)
        for i, sender in enumerate(("10001", "10002", "1001"))
    ]
    other, _ = await seed_event(
        ledger,
        bot_user_id="8000",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="10002",
        direction="inbound",
        platform_message_id="family-other-entry",
        content="同族稀有证据",
    )
    first, middle, root = (row.author_person_id for row in rows)
    async with database.immediate_session() as session:
        await merge_persons(session, first, middle)
    async with database.immediate_session() as session:
        await merge_persons(session, middle, root)
    for person in (first, middle, root):
        page = await ledger.query_history(
            conversation_id=rows[0].canonical_conversation_id,
            keyword="同族稀有证据",
            person_id=person,
            order="oldest",
        )
        assert [row.id for row in page.events] == [row.id for row in rows]
        assert [row.author_person_id for row in page.events] == [first, middle, root]
        assert other.id not in {row.id for row in page.events}


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-10-21T23:30:00.123456-05:00", "2026-10-22T04:30:00.123458Z"),
        ("2026-10-22T04:30:00.123456Z", "2026-10-22T12:30:00.123458+08:00"),
        ("2026-10-22T12:30:00.123456", "2026-10-22T12:30:00.123458"),
    ],
)
async def test_date_offsets_preserve_microseconds_and_half_open_interval(database, start, end):
    ledger = EventLedgerRepository(database)
    beginning = datetime(2026, 10, 22, 4, 30, 0, 123456, tzinfo=UTC)
    rows = [
        await _event(
            database,
            ledger,
            f"microsecond-{offset}",
            "date evidence",
            occurred_at=beginning + timedelta(microseconds=offset),
        )
        for offset in (-1, 0, 1, 2)
    ]
    request = HistoryQuery.model_validate({"start_at": start, "end_at": end})
    assert request.start_at == beginning
    assert request.end_at == beginning + timedelta(microseconds=2)
    page = await ledger.query_history(
        conversation_id=rows[0].canonical_conversation_id,
        start_at=request.start_at,
        end_at=request.end_at,
        order="oldest",
    )
    assert [row.id for row in page.events] == [rows[1].id, rows[2].id]
    assert [event_view(row)["occurred_at"] for row in page.events] == [
        "2026-10-22T12:30:00.123456+08:00",
        "2026-10-22T12:30:00.123457+08:00",
    ]


async def test_live_page_continuation_accepts_new_asr_without_replaying_newer_rows(database):
    ledger = EventLedgerRepository(database)
    awaiting = await _event(
        database,
        ledger,
        "awaiting-asr",
        "",
        segments=({"type": "audio", "data": {}},),
    )
    middle = await _event(database, ledger, "live-middle", "电动车")
    newest = await _event(database, ledger, "live-newest", "电动车")
    conversation = newest.canonical_conversation_id
    first = await ledger.query_history(conversation_id=conversation, keyword="电动车", limit=1)
    assert [row.id for row in first.events] == [newest.id]
    assert first.has_more and first.next_cursor == newest.id
    async with database.read_session() as reader:
        source = await reader.get(CanonicalConversationModel, conversation)
        generation = source.generation
    assert await ledger.set_audio_transcript(
        awaiting.id,
        serialize_transcripts((AudioTranscript("current", 0, "電動車"),)),
        generation=generation,
        expected_source=awaiting,
    )
    appended = await _event(database, ledger, "live-added-after-page", "电动车")
    async with database.immediate_session() as writer:
        removed_keeper = await writer.get(ChatEventModel, middle.id)
        removed_keeper.utterance_fingerprint = "accepted-duplicate-fingerprint"
        removed_keeper.suppression_status = "duplicate"
    following = await ledger.query_history(
        conversation_id=conversation, keyword="电动车", cursor=first.next_cursor, limit=1
    )
    assert [row.id for row in following.events] == [awaiting.id]
    assert following.matched_fields == {awaiting.id: ("audio_transcript:0",)}
    assert not following.has_more
    assert appended.id > first.next_cursor
    assert [row.id for row in first.events] == [newest.id]
    after_anchor = await ledger.query_history(
        conversation_id=conversation,
        keyword="电动车",
        cursor=first.next_cursor,
        after_event_id=awaiting.id,
    )
    assert not after_anchor.events and not after_anchor.has_more


async def test_distant_hits_merge_overlapping_context_without_returning_the_middle(database):
    ledger = EventLedgerRepository(database)
    hits = {2, 3, 46}
    rows = [
        await _event(
            database,
            ledger,
            f"window-{index}",
            "命中" if index in hits else f"context {index}",
        )
        for index in range(50)
    ]
    page = await ledger.query_history(
        conversation_id=rows[0].canonical_conversation_id,
        keyword="命中",
        context_before=2,
        context_after=2,
    )
    assert [row.id for row in page.events] == [rows[index].id for index in (46, 3, 2)]
    expected_context = (0, 1, 4, 5, 44, 45, 47, 48)
    assert [row.id for row in page.context] == [rows[index].id for index in expected_context]
    assert set(page.matched_fields) == {rows[index].id for index in hits}
    assert len({row.id for row in (*page.events, *page.context)}) == 11
