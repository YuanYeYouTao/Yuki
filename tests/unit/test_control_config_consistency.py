"""Canonical configuration validation and one transaction across management entries."""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from tests.conftest import make_settings
from tests.unit.test_control_plane_foundation import context

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.control_plane import (
    ControlCommand,
    ControlCommandError,
    ControlCommandService,
    ProblemCode,
)
from qq_ai_bot.conversation.canonical_db_models import ControlCommandReceiptModel
from qq_ai_bot.domain.identity import PersonId, SpaceId
from qq_ai_bot.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import AdminOperationEventModel


async def setup(database):
    person, space = PersonId.new(), SpaceId.new()
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        for model, owner in ((CanonicalPersonModel, person), (CanonicalSpaceModel, space)):
            session.add(
                model(id=owner.text, revision=1, enabled=True, created_at=now, updated_at=now)
            )
    settings = make_settings(database.url, daily_chat_message_delay_max_seconds=10)
    runtime = RuntimeConfigService(settings=settings, database=database)
    await runtime.initialize()
    return runtime, person, space


async def set_value(runtime, key, value, scope="global", owner=""):
    return await runtime.set_override(
        key,
        value,
        scope_type=scope,
        scope_id=owner,
        actor_user_id="test-operator",
        trigger_message_id="",
    )


@pytest.mark.asyncio
async def test_cross_scope_validation_uses_canonical_owners(database: Database):
    runtime, person, space = await setup(database)
    assert (await set_value(runtime, "reply.delay_min_seconds", 5, "group", space.text)).success
    # A Person maximum must also be valid in the Space where that Person can speak.
    result = await set_value(runtime, "reply.delay_max_seconds", 4, "user", person.text)
    assert not result.success and result.error_category == "validation_error"
    assert (await set_value(runtime, "reply.delay_max_seconds", 6, "user", person.text)).success
    # Global changes must remain valid under the more specific Person maximum.
    assert (await set_value(runtime, "reply.delay_min_seconds", 7)).success is False
    actual = await runtime.snapshot(user_id=person.text, group_id=space.text)
    assert (actual.reply.delay_min_seconds, actual.reply.delay_max_seconds) == (5, 6)


@pytest.mark.asyncio
async def test_context_windows_are_hot_and_watermarks_validate_inherited_scopes(database):
    runtime, person, space = await setup(database)
    original = await runtime.snapshot(user_id=person.text, group_id=space.text)
    assert original.context.window_tokens == 96000
    assert (
        original.context.compaction_trigger_ratio,
        original.context.compaction_target_ratio,
    ) == (
        0.90,
        0.60,
    )
    assert (
        original.context.work_compaction_trigger_ratio,
        original.context.work_compaction_target_ratio,
    ) == (0.90, 0.50)
    assert (await set_value(runtime, "context.window_tokens", 160000)).success
    assert (
        await set_value(runtime, "context.work_window_tokens", 192000, "group", space.text)
    ).success
    assert (
        await set_value(runtime, "context.compaction_output_tokens", 4096, "user", person.text)
    ).success
    assert (await set_value(runtime, "context.compaction_trigger_ratio", 0.8)).success
    assert (
        await set_value(runtime, "context.compaction_target_ratio", 0.7, "user", person.text)
    ).success
    refused = await set_value(
        runtime, "context.compaction_trigger_ratio", 0.65, "group", space.text
    )
    assert not refused.success and refused.error_category == "validation_error"
    actual = await runtime.snapshot(user_id=person.text, group_id=space.text)
    assert (actual.context.window_tokens, actual.context.work_window_tokens) == (160000, 192000)
    assert actual.context.compaction_output_tokens == 4096
    assert (actual.context.compaction_trigger_ratio, actual.context.compaction_target_ratio) == (
        0.8,
        0.7,
    )
    assert original.context.window_tokens == 96000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "field"),
    [
        ("context.compaction_output_tokens", "compaction_output_tokens"),
        ("context.rollup_output_tokens", "rollup_output_tokens"),
    ],
)
async def test_summary_output_budget_above_old_ui_ceiling_is_saved_and_reloaded(
    database: Database, key, field
):
    runtime, person, space = await setup(database)
    original = await runtime.snapshot(user_id=person.text, group_id=space.text)
    changed = await set_value(runtime, key, 65536)
    assert changed.success and changed.change_id is not None
    actual = await runtime.snapshot(user_id=person.text, group_id=space.text)
    assert getattr(actual.context, field) == 65536
    assert getattr(original.context, field) != 65536

    reloaded = RuntimeConfigService(settings=make_settings(database.url), database=database)
    await reloaded.initialize()
    effective = await reloaded.get_effective(key, user_id=person.text, group_id=space.text)
    assert effective.value == 65536 and effective.source == "runtime:global"
    assert getattr((await reloaded.snapshot()).context, field) == 65536


@pytest.mark.asyncio
async def test_work_watermarks_are_hot_independent_and_validate_inherited_scopes(database):
    runtime, person, space = await setup(database)
    original = await runtime.snapshot(user_id=person.text, group_id=space.text)
    assert (await set_value(runtime, "context.work_compaction_trigger_ratio", 0.8)).success
    assert (
        await set_value(runtime, "context.work_compaction_target_ratio", 0.7, "user", person.text)
    ).success
    refused = await set_value(
        runtime, "context.work_compaction_trigger_ratio", 0.65, "group", space.text
    )
    assert not refused.success and refused.error_category == "validation_error"
    actual = await runtime.snapshot(user_id=person.text, group_id=space.text)
    assert (
        actual.context.work_compaction_trigger_ratio,
        actual.context.work_compaction_target_ratio,
    ) == (
        0.8,
        0.7,
    )
    assert (actual.context.compaction_trigger_ratio, actual.context.compaction_target_ratio) == (
        0.9,
        0.6,
    )
    assert (await set_value(runtime, "context.compaction_target_ratio", 0.4)).success
    changed = await runtime.snapshot(user_id=person.text, group_id=space.text)
    assert changed.context.compaction_target_ratio == 0.4
    assert changed.context.work_compaction_target_ratio == 0.7
    assert original.context.work_compaction_target_ratio == 0.5


