"""MCP dispatch certainty and hot revocation, through the original binding."""

import json
from types import SimpleNamespace

import httpx
import pytest
from tests.unit.test_tool_effect_audit import active_work
from tests.unit.test_tool_kernel_mcp import _call_mcp

from qq_ai_bot.domain.messages import ToolCall, ToolFunction
from qq_ai_bot.mcp.fake import FakeMCPConnection
from qq_ai_bot.mcp.manager import MCPManager
from qq_ai_bot.mcp.repository import MCPRepository


async def manager_case(database, tmp_path, *, read_only=False, failure=None):
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "offline": {
                        "command": "unused-test-command",
                        "lifecycle": "keep_alive",
                        "yuki": {"toolAnnotations": {"effect": {"readOnlyHint": read_only}}},
                    }
                }
            }
        )
    )
    log = tmp_path / "mcp-downstream.jsonl"

    class Connection(FakeMCPConnection):
        async def call_tool(self, name, arguments):
            with log.open("a") as handle:
                handle.write(json.dumps({"name": name, "arguments": arguments}) + "\n")
            raise failure or OSError("remote committed but response lost")

    connection = Connection(
        tools=(
            SimpleNamespace(
                name="effect",
                description="offline effect",
                inputSchema={"type": "object"},
                outputSchema=None,
                annotations=None,
            ),
        )
    )
    manager = MCPManager(
        enabled=True,
        config_path=path,
        cache_enabled=False,
        metadata_cache_ttl_seconds=60,
        connect_timeout_seconds=1,
        request_timeout_seconds=1,
        max_parallel_calls=1,
        repository=MCPRepository(database),
        connection_factory=lambda *_args, **_kwargs: connection,
    )
    await manager.start()
    await manager.ensure_metadata("offline")
    return manager, log


@pytest.mark.parametrize("read_only", [False, True])
async def test_reconnect_does_not_replay_original_write_and_read_certainty_stays_separate(
    database, tmp_path, read_only
):
    manager, log = await manager_case(database, tmp_path, read_only=read_only)
    try:
        _, owner, _ = await active_work(database, tmp_path)
        call = ToolCall("original-mcp", ToolFunction("mcp__offline__effect", "{}"))

        async def invoke():
            result = await _call_mcp(manager, "offline", "effect", {})
            assert result.uncertain is (not read_only)
            assert result.mutation_committed is (False if read_only else None)
            assert result.retryable is read_only
            return json.dumps(result.model_payload())

        await owner.execute(call, invoke, side_effecting=not read_only)
        await owner.execute(call, invoke, side_effecting=not read_only)
        assert len(log.read_text().splitlines()) == 1
        if not read_only:
            rejected = await owner.execute(
                ToolCall("new-write", call.function), invoke, side_effecting=True
            )
            assert json.loads(rejected)["error"] == "unresolved_prior_effect"
            assert len(log.read_text().splitlines()) == 1
    finally:
        await manager.close()


async def test_disabled_server_refuses_new_call_before_remote_handler(database, tmp_path):
    manager, log = await manager_case(database, tmp_path)
    try:
        manager.configure_runtime(enabled=False)
        result = await _call_mcp(manager, "offline", "effect", {})
        assert not result.ok and result.mutation_committed is False and not result.uncertain
        assert not log.exists()
    finally:
        await manager.close()


@pytest.mark.parametrize("status,uncertain", [(400, False), (401, False), (408, True), (500, True)])
async def test_write_http_timeout_or_server_failure_is_not_a_confirmed_rejection(
    database, tmp_path, status, uncertain
):
    failure = httpx.HTTPStatusError(
        "offline failure",
        request=httpx.Request("POST", "https://offline.invalid/mcp"),
        response=httpx.Response(status),
    )
    manager, log = await manager_case(database, tmp_path, failure=failure)
    try:
        result = await _call_mcp(manager, "offline", "effect", {})
        assert result.uncertain is uncertain
        assert result.mutation_committed is (None if uncertain else False)
        assert not result.retryable
        assert len(log.read_text().splitlines()) == 1
    finally:
        await manager.close()


@pytest.mark.parametrize("change", ["metadata", "disabled"])
async def test_change_during_connection_wait_refuses_original_call(
    database, tmp_path, monkeypatch, change
):
    manager, log = await manager_case(database, tmp_path)
    original = manager._ensure_connection

    async def waiting(server_id, config):
        connection = await original(server_id, config)
        if change == "metadata":
            manager._tools[server_id] = tuple(
                metadata.model_copy(update={"metadata_hash": "new-revision"})
                for metadata in manager._tools[server_id]
            )
        else:
            manager.configure_runtime(enabled=False)
        return connection

    monkeypatch.setattr(manager, "_ensure_connection", waiting)
    try:
        result = await _call_mcp(manager, "offline", "effect", {})
        assert not result.ok and result.mutation_committed is False and not result.uncertain
        assert not log.exists()
    finally:
        await manager.close()
