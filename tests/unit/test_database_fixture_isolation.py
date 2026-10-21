"""Fast database fixtures keep real schema, independent state and SQL integrity."""

import sqlite3
import stat
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError
from tests.support.event_seed import seed_scoped_event

from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel
from yuki.identity.write_settings import (
    IdentityWriteSettings,
    configure_identity_write_settings,
    identity_write_settings,
)
from yuki.persistence.history_search import compile_match, prepare_query
from yuki.persistence.models import ChatEventModel, MemoryFactModel
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


async def test_database_copy_cannot_mutate_the_closed_readonly_template(
    database, _database_template
):
    template_uri = f"file:{_database_template.as_posix()}?mode=ro"
    assert database.engine.url.database != str(_database_template)
    assert not _database_template.stat().st_mode & stat.S_IWUSR
    assert not _database_template.with_name(_database_template.name + "-wal").exists()
    assert identity_write_settings().ignored_bot_users == frozenset()
    configure_identity_write_settings(IdentityWriteSettings(frozenset({"fixture-only-bot"})))
    with sqlite3.connect(template_uri, uri=True) as template:
        original = template.execute("SELECT id,enabled FROM persons ORDER BY id LIMIT 1").fetchone()
        expected_schema = template.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
    async with database.sessions() as reader:
        actual_schema = (
            await reader.execute(
                text("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")
            )
        ).all()
        person = await reader.get(CanonicalPersonModel, original[0])
        assert person.enabled == bool(original[1])
        assert [tuple(row) for row in actual_schema] == expected_schema
    async with database.immediate_session() as writer:
        person = await writer.get(CanonicalPersonModel, original[0])
        person.enabled = not person.enabled
    with sqlite3.connect(template_uri, uri=True) as template:
        assert template.execute(
            "SELECT enabled FROM persons WHERE id=?", (original[0],)
        ).fetchone() == (original[1],)
    async with database.sessions() as reader:
        assert (await reader.get(CanonicalPersonModel, original[0])).enabled != bool(original[1])


async def test_copied_database_uses_fresh_connection_foreign_key_enforcement(database):
    assert identity_write_settings().ignored_bot_users == frozenset()
    async with database.sessions() as reader:
        assert await reader.scalar(text("PRAGMA foreign_keys")) == 1
        assert await reader.scalar(text("PRAGMA journal_mode")) == "wal"
        person_id = await reader.scalar(select(CanonicalPersonModel.id).limit(1))
        space_id = await reader.scalar(select(CanonicalSpaceModel.id).limit(1))
        assert (
            await reader.scalar(
                select(CanonicalPersonModel.id)
                .where(CanonicalPersonModel.enabled.is_(False))
                .limit(1)
            )
            is None
        )
    insert_membership = text(
        "INSERT INTO memberships(canonical_person_id,canonical_space_id,"
        "first_seen_at,last_seen_at) VALUES(:person,:space,'2026-10-20','2026-10-20')"
    )
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        async with database.immediate_session() as writer:
            await writer.execute(insert_membership, {"person": "missing-person", "space": space_id})
    async with database.immediate_session() as writer:
        await writer.execute(insert_membership, {"person": person_id, "space": space_id})
    async with database.sessions() as reader:
        assert (
            await reader.scalar(
                text(
                    "SELECT canonical_person_id FROM memberships WHERE canonical_person_id=:person"
                ),
                {"person": person_id},
            )
            == person_id
        )


async def test_copied_fts_triggers_follow_original_insert_edit_and_delete(database):
    writer = ScopedEventLedgerUnitOfWork(database)
    original = await seed_scoped_event(
        writer,
        scope=ConversationScope.group("8000", "2001"),
        platform_message_id="fixture-fts-original",
        sender_user_id="1001",
        direction="inbound",
        content="astronomy-original",
    )
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        fact = MemoryFactModel(
            scope_type="person",
            canonical_subject_person_id=original.event.author_person_id,
            kind="fact",
            memory_key="fixture:fts",
            category="test",
            content="astronomy-memory",
            normalized_content="astronomy-memory",
            source_type="explicit",
            created_at=now,
            updated_at=now,
        )
        session.add(fact)
        await session.flush()
        fact_id = fact.id
    for search, expected in (("astronomy", True), ("geology", False)):
        async with database.sessions() as reader:
            assert (
                bool(
                    await reader.scalar(
                        text(
                            "SELECT rowid FROM chat_history_fts WHERE chat_history_fts MATCH :query"
                        ),
                        {
                            "query": compile_match(
                                original.event.canonical_conversation_id, prepare_query(search)
                            )
                        },
                    )
                )
                is expected
            )
            assert (
                bool(
                    await reader.scalar(
                        text(
                            "SELECT rowid FROM memory_facts_fts WHERE memory_facts_fts MATCH :query"
                        ),
                        {"query": search},
                    )
                )
                is expected
            )
    await writer.update_platform_message(
        original.event.id,
        InboundMessage(
            message_id="fixture-fts-original",
            event_type="message",
            scope_type=ScopeType.GROUP,
            sender=SenderIdentity("1001"),
            text="geology-edited",
            bot_user_id="8000",
            group_id="2001",
        ),
        presence_id=original.event.ingress_presence_id,
        edited_at=now,
        revision=1,
    )
    async with database.immediate_session() as session:
        await session.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == fact_id)
            .values(content="geology-memory")
        )
    searches = (
        (
            "chat_history_fts",
            lambda word: compile_match(
                original.event.canonical_conversation_id, prepare_query(word)
            ),
        ),
        ("memory_facts_fts", lambda word: word),
    )
    async with database.sessions() as reader:
        for table, query in searches:
            assert (
                await reader.scalar(
                    text(f"SELECT rowid FROM {table} WHERE {table} MATCH :query"),
                    {"query": query("astronomy")},
                )
                is None
            )
            assert (
                await reader.scalar(
                    text(f"SELECT rowid FROM {table} WHERE {table} MATCH :query"),
                    {"query": query("geology")},
                )
                is not None
            )
    async with database.immediate_session() as session:
        await session.execute(delete(ChatEventModel).where(ChatEventModel.id == original.event.id))
        await session.execute(delete(MemoryFactModel).where(MemoryFactModel.id == fact_id))
    async with database.sessions() as reader:
        for table, query in searches:
            assert (
                await reader.scalar(
                    text(f"SELECT rowid FROM {table} WHERE {table} MATCH :query"),
                    {"query": query("geology")},
                )
                is None
            )
