"""Retired MCP surfaces cannot dispatch; historical receipts retain their identity."""

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import select
from tests.unit.test_control_external_execution import setup

from qq_ai_bot.control_plane import (
    ControlCommand,
    ControlCommandError,
    ControlCommandService,
    ControlQueryService,
    OperationStatus,
    PageRequest,
    ProblemCode,
)
from qq_ai_bot.control_plane.capabilities import is_protocol_capability
from qq_ai_bot.control_plane.surface import method_capability
from qq_ai_bot.conversation.canonical_db_models import ControlCommandReceiptModel
from qq_ai_bot.persistence.control_query import ControlQueryAdapter


def test_retired_mcp_has_no_executable_or_catalog_surface():
    for name in ("mutate_mcp", "list_mcp_servers"):
        assert not hasattr(ControlCommandService, name)
        assert not hasattr(ControlQueryService, name)
        with pytest.raises(KeyError):
            method_capability(name)
    for capability in ("control.mcp.read", "control.mcp.mutate", "mcp.web_search"):
        assert not is_protocol_capability(capability)
    assert is_protocol_capability("web_search")


@pytest.mark.asyncio
async def test_historical_running_mcp_receipt_becomes_unknown_and_remains_queryable(database):
    # Reserve a valid generic external intent, then restore its historical operation discriminator.
    plugin, adapter, service, ctx, command = await setup(database)
    task = asyncio.create_task(service.mutate_plugin(ctx, command))
    await asyncio.wait_for(plugin.entered.wait(), 2)
    try:
        pending = await service.mutate_plugin(ctx, command)
        async with database.immediate_session() as session:
            row = await session.scalar(
                select(ControlCommandReceiptModel)
                .where(ControlCommandReceiptModel.request_id == ctx.request_id.text)
                .limit(1)
            )
            assert row is not None
            row.operation = "control.mcp.mutate"
        assert await adapter.recover_interrupted_controls() == 1
        assert await adapter.recover_interrupted_controls() == 0
        queries = ControlQueryService(ControlQueryAdapter(database))
        receipt = await queries.read_operation(ctx, pending.operation.operation_id)
        assert receipt.operation_id == pending.operation.operation_id
        assert receipt.status is OperationStatus.UNKNOWN
        assert receipt.error_category == "process_restart"
        assert (await queries.list_audit_events(ctx, PageRequest())).items
        retry_ctx = replace(
            ctx,
            request_id=type(ctx.request_id).new(),
            principal=replace(
                ctx.principal,
                granted_capabilities=(
                    *ctx.principal.granted_capabilities,
                    "control.operation.retry",
                ),
            ),
        )
        with pytest.raises(ControlCommandError) as exc:
            await service.retry_operation(
                retry_ctx,
                ControlCommand(
                    request_id=retry_ctx.request_id,
                    expected_revision=0,
                    payload={"action": "retry", "resource_id": receipt.operation_id},
                ),
            )
        assert exc.value.problem.code is ProblemCode.OPERATION_UNAVAILABLE
        assert (
            await queries.read_operation(ctx, receipt.operation_id)
        ).status is OperationStatus.UNKNOWN
        assert plugin.calls == 1
    finally:
        task.cancel()
        with pytest.raises((asyncio.CancelledError, ControlCommandError)):
            await task
