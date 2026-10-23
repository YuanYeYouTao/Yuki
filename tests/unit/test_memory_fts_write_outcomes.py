"""FTS maintenance retains its actual writer outcome through the Admin entry."""

import sqlite3

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from tests.conftest import make_settings
from tests.unit.test_memory_mutation import _service
from tests.unit.test_memory_rebuild import _admin

from yuki.admin.audit import AdminAuditService
from yuki.admin.models import AdminActor
from yuki.memory.errors import MemoryRetrievalError
from yuki.memory.fts import SQLiteMemoryFTSIndex
from yuki.persistence.database import write_failure_committed, write_failure_phase
from yuki.services.admin.memory_admin import MemoryAdminService


@pytest.mark.parametrize("phase", ["execute", "commit"])
async def test_admin_fts_rebuild_keeps_database_phase_and_original_error(database, phase):
    mutations, facts, ledger, _ = _service(database)
    admin = MemoryAdminService(
        settings=make_settings(database.url),
        memories=facts,
        audit=AdminAuditService(database),
        mutations=mutations,
        ledger=ledger,
    )
    actor = AdminActor(
        user_id="9000",
        person_id=await _admin(database),
        is_superuser=True,
        trigger_message_id="fts-rebuild",
        conversation_key="private:9000",
    )
    original = OperationalError("FTS rebuild", {}, sqlite3.OperationalError("lost database ACK"))
    rebuilding = False

    def execute(conn, cursor, sql, params, context, many):
        nonlocal rebuilding
        if sql.startswith("INSERT INTO memory_facts_fts(memory_facts_fts)"):
            rebuilding = True
            if phase == "execute":
                raise original

    def committed(session):
        if rebuilding and phase == "commit":
            raise original

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", execute)
    event.listen(Session, "after_commit", committed)
    try:
        with pytest.raises(OperationalError) as caught:
            await admin.rebuild_index(actor)
    finally:
        event.remove(engine, "before_cursor_execute", execute)
        event.remove(Session, "after_commit", committed)
    assert caught.value is original
    assert write_failure_phase(original) == phase
    assert write_failure_committed(original) is (False if phase == "execute" else None)
    assert (await admin.index_status(actor)).healthy


async def test_fts_reader_keeps_its_public_domain_error(database):
    async with database.immediate_session() as writer:
        await writer.execute(text("DROP TABLE memory_facts_fts"))
    with pytest.raises(MemoryRetrievalError) as caught:
        await SQLiteMemoryFTSIndex(database).health()
    assert caught.value.code == "memory_index_unavailable"
