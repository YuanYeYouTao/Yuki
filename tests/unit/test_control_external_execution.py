"""External control effects leave the writer and retain original request evidence."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.unit.test_control_plane_foundation import context

from qq_ai_bot.control_plane import (
    ControlCommand,
    ControlCommandError,
    ControlCommandService,
    ControlQueryService,
    OperationStatus,
    PageRequest,
    ProblemCode,
)
from qq_ai_bot.conversation.canonical_db_models import ControlCommandReceiptModel
from qq_ai_bot.identity.db_models import CanonicalPersonModel
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.unit_of_work import next_updated_at, state_revision
from qq_ai_bot.plugin_host.db_models import PluginInstallationModel
from yuki_plugin_sdk.api import PLUGIN_API_VERSION


class ControlledPlugin:
    def __init__(self, database: Database, *, fail_after_effect: bool = False) -> None:
        self.database = database
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.calls = 0
        self.fail_after_effect = fail_after_effect

    async def _manage(self, plugin_id: str, *, action: str, expected_revision: int):
        self.calls += 1
        self.entered.set()
        await self.release.wait()  # stands for a network/lifecycle wait
        async with self.database.immediate_session() as session:
            row = await session.get(PluginInstallationModel, plugin_id)
            assert row is not None and state_revision(row.updated_at) == expected_revision
            row.enabled = action == "enable"
            row.status = "approved" if row.enabled else "disabled"
            row.updated_at = next_updated_at(row.updated_at)
            result = SimpleNamespace(status=row.status, updated_at=row.updated_at)
        if self.fail_after_effect:
            raise RuntimeError("effect persisted but reply unavailable")
        return result

    async def disable(self, plugin_id, *, actor_user_id, expected_revision):
        return await self._manage(plugin_id, action="disable", expected_revision=expected_revision)

    async def enable(self, plugin_id, *, actor_user_id, expected_revision):
        return await self._manage(plugin_id, action="enable", expected_revision=expected_revision)


async def setup(database: Database, *, fail_after_effect: bool = False):
    stamp = datetime.now(UTC)
    async with database.immediate_session() as session:
        session.add(
            PluginInstallationModel(
                plugin_id="probe",
                name="Probe",
                version="1.0.0",
                plugin_api=PLUGIN_API_VERSION,
                yuki_requires=">=0",
                manifest_hash="0" * 64,
                entrypoint="probe.py:Plugin",
                enabled=True,
                status="approved",
                discovered_at=stamp,
                updated_at=stamp,
            )
        )
    plugin = ControlledPlugin(database, fail_after_effect=fail_after_effect)
    adapter = ControlCommandAdapter(database, plugins=plugin)
    service = ControlCommandService(adapter)
    ctx = context("control.plugin.mutate", "control.operation.read", "control.audit.read")
    command = ControlCommand(
        request_id=ctx.request_id,
        expected_revision=state_revision(stamp),
        payload={"action": "disable", "resource_id": "probe"},
    )
    return plugin, adapter, service, ctx, command


@pytest.mark.asyncio
async def test_network_wait_has_no_writer_and_concurrent_reentry_is_not_reexecution(
    database: Database,
):
    plugin, _, service, ctx, command = await setup(database)
    task = asyncio.create_task(service.mutate_plugin(ctx, command))
    await asyncio.wait_for(plugin.entered.wait(), 2)
    try:
        # Another writer can commit while the external effect is waiting.
        async with database.immediate_session() as session:
            session.add(
                CanonicalPersonModel(
                    id=ctx.principal.person_id.text,
                    enabled=True,
                    revision=1,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        pending = await service.mutate_plugin(ctx, command)
        assert not pending.success and pending.operation.status is OperationStatus.RUNNING
        queries = ControlQueryService(ControlQueryAdapter(database))
        assert (
            await queries.read_operation(ctx, pending.operation.operation_id) == pending.operation
        )
        assert (await queries.list_operations(ctx, PageRequest())).items[
            0
        ].status is OperationStatus.RUNNING
        other = replace(ctx, request_id=type(ctx.request_id).new())
        with pytest.raises(ControlCommandError) as exc:
            await service.mutate_plugin(
                other,
                ControlCommand(
                    request_id=other.request_id,
                    expected_revision=command.expected_revision,
                    payload={"action": "enable", "resource_id": "probe"},
                ),
            )
        assert exc.value.problem.code is ProblemCode.PRECONDITION_FAILED
        assert plugin.calls == 1
    finally:
        plugin.release.set()
        result = await task
    assert result.success
    assert await service.mutate_plugin(ctx, command) == result
    assert plugin.calls == 1
    finished = await queries.read_operation(ctx, pending.operation.operation_id)
    assert finished.status is OperationStatus.SUCCEEDED
    audits = (await queries.list_audit_events(ctx, PageRequest())).items
    assert len(audits) == 2
    assert all(
        item.principal_id == ctx.principal.principal_id and item.request_id == ctx.request_id
        for item in audits
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["domain_reply", "receipt_commit", "cancel"])
async def test_unknown_effect_is_persisted_and_never_replayed(database: Database, failure: str):
    plugin, adapter, service, ctx, command = await setup(
        database, fail_after_effect=failure == "domain_reply"
    )
    task = asyncio.create_task(service.mutate_plugin(ctx, command))
    await asyncio.wait_for(plugin.entered.wait(), 2)
    if failure == "receipt_commit":

        def crash():
            raise RuntimeError("receipt commit failed")

        adapter._after_audit_flush = crash
    if failure == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        result = await service.mutate_plugin(ctx, command)
    else:
        plugin.release.set()
        result = await task
    assert not result.success and result.operation.status is OperationStatus.UNKNOWN
    assert await service.mutate_plugin(ctx, command) == result
    assert plugin.calls == 1
    queries = ControlQueryService(ControlQueryAdapter(database))
    # A new UUID is not an escape hatch for uncertain effects on the same resource.
    other = replace(ctx, request_id=type(ctx.request_id).new())
    with pytest.raises(ControlCommandError) as exc:
        await service.mutate_plugin(
            other,
            ControlCommand(
                request_id=other.request_id,
                expected_revision=command.expected_revision,
                payload=command.payload,
            ),
        )
    assert exc.value.problem.code is ProblemCode.PRECONDITION_FAILED
    assert plugin.calls == 1
    assert (
        await queries.read_operation(ctx, result.operation.operation_id)
    ).status is OperationStatus.UNKNOWN
    async with database.sessions() as session:
        row = await session.scalar(select(ControlCommandReceiptModel))
        assert row.status == "unknown"


@pytest.mark.asyncio
async def test_startup_marks_abandoned_intent_unknown_without_reexecution(database: Database):
    plugin, adapter, service, ctx, command = await setup(database)
    task = asyncio.create_task(service.mutate_plugin(ctx, command))
    await asyncio.wait_for(plugin.entered.wait(), 2)
    assert await adapter.recover_interrupted_controls() == 1
    assert await adapter.recover_interrupted_controls() == 0
    result = await service.mutate_plugin(ctx, command)
    assert result.operation.status is OperationStatus.UNKNOWN
    assert result.operation.error_category == "process_restart"
    task.cancel()
    with pytest.raises((asyncio.CancelledError, ControlCommandError)):
        await task
    assert plugin.calls == 1
