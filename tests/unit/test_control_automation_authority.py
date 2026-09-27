"""Control operators administer real owners without acquiring their identity."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from tests.conftest import make_settings
from tests.support.social_identity_cases import social_env
from tests.unit.test_automation_runtime import FakeClock, _script
from tests.unit.test_control_plane_foundation import context

from qq_ai_bot.automation.authority import PermissionLevel
from qq_ai_bot.automation.models import AutomationStatus
from qq_ai_bot.automation.registry import build_capability_registry
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.control_plane import ControlCommand, ControlCommandError, ControlCommandService
from qq_ai_bot.domain.identity import PersonId, RequestId
from qq_ai_bot.identity.canonical_repository import ensure_person
from qq_ai_bot.identity.db_models import IdentityBindingModel
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.time.service import TimeContextService


def automation_service(database):
    return AutomationService(
        settings=make_settings(database.url, automation_enabled=True, superusers=("9999",)),
        repository=AutomationRepository(database),
        registry=build_capability_registry(),
        time_service=TimeContextService(database, clock=FakeClock(datetime.now(UTC))),
    )


def group_script():
    script = _script().model_dump(mode="json")
    script["context"] = {"scene": "current_group"}
    script["steps"][0]["arguments"].pop("target")
    return script


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_kind", ["person", "self"])
async def test_control_create_edit_and_transition_preserve_owner_and_scene(
    database, tmp_path, owner_kind
):
    env = await social_env(database, tmp_path)
    env.bot.calls.clear()
    service = automation_service(database)
    commands = ControlCommandService(ControlCommandAdapter(database, automation=service))
    ctx = context("control.automation.mutate")
    # The operator has no QQ account or canonical Person at all.
    ctx = replace(ctx, principal=replace(ctx.principal, person_id=None))
    owner = "self" if owner_kind == "self" else env.person
    script = group_script()
    command = ControlCommand(
        request_id=ctx.request_id,
        expected_revision=0,
        payload={
            "action": "create",
            "spec": {
                "script": script,
                "owner_id": owner,
                "conversation_id": env.context.conversation_id,
            },
        },
    )
    created = await commands.mutate_automation(ctx, command)
    assert await commands.mutate_automation(ctx, command) == created
    repository = AutomationRepository(database)
    row = await repository.get(int(created.resource_id))
    assert row.creator_kind == owner_kind
    assert row.bot_user_id == env.bot.self_id
    assert row.creator_user_id == ("" if owner_kind == "self" else "10001")
    assert row.created_from_message_id == ""
    assert row.canonical_creator_person_id == (None if owner_kind == "self" else env.person)
    assert row.authority_snapshot["canonical_conversation_id"] == env.context.conversation_id
    assert row.authority_snapshot["current_group_id"] == "20001"
    assert row.authority_snapshot["permission_level"] == (
        PermissionLevel.SELF.value if owner_kind == "self" else PermissionLevel.USER.value
    )
    script["name"] = "edited"
    current = created
    for action, spec in (("update", script), ("pause", None), ("resume", None), ("run_now", None)):
        ctx = replace(ctx, request_id=RequestId.new())
        payload = {"action": action, "resource_id": current.resource_id}
        if spec is not None:
            payload["spec"] = spec
        current = await commands.mutate_automation(
            ctx,
            ControlCommand(
                request_id=ctx.request_id,
                expected_revision=current.revision,
                payload=payload,
            ),
        )
        row = await repository.get(int(current.resource_id))
        assert row.creator_kind == owner_kind
        assert row.canonical_target_space_id == env.space
        assert row.authority_snapshot["canonical_conversation_id"] == env.context.conversation_id
        assert row.authority_snapshot["current_group_id"] == "20001"
    assert row.status is AutomationStatus.ACTIVE and row.next_run_at is not None
    assert env.bot.calls == []  # control schedules; it never delivers by itself


@pytest.mark.asyncio
async def test_control_rejects_ambiguous_owner_binding_without_creating_task(database, tmp_path):
    env = await social_env(database, tmp_path)
    async with database.immediate_session() as session:
        other = await ensure_person(session, "10002")
        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.person_id == other)
        )
        binding.person_id = env.person
    ctx = context("control.automation.mutate")
    ctx = replace(ctx, principal=replace(ctx.principal, person_id=PersonId.parse(env.person)))
    service = automation_service(database)
    commands = ControlCommandService(ControlCommandAdapter(database, automation=service))
    with pytest.raises(ControlCommandError):
        await commands.mutate_automation(
            ctx,
            ControlCommand(
                request_id=ctx.request_id,
                expected_revision=0,
                payload={
                    "action": "create",
                    "spec": {
                        "script": group_script(),
                        "conversation_id": env.context.conversation_id,
                    },
                },
            ),
        )
    assert await AutomationRepository(database).active_count() == 0
