"""All automation history remains paged and bound to its original internal owner."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event
from tests.support.social_identity_cases import social_env
from tests.unit.test_control_automation_authority import automation_service, group_script
from tests.unit.test_control_plane_foundation import context

from qq_ai_bot.control_plane import (
    ControlCommand,
    ControlCommandService,
    ControlQueryError,
    ControlQueryService,
    PageRequest,
    ProblemCode,
)
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.persistence.models import AutomationRunModel, AutomationStepRunModel


@pytest.fixture
async def histories(database, tmp_path):
    env = await social_env(database, tmp_path)
    commands = ControlCommandService(
        ControlCommandAdapter(database, automation=automation_service(database))
    )
    ids = []
    for i in range(2):
        ctx = context("control.automation.mutate")
        script = group_script()
        script["name"] = f"history-{i}"
        result = await commands.mutate_automation(
            ctx,
            ControlCommand(
                request_id=ctx.request_id,
                expected_revision=0,
                payload={
                    "action": "create",
                    "spec": {
                        "owner_id": "self",
                        "conversation_id": env.context.conversation_id,
                        "script": script,
                    },
                },
            ),
        )
        ids.append(int(result.resource_id))
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        runs = []
        for i in range(26):
            row = AutomationRunModel(
                automation_id=ids[0] if i < 25 else ids[1],
                scheduled_for=now + timedelta(seconds=i),
                actual_started_at=now,
                status="succeeded",
                idempotency_key=f"history-{i}",
                result_summary_json='{"private":"secret-result"}',
                created_at=now,
            )
            session.add(row)
            runs.append(row)
        await session.flush()
        for i in range(202):
            session.add(
                AutomationStepRunModel(
                    run_id=runs[24].id if i < 201 else runs[25].id,
                    step_id=f"step-{i}",
                    capability="social.send_message",
                    status="succeeded",
                    input_summary_json='{"private":"private-input"}',
                    output_summary_json='{"private":"private-output"}',
                    started_at=now,
                )
            )
    return ids[0], ids[1], runs[24].id, runs[25].id


async def test_complete_history_pages_no_private_columns_or_cross_owner_steps(database, histories):
    owner, _other, run, foreign = histories
    queries = ControlQueryService(ControlQueryAdapter(database))
    ctx = context("control.automation.read")
    statements = []

    def capture(_, __, sql, *args):
        statements.append(sql)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        first = await queries.list_automation_runs(ctx, PageRequest(limit=20), automation_id=owner)
        second = await queries.list_automation_runs(
            ctx, PageRequest(limit=20, cursor=first.next_cursor), automation_id=owner
        )
        cursor = None
        steps = []
        while True:
            page = await queries.list_automation_steps(
                ctx, PageRequest(limit=100, cursor=cursor), automation_id=owner, run_id=run
            )
            steps.extend(page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        with pytest.raises(ControlQueryError) as exc:
            await queries.list_automation_steps(
                ctx, PageRequest(), automation_id=owner, run_id=foreign
            )
        assert exc.value.problem.code is ProblemCode.NOT_FOUND
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert len(first.items) + len(second.items) == 25
    assert len(steps) == 201 and len({row.resource_id for row in steps}) == 201
    assert all(row.fields["run_id"] == run for row in steps)
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    assert not any(
        token in " ".join(statements)
        for token in (
            "authority_snapshot_json",
            "script_json",
            "result_summary_json",
            "input_summary_json",
            "output_summary_json",
            "creator_user_id",
            "bot_user_id",
        )
    )


async def test_history_cursor_binds_resource_owner_and_run(database, histories):
    owner, other, run, _ = histories
    queries = ControlQueryService(ControlQueryAdapter(database))
    ctx = context("control.automation.read")
    runs = await queries.list_automation_runs(ctx, PageRequest(limit=1), automation_id=owner)
    steps = await queries.list_automation_steps(
        ctx, PageRequest(limit=1), automation_id=owner, run_id=run
    )
    for method, cursor, args in (
        (queries.list_automation_runs, runs.next_cursor, {"automation_id": other}),
        (queries.list_automation_steps, runs.next_cursor, {"automation_id": owner}),
        (
            queries.list_automation_steps,
            steps.next_cursor,
            {"automation_id": owner, "run_id": None},
        ),
    ):
        with pytest.raises(ControlQueryError) as exc:
            await method(ctx, PageRequest(cursor=cursor), **args)
        assert exc.value.problem.code is ProblemCode.VALIDATION_ERROR
    with pytest.raises(ControlQueryError) as exc:
        await queries.list_automation_runs(
            context("control.execution.metadata.read"), PageRequest(), automation_id=owner
        )
    assert exc.value.problem.code is ProblemCode.CAPABILITY_DENIED


@pytest.mark.parametrize("identity", [True, 0, -1, "123", 2**63])
async def test_history_requires_internal_bounded_integer(database, identity):
    queries = ControlQueryService(ControlQueryAdapter(database))
    with pytest.raises(ControlQueryError) as exc:
        await queries.list_automation_runs(
            context("control.automation.read"), PageRequest(), automation_id=identity
        )
    assert exc.value.problem.code is ProblemCode.VALIDATION_ERROR
