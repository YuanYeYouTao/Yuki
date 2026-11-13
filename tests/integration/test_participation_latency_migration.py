"""Real 0155 upgrade and original initiative outbox recovery on local SQLite."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from tests.integration.test_participation_latency_commands import (
    latency_evidence as latency_evidence,
)
from tests.support.autonomy_repository_helpers import _enable, _scene

from yuki.conversation.autonomy_db_models import InitiativeRunModel
from yuki.conversation.autonomy_repository import AutonomyRepository
from yuki.persistence.database import Database


async def test_old_outbox_upgrade_keeps_original_facts_and_active_lifecycle(
    tmp_path, monkeypatch, latency_evidence
):
    path = tmp_path / "upgrade.sqlite3"
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "0155")
    database = Database(url)
    try:
        scene = await _scene(database)
        repository = AutonomyRepository(database)
        binding = await _enable(repository, scene)
        accepted = []
        for index in range(3):
            admission = await repository.accept_host_proposal(
                proposal_id=f"synthetic-migration-proposal-{index}",
                binding=binding,
                owner=binding.effective_owner,
                space_id=scene.space,
                presence_id=scene.presence,
                sources=(),
                trigger_kind="intrinsic",
            )
            assert admission.outcome == "accepted" and admission.run is not None
            accepted.append(admission.run.run_id)
        now = datetime.now(UTC)
        async with database.immediate_session() as writer:
            writer.add_all(
                [
                    InitiativeRunModel(
                        id=f"terminal-{index:04d}",
                        proposal_id=f"terminal-proposal-{index:04d}",
                        conversation_id=scene.conversation,
                        generation=1,
                        owner=binding.effective_owner.value,
                        controller_epoch=binding.controller_epoch,
                        space_id=scene.space,
                        presence_id=scene.presence,
                        payload_hash="0" * 64,
                        sources_json="[]",
                        support_refs_json="[]",
                        trigger_kind="intrinsic",
                        state="completed",
                        feedback_sequence=0,
                        created_at=now - timedelta(seconds=index + 1),
                        updated_at=now,
                    )
                    for index in range(256)
                ]
            )
        assert [run.run_id for run in await repository.list_active()] == accepted
    finally:
        await database.close()

    with sqlite3.connect(path) as db:
        old_rows = db.execute("SELECT * FROM autonomy_initiative_runs ORDER BY id").fetchall()
        old_schema = db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0155",)
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "0156")
    with sqlite3.connect(path) as db:
        version = db.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        rows = db.execute("SELECT * FROM autonomy_initiative_runs ORDER BY id").fetchall()
        schema = db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        foreign_key_errors = db.execute("PRAGMA foreign_key_check").fetchall()
    added = [record for record in schema if record not in old_schema]
    latency_evidence.update(
        before_revision="0155",
        after_revision=version,
        original_rows=len(old_rows),
        same_rows=rows == old_rows,
        added_schema=[(entry[0], entry[2]) for entry in added],
        foreign_key_errors=len(foreign_key_errors),
    )
    assert version == "0156", "new active-outbox migration is absent"
    assert rows == old_rows and not foreign_key_errors
    assert len(added) == 1 and added[0][0] == "index" and added[0][2] == "autonomy_initiative_runs"

    database = Database(url)
    try:
        repository = AutonomyRepository(database)
        statements = []

        def capture(_connection, _cursor, sql, parameters, _context, _executemany):
            if sql.lstrip().upper().startswith("SELECT") and "autonomy_initiative_runs" in sql:
                statements.append((sql, parameters))

        event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
        try:
            assert [run.run_id for run in await repository.list_active()] == accepted
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
        async with database.read_session() as reader:
            connection = await reader.connection()
            plans = []
            for sql, parameters in statements:
                plans.extend(
                    row[3]
                    for row in (
                        await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, parameters)
                    ).all()
                )
        assert not any(
            "SCAN autonomy_initiative_runs" in plan and "USING" not in plan for plan in plans
        )
        await repository.record_feedback(accepted[0], sequence=1, outcome="running")
        assert [run.run_id for run in await repository.list_active()] == accepted
        await repository.record_feedback(accepted[0], sequence=2, outcome="completed")
        assert [run.run_id for run in await repository.list_active()] == accepted[1:]
        latency_evidence.update(query_plan=plans, running_preserved=True, terminal_removed=True)
    finally:
        await database.close()
    database = Database(url)
    try:
        repository = AutonomyRepository(database)
        assert [run.run_id for run in await repository.list_active()] == accepted[1:]
        settled = await repository.get_run(accepted[0])
        assert settled.state == "completed" and settled.feedback_sequence == 2
        latency_evidence["reopened_original_terminal_and_remaining_active"] = True
    finally:
        await database.close()
