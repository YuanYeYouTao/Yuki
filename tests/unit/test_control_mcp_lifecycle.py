"""Control and ordinary MCP lifecycle calls share locks, versions and actual state."""

import asyncio
import json
from dataclasses import replace

import pytest
from tests.unit.test_control_plane_foundation import context

from qq_ai_bot.control_plane import (
    ControlCommand,
    ControlCommandError,
    ControlCommandService,
    ControlQueryService,
    PageRequest,
    ProblemCode,
)
from qq_ai_bot.domain.identity import RequestId
from qq_ai_bot.mcp.fake import FakeMCPConnection
from qq_ai_bot.mcp.manager import MCPManager
from qq_ai_bot.mcp.repository import MCPRepository
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.control_query import ControlQueryAdapter


class CountingConnection(FakeMCPConnection):
    connections = 0

    async def connect(self):
        self.connections += 1
        await super().connect()


async def setup(database, tmp_path):
    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"probe": {"command": "offline", "lifecycle": "lazy"}}}),
        encoding="utf-8",
    )
    connection = CountingConnection()
    manager = MCPManager(
        enabled=True,
        config_path=path,
        cache_enabled=True,
        metadata_cache_ttl_seconds=3600,
        connect_timeout_seconds=2,
        request_timeout_seconds=2,
        max_parallel_calls=1,
        repository=MCPRepository(database),
        connection_factory=lambda *_args, **_kwargs: connection,
    )
    await manager.start()
    return manager, connection


@pytest.mark.asyncio
async def test_control_returns_actual_mcp_status_and_replay_never_reconnects(database, tmp_path):
    manager, connection = await setup(database, tmp_path)
    commands = ControlCommandService(ControlCommandAdapter(database, mcp_manager=manager))
    queries = ControlQueryService(ControlQueryAdapter(database, mcp_manager=manager))
    ctx = context("control.mcp.read", "control.mcp.mutate")
    try:
        for action, status in (
            ("disable", "disabled"),
            ("enable", "disconnected"),
            ("refresh", "connected"),
            ("reconnect", "connected"),
        ):
            current = (await queries.list_mcp_servers(ctx, PageRequest())).items[0]
            ctx = replace(ctx, request_id=RequestId.new())
            command = ControlCommand(
                request_id=ctx.request_id,
                expected_revision=current.revision,
                payload={"action": action, "resource_id": "probe"},
            )
            result = await commands.mutate_mcp(ctx, command)
            assert result.success and result.effective_state["status"] == status
            calls = connection.connections
            assert await commands.mutate_mcp(ctx, command) == result
            assert connection.connections == calls
            updated = (await queries.list_mcp_servers(ctx, PageRequest())).items[0]
            assert updated.revision == result.revision
            if action == "disable":
                ctx = replace(ctx, request_id=RequestId.new())
                rejected = ControlCommand(
                    request_id=ctx.request_id,
                    expected_revision=updated.revision,
                    payload={"action": "refresh", "resource_id": "probe"},
                )
                for _ in range(2):
                    with pytest.raises(ControlCommandError) as exc:
                        await commands.mutate_mcp(ctx, rejected)
                    assert exc.value.problem.code is ProblemCode.PRECONDITION_FAILED
                # Proven pre-effect rejection must not leave an unknown resource reservation.
                assert connection.connections == calls
        assert connection.connections == 2
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_refresh_waiting_for_lock_does_not_reenable_a_disabled_server(database, tmp_path):
    manager, connection = await setup(database, tmp_path)
    lock = manager._lock("probe")
    await lock.acquire()
    try:
        disable = asyncio.create_task(manager.set_enabled("probe", False))
        await asyncio.sleep(0)
        refresh = asyncio.create_task(manager.refresh("probe"))
        await asyncio.sleep(0)
    finally:
        lock.release()
    await disable
    with pytest.raises(RuntimeError, match="disabled"):
        await refresh
    assert not manager.server_enabled("probe") and connection.connections == 0
    await manager.close()
