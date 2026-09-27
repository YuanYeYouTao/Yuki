"""Schemas and maintenance revisions come from the original running domain."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event
from tests.unit.test_control_automation_authority import automation_service
from tests.unit.test_control_plane_foundation import context

from qq_ai_bot.automation.models import AutomationScript
from qq_ai_bot.control_plane import ControlQueryError, ControlQueryService, ProblemCode
from qq_ai_bot.control_plane.json_types import freeze_json_object
from qq_ai_bot.memory.rebuild.models import MemoryRebuildSelection
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.persistence.models import MemoryRebuildRunModel
from qq_ai_bot.persistence.unit_of_work import state_revision


async def test_read_schemas_reuse_native_models_without_execution(database):
    service = automation_service(database)
    q = ControlQueryService(ControlQueryAdapter(database, automation=service))
    result = await q.read_automation_schema(context("control.automation.read"))
    assert result.fields["script"] == freeze_json_object(AutomationScript.model_json_schema())
    names = {item["name"] for item in result.fields["capabilities"]}
    assert {"yuki.agent", "social.send_message", "workspace.write"} <= names
    assert "onebot.send_group_message" not in names
    selected = next(item for item in result.fields["capabilities"] if item["name"] == "yuki.agent")
    assert "self" in selected["permitted_levels"]
    assert (
        "self"
        not in next(
            item for item in result.fields["capabilities"] if item["name"] == "workspace.list"
        )["permitted_levels"]
    )
    assert "delivery_target" in selected["schema"]["properties"]
    result = await q.read_memory_maintenance_schema(context("control.memory.metadata.read"))
    assert result.fields["rebuild"] == freeze_json_object(
        MemoryRebuildSelection.model_json_schema()
    )
    with pytest.raises(ControlQueryError) as denied:
        await q.read_automation_schema(context("control.memory.metadata.read"))
    assert denied.value.problem.code == ProblemCode.CAPABILITY_DENIED
    with pytest.raises(ControlQueryError) as unavailable:
        await ControlQueryService(ControlQueryAdapter(database)).read_automation_schema(
            context("control.automation.read")
        )
    assert unavailable.value.problem.code == ProblemCode.OPERATION_UNAVAILABLE


async def test_maintenance_detail_exposes_original_revision_not_private_selection(database):
    now, identity = datetime.now(UTC), str(uuid4())
    async with database.immediate_session() as session:
        session.add(
            MemoryRebuildRunModel(
                public_id=identity,
                status="planned",
                selection_json='{"private":"selection"}',
                selection_hash="a" * 64,
                snapshot_max_event_id=0,
                snapshot_created_at=now,
                extraction_fingerprint="b" * 64,
                plan_statistics_json='{"private":"stats"}',
                created_at=now,
                updated_at=now,
            )
        )
    q = ControlQueryService(ControlQueryAdapter(database))
    sql = []

    def capture(_, __, statement, *args):
        sql.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        result = await q.read_memory_maintenance_run(
            context("control.memory.metadata.read"), f"rebuild:{identity}"
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert result.fields["plan_statistics_error"] == "invalid_original_statistics"
    assert "plan_statistics_json" not in result.fields and "plan_statistics" not in result.fields
    assert result.fields["revision"] == state_revision(now)
    assert result.fields["public_id"] == identity and result.fields["review_counts"] == {}
    assert not any(
        "selection_json" in statement or "created_by_user_id" in statement for statement in sql
    )
    with pytest.raises(ControlQueryError) as bad:
        await q.read_memory_maintenance_run(
            context("control.memory.metadata.read"), f"control:{identity}"
        )
    assert bad.value.problem.code == ProblemCode.VALIDATION_ERROR


async def test_dream_detail_uses_original_dream_columns_not_rebuild_statistics(database):
    from tests.unit.test_memory_dream import _empty_dream_statistics

    from qq_ai_bot.memory.dream.models import DreamRunMode
    from qq_ai_bot.memory.dream.repository import DreamRepository

    run = await DreamRepository(database).create_run(
        mode=DreamRunMode.FULL,
        statistics=_empty_dream_statistics(),
        clusters=(),
        snapshot_max_fact_id=0,
        actor_user_id=None,
        scheduled_slot=None,
    )
    view = await ControlQueryService(ControlQueryAdapter(database)).read_memory_maintenance_run(
        context("control.memory.metadata.read"), f"dream:{run.public_id}"
    )
    assert view.fields["plan_statistics"]["eligible_facts"] == 0
    assert view.fields["revision"] == state_revision(run.updated_at)
    assert view.fields["model_calls"] == 0
    assert "statistics_json" not in view.fields and "created_by_user_id" not in view.fields
