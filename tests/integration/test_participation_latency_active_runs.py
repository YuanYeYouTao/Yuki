"""Real SQLite/repository active-run discovery under unrelated terminal history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import event
from tests.integration.test_participation_latency_commands import (
    latency_evidence as latency_evidence,
)
from tests.support.autonomy_repository_helpers import _enable, _scene

from yuki.conversation.autonomy_db_models import InitiativeRunModel
from yuki.conversation.autonomy_repository import AutonomyRepository


async def test_active_discovery_preserves_order_without_scanning_unrelated_history(
    database, latency_evidence
):
    scene = await _scene(database)
    repository = AutonomyRepository(database)
    binding = await _enable(repository, scene)
    accepted = await repository.accept_host_proposal(
        proposal_id="latency-accepted-original",
        binding=binding,
        owner=binding.effective_owner,
        space_id=scene.space,
        presence_id=scene.presence,
        sources=(),
        trigger_kind="intrinsic",
    )
    assert accepted.outcome == "accepted" and accepted.run is not None
    now = datetime.now(UTC)
    rows = []
    for index in range(514):
        is_active = index in (0, 513)
        rows.append(
            InitiativeRunModel(
                id=f"latency-run-{index:04d}",
                proposal_id=f"latency-proposal-{index:04d}",
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
                state=("running" if index == 0 else "accepted") if is_active else "completed",
                feedback_sequence=0,
                created_at=now + timedelta(seconds=-60 if index == 0 else 60 if is_active else 0),
                updated_at=now,
            )
        )
    async with database.immediate_session() as writer:
        writer.add_all(rows)

    statements = []

    def capture(_connection, _cursor, statement, parameters, _context, _executemany):
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "autonomy_initiative_runs" in statement
        ):
            statements.append((statement, parameters))

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        active = await repository.list_active()
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert [run.run_id for run in active] == [
        "latency-run-0000",
        accepted.run.run_id,
        "latency-run-0513",
    ]
    assert statements
    plans = []
    async with database.read_session() as reader:
        connection = await reader.connection()
        for statement, parameters in statements:
            result = await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters)
            plans.extend(str(row[3]) for row in result.fetchall())
    full_scans = [
        detail
        for detail in plans
        if "SCAN autonomy_initiative_runs" in detail and "USING" not in detail
    ]
    latency_evidence.update(
        scenario="active outbox with unrelated terminal history",
        boundary="real repository and isolated SQLite schema; history rows are synthetic",
        terminal_rows=512,
        active_rows=len(active),
        actual_sql_count=len(statements),
        actual_sql_parameters=[
            list(parameters) if isinstance(parameters, tuple) else parameters
            for _, parameters in statements
        ],
        query_plan=plans,
        full_unindexed_scan=bool(full_scans),
        order_and_identity_preserved=True,
    )
    # No index name or function call count is required; scanning all terminal rows is avoidable.
    assert not full_scans, "active-run discovery scanned the whole terminal history table"
