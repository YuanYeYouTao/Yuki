"""All automation history remains paged and bound to its original internal owner."""

from datetime import UTC, datetime, timedelta

import pytest
from tests.support.control_automation_authority_helpers import automation_service, group_script
from tests.support.control_plane_foundation_helpers import context
from tests.support.social_identity_cases import social_env

from qq_ai_bot.control_plane import (
    ControlCommand,
    ControlCommandService,
)
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
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
