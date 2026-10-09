"""Independent facts, explicit target corrections and the deployed 0101 upgrade."""

import asyncio
import sqlite3

from alembic import command
from alembic.config import Config
from tests.unit.test_memory_mutation import _context, _event, _service

from qq_ai_bot.memory.mutation.models import (
    MemoryMutationOperation,
    MemoryMutationRequest,
    MemoryMutationTarget,
)
from qq_ai_bot.persistence.schema_guard import canonical_schema_revision


async def test_same_key_create_preserves_originals_and_correct_changes_only_named_fact(database):
    mutations, facts, ledger, _ = _service(database)
    source = "我喜欢喝茶。" + "这是当时完整的真实说明。" * 60
    first = await _event(ledger, message_id="first", sender_user_id="1001", content=source)
    second = await _event(
        ledger, message_id="second", sender_user_id="1001", content="我喜欢喝可可"
    )
    request = MemoryMutationRequest(
        operation=MemoryMutationOperation.CREATE,
        target=MemoryMutationTarget(subject_ref="speaker", scope_type="person"),
        new_content="喜欢喝茶",
        memory_key="drink",
        category="preference",
        confidence=0.21,
    )
    a = await mutations.mutate(request, _context(first))
    b = await mutations.mutate(
        request.model_copy(update={"new_content": "喜欢喝可可", "confidence": 0.93}),
        _context(second),
    )
    assert a.ok and b.ok and a.new_fact_id != b.new_fact_id
    original_a = await facts.get_fact(a.new_fact_id)
    original_b = await facts.get_fact(b.new_fact_id)
    assert original_a.status.value == original_b.status.value == "active"
    assert original_a.confidence == 0.21 and original_b.confidence == 0.93
    assert original_a.conflict_state.value == original_b.conflict_state.value == "clear"
    assert [row.excerpt for row in await facts.list_evidence(original_a.id)] == [source]

    correction = await _event(
        ledger, message_id="correction", sender_user_id="1001", content="我更喜欢乌龙茶"
    )
    result = await mutations.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CORRECT,
            fact_id=a.new_fact_id,
            new_content="更喜欢乌龙茶",
            confidence=0.81,
        ),
        _context(correction),
    )
    assert result.ok and result.new_fact_id not in {a.new_fact_id, b.new_fact_id}
    assert (await facts.get_fact(a.new_fact_id)).status.value == "superseded"
    assert (await facts.get_fact(b.new_fact_id)).model_dump() == original_b.model_dump()
    updated = await facts.get_fact(result.new_fact_id)
    assert updated.confidence == 0.81 and updated.supersedes_id == a.new_fact_id
    assert {row.event_id for row in await facts.list_evidence(updated.id)} == {
        first.id,
        correction.id,
    }
    # Retrying this original request returns its original durable receipt.
    replay = await mutations.mutate(request, _context(first))
    assert replay.deduplicated and replay.new_fact_id == a.new_fact_id


async def test_upgrade_preserves_rows_and_allows_independent_same_key(tmp_path, monkeypatch):
    path = tmp_path / "deployed.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, config, "0101")
    columns = (
        "scope_type",
        "visibility_type",
        "kind",
        "memory_key",
        "category",
        "content",
        "normalized_content",
        "importance",
        "confidence",
        "source_type",
        "authority",
        "status",
        "conflict_state",
        "created_at",
        "updated_at",
        "last_confirmed_at",
        "validation_version",
        "review_state",
    )
    values = (
        "self",
        "global",
        "fact",
        "same-key",
        "free-label",
        "original",
        "original",
        2,
        0.31,
        "automatic",
        "agent_reflection",
        "active",
        "clear",
        "2026-01-01",
        "2026-01-01",
        "2026-01-01",
        "old",
        "verified",
    )
    sql = (
        f"INSERT INTO memory_facts ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})"
    )
    with sqlite3.connect(path) as db:
        db.execute(sql, values)
        db.commit()
        before = db.execute("SELECT * FROM memory_facts").fetchall()
        tables = set(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
    await asyncio.to_thread(command.upgrade, config, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (
            canonical_schema_revision(),
        )
        assert db.execute("SELECT * FROM memory_facts").fetchall() == before
        assert (
            set(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
            == tables
        )
        indexes = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert not any(name.startswith("uq_memory_facts_active_canonical_") for name in indexes)
        db.execute(sql, values)
        assert db.execute(
            "SELECT COUNT(*) FROM memory_facts WHERE memory_key='same-key'"
        ).fetchone() == (2,)
