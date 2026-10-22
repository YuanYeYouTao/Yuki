"""Callback observation keeps accepted legacy Work without changing its boundary."""

import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update
from tests.integration.test_plugin_agent_domain_grants import source_host
from tests.unit.test_history_dispatch_ownership import _tool

from yuki.llm.fake import FakeLLMProvider
from yuki.plugin_host import main_turn
from yuki.plugin_host.facades import HostPluginContext
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import work
from yuki_plugin_sdk.permissions import PluginPermission


@pytest.mark.parametrize("state", ["queued", "running"])
@pytest.mark.parametrize("observation", ["timeout", "cancel"])
async def test_old_boundary_work_survives_callback_observation(
    database, tmp_path, monkeypatch, state, observation
):
    provider = FakeLLMProvider(lambda _: _tool("environment_status", {}, "original-status"))
    _, _, chat, source, host = await source_host(database, tmp_path, provider, ())
    sandbox = SimpleNamespace(execute=AsyncMock(return_value={"ready": True}))
    chat._tools.sandbox_client = sandbox
    with host.invocation_scope(host.plugin_id, source):
        first = await host.agent.run("Inspect the original environment", max_model_requests=1)
    repository = WorkRepository(database)
    original = await repository.get(first.data["work_id"])
    assert original["state"] == "queued"
    saved = json.loads(original["source_json"])
    # Valid historical SDK subset and exact original boundary codec.
    saved["allowed_tools"] = ["environment_status", "workspace_read"]
    saved["execution_boundary"] = hashlib.sha256(
        json.dumps(
            [
                saved["origin"],
                original["conversation_id"],
                saved["parent_execution_id"],
                saved["allowed_tools"],
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    raw = json.dumps(saved, ensure_ascii=False, separators=(",", ":"))
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work)
            .where(work.c.id == original["id"])
            .values(
                source_json=raw,
                source_key=f"invocation:{saved['execution_boundary']}",
            )
        )
    if state == "running":
        lease = await repository.acquire(original["conversation_id"], original["generation"])
        try:
            current = await repository.get(original["id"])
            await repository.transition(lease, original["id"], current["revision"], "running")
        finally:
            await repository.release(lease)
    await host.close_host_resources()
    await database.engine.dispose()
    restored = HostPluginContext(
        plugin_id=host.plugin_id,
        approved_permissions=(PluginPermission.AGENT_RUN,),
        services=host._services,
    )
    entered, release = asyncio.Event(), asyncio.Event()
    complete = provider.complete
    provider._responder = lambda _: _tool(
        "task_control", {"action": "complete", "result": "original finished"}, "finish-original"
    )

    async def paused(request):
        entered.set()
        await release.wait()
        return await complete(request)

    monkeypatch.setattr(provider, "complete", paused)
    monkeypatch.setattr(main_turn, "CALLBACK_WAIT_SECONDS", 0.3 if observation == "timeout" else 30)
    try:
        with restored.invocation_scope(restored.plugin_id, source):
            observer = asyncio.create_task(
                restored.agent.run(
                    "Inspect the original environment",
                    max_model_requests=1,
                )
            )
            await asyncio.wait_for(entered.wait(), 3)
            if observation == "cancel":
                observer.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await observer
            else:
                current = await asyncio.wait_for(observer, 3)
                assert current.data["work_id"] == original["id"]
                assert current.data["pending"] is True
            accepted = await repository.get(original["id"])
            assert accepted["state"] in {"queued", "running"}
            assert accepted["source_json"] == raw
            active = [
                task
                for task in main_turn._RUNNING.values()
                if task.get_name() == f"plugin-main-{host.plugin_id}"
            ]
            assert len(active) == 1 and not active[0].done()
            release.set()
            await asyncio.wait_for(asyncio.gather(*active), 3)
        final = await repository.get(original["id"])
        assert final["state"] == "completed"
        assert final["source_json"] == raw
        assert final["source_key"] == f"invocation:{saved['execution_boundary']}"
        assert final["model_requests"] == 2 and len(provider.requests) == 2
        sandbox.execute.assert_awaited_once()
        async with database.read_session() as session:
            assert await session.scalar(select(func.count()).select_from(work)) == 1
    finally:
        release.set()
        await restored.close_host_resources()