@pytest.mark.asyncio
async def test_delete_and_rollback_revalidate_inherited_values(database: Database):
    runtime, _, space = await setup(database)
    assert (await set_value(runtime, "reply.delay_min_seconds", 5)).success
    lower = await set_value(runtime, "reply.delay_min_seconds", 1, "group", space.text)
    assert lower.success and lower.change_id is not None
    assert (await set_value(runtime, "reply.delay_max_seconds", 3, "group", space.text)).success
    for result in (
        await runtime.delete_override(
            "reply.delay_min_seconds",
            scope_type="group",
            scope_id=space.text,
            actor_user_id="test-operator",
            trigger_message_id="",
        ),
        await runtime.rollback(lower.change_id, actor_user_id="test-operator"),
    ):
        assert not result.success and result.error_category == "validation_error"
    actual = await runtime.snapshot(group_id=space.text)
    assert (actual.reply.delay_min_seconds, actual.reply.delay_max_seconds) == (1, 3)


@pytest.mark.asyncio
async def test_direct_config_and_control_command_share_writer_order(
    database: Database, monkeypatch
):
    runtime, _, _ = await setup(database)
    entered, release, control_loaded = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = runtime._repository.save_with_audit

    async def paused_save(**kwargs):
        if kwargs["actor"].user_id == "test-operator":
            entered.set()
            await release.wait()
        return await original(**kwargs)

    monkeypatch.setattr(runtime._repository, "save_with_audit", paused_save)
    adapter = ControlCommandAdapter(database, runtime_config=runtime)
    load = adapter._load_receipt

    async def observed_load(*args):
        control_loaded.set()
        return await load(*args)

    monkeypatch.setattr(adapter, "_load_receipt", observed_load)
    ctx = context("control.config.mutate")
    command = ControlCommand(
        request_id=ctx.request_id,
        expected_revision=0,
        payload={
            "key": "agent.max_model_requests",
            "scope_type": "global",
            "scope_id": "",
            "value": 20,
        },
    )
    direct = asyncio.create_task(set_value(runtime, "agent.max_model_requests", 19))
    await asyncio.wait_for(entered.wait(), 2)
    control = asyncio.create_task(ControlCommandService(adapter).set_config(ctx, command))
    try:
        # On the old chain Control acquired the writer while the direct entry held
        # the process lock. Releasing the direct entry then deadlocked both paths.
        try:
            await asyncio.wait_for(control_loaded.wait(), 0.1)
        except TimeoutError:
            pass
        release.set()
        outcomes = await asyncio.wait_for(
            asyncio.gather(direct, control, return_exceptions=True), 2
        )
        assert outcomes[0].success
        assert isinstance(outcomes[1], ControlCommandError)
        assert outcomes[1].problem.code is ProblemCode.VERSION_CONFLICT
        assert (await runtime.get_effective("agent.max_model_requests")).value == 19
    finally:
        release.set()
        for task in (direct, control):
            task.cancel()
        await asyncio.gather(direct, control, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["set", "unset", "rollback"])
@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
@pytest.mark.parametrize("entry", ["direct", "control"])
async def test_failure_after_config_write_aborts_instead_of_committing_failed_result(
    database: Database,
    monkeypatch,
    action,
    error_type,
    entry,
):
    from qq_ai_bot.admin import config_service

    runtime, _, _ = await setup(database)
    ctx = context("control.config.mutate")

    # Rollback ownership uses the real operator, including the domain audit.
    async def baseline(value):
        return await runtime.set_override(
            "agent.max_model_requests",
            value,
            scope_type="global",
            scope_id="",
            actor_user_id=ctx.principal.principal_id.text,
            trigger_message_id="",
        )

    assert (await baseline(19)).success
    latest = await baseline(20)
    assert latest.success and latest.change_id is not None
    audit_count = 2
    original = config_service.add_audit_event

    async def fail_after_flush(*args, **kwargs):
        row = await original(*args, **kwargs)
        if kwargs["success"]:
            raise error_type("injected after config and audit flush")
        return row

    monkeypatch.setattr(config_service, "add_audit_event", fail_after_flush)
    service = ControlCommandService(ControlCommandAdapter(database, runtime_config=runtime))
    payload = {"key": "agent.max_model_requests", "scope_type": "global", "scope_id": ""}
    if action == "set":
        payload["value"] = 21
        invoke = service.set_config
    elif action == "unset":
        invoke = service.unset_config
    else:
        payload = {"change_id": latest.change_id}
        invoke = service.rollback_config
    command = ControlCommand(request_id=ctx.request_id, expected_revision=2, payload=payload)
    with pytest.raises(error_type):
        if entry == "control":
            await invoke(ctx, command)
        elif action == "set":
            await baseline(21)
        elif action == "unset":
            await runtime.delete_override(
                "agent.max_model_requests",
                scope_type="global",
                scope_id="",
                actor_user_id=ctx.principal.principal_id.text,
                trigger_message_id="",
            )
        else:
            await runtime.rollback(latest.change_id, actor_user_id=ctx.principal.principal_id.text)
    value = (await runtime.inspect_configs(("agent.max_model_requests",)))[0]
    assert (value.effective.value, value.version) == (20, 2)
    async with database.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(AdminOperationEventModel))
            == audit_count
        )
        assert (
            await session.scalar(select(func.count()).select_from(ControlCommandReceiptModel)) == 0
        )
