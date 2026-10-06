"""One production execution core and complete runtime resource ownership."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.social_identity_cases import social_env
from tests.unit.test_work_protocol_continuity import _control

from qq_ai_bot.container import ApplicationContainer
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import ChatMessage, InboundMessage, SenderIdentity
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.plugin_host.manifest import PluginManifest
from qq_ai_bot.runtime.activation_bindings import ActiveWorkBindings
from qq_ai_bot.runtime.work_activation import activate_work, bind_work_activation
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects
from qq_ai_bot.services.yuki_runtime import YukiRuntime


async def test_production_sources_share_core_without_sharing_turn_state(database, tmp_path):
    plugin_directory = tmp_path / "plugins"
    plugin_directory.mkdir()
    settings = make_settings(
        database.url, plugin_directory=plugin_directory, plugin_system_enabled=False
    )
    app = ApplicationContainer(settings, database=database)
    try:
        runtime = app.runtime
        assert app.chat.runtime is runtime
        assert runtime.main_turns.executions is runtime.executions
        assert runtime.bindings.executions is runtime.executions
        assert app.subagent_execution.services.active_bindings is runtime.bindings
        assert app._automation_handlers.main_turns is runtime.main_turns
        assert app._automation_handlers.main_contract is runtime.contract
        assert app.subagent_execution.services.runner is runtime.runner
        assert app.work_resumer.services.active_bindings is runtime.bindings
        assert app.plugin_background_turns._chat.runtime is runtime
        context = app._create_plugin_context(
            PluginManifest(
                id="fixture-runtime",
                name="Runtime fixture",
                version="1.0.0",
                description="Verify shared Host execution dependencies",
                entrypoint="fixture:Plugin",
                plugin_api="3.1",
                yuki_requires=">=3.9.0",
            ),
            frozenset(),
        )
        assert context._services.agent_runner is runtime.runner
        names = app.lifecycle.names
        assert names.count("yuki_runtime") == 1
        assert "runtime_work" not in names and "subagents" not in names
        assert names.index("main_agent_manifest") < names.index("yuki_runtime")
        assert names.index("yuki_runtime") < names.index("plugin_background_turns")

        # The singleton contains execution resources, not a shared actor/session.
        old = SimpleNamespace(current={"id": "conversation-a"})
        other = SimpleNamespace(current={"id": "conversation-b"})
        with runtime.bindings.bind("conversation-a", old):
            with runtime.bindings.bind("conversation-b", other):
                assert runtime.bindings.get("conversation-a") is old
                assert runtime.bindings.get("conversation-b") is other
        assert runtime.bindings.get("conversation-a") is None
        assert runtime.bindings.get("conversation-b") is None
        await runtime.close()
        app.subagent_execution.children.acquire = AsyncMock()
        with pytest.raises(RuntimeError, match="yuki_runtime_closing"):
            await app.subagent_execution.run("child-after-close")
        app.subagent_execution.children.acquire.assert_not_awaited()
    finally:
        await app.model_clients.close()
        await app.plugin_http.close()


class Worker:
    def __init__(self, name, log, *, start_error=None, close_error=None, started=None):
        self.name, self.log = name, log
        self.start_error, self.close_error = start_error, close_error
        self.started = started

    async def start(self):
        self.log.append(f"start:{self.name}")
        if self.started is not None:
            self.started.set()
            await asyncio.Event().wait()
        if self.start_error is not None:
            raise self.start_error

    async def close(self):
        self.log.append(f"close:{self.name}")
        if self.close_error is not None:
            raise self.close_error

    async def health(self):
        return {"name": self.name}


def runtime_with_workers(*workers, bindings=None):
    bindings = bindings if bindings is not None else ActiveWorkBindings()
    runtime = YukiRuntime(
        SimpleNamespace(executions=bindings.executions),
        SimpleNamespace(main_contract=object()),
        bindings,
    )
    for worker in workers:
        runtime.register_worker(worker.name, worker)
    return runtime


async def test_runtime_start_failure_closes_partial_worker_and_earlier_owner():
    log = []
    runtime = runtime_with_workers(
        Worker("work", log),
        Worker("child", log, start_error=ValueError("prepare failed")),
        Worker("never-started", log),
    )
    with pytest.raises(ValueError, match="prepare failed"):
        await runtime.start()
    await runtime.close()
    assert log == ["start:work", "start:child", "close:child", "close:work"]


async def test_runtime_cancelled_start_rolls_back_earlier_worker_and_preserves_cancel():
    log, child_started = [], asyncio.Event()
    runtime = runtime_with_workers(Worker("work", log), Worker("child", log, started=child_started))
    start = asyncio.create_task(runtime.start())
    await child_started.wait()
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    await runtime.close()
    assert log == ["start:work", "start:child", "close:child", "close:work"]


async def test_runtime_shutdown_failure_does_not_leave_other_worker_running():
    log = []
    runtime = runtime_with_workers(
        Worker("work", log), Worker("child", log, close_error=ValueError("cleanup failed"))
    )
    await runtime.start()
    with pytest.raises(ExceptionGroup, match="application shutdown failed"):
        await runtime.close()
    await runtime.close()
    assert log == ["start:work", "start:child", "close:child", "close:work"]


async def test_runtime_shutdown_cancellation_still_closes_other_worker():
    log = []
    runtime = runtime_with_workers(
        Worker("work", log), Worker("child", log, close_error=asyncio.CancelledError())
    )
    await runtime.start()
    with pytest.raises(asyncio.CancelledError):
        await runtime.close()
    assert log == ["start:work", "start:child", "close:child", "close:work"]
    # The second caller must not repeat a partly completed close or revive Work.
    await runtime.close()
    assert log == ["start:work", "start:child", "close:child", "close:work"]


async def test_shutdown_joins_original_work_recovery_and_keeps_budget_receipt(database, tmp_path):
    control = await _control(database, tmp_path)
    identity = control.current["id"]
    await control.repository.checkpoint(control.lease, identity, {"keep": "cursor"}, models=2)
    await control.repository.prepare_effect(control.lease, identity, "original", "tool")
    await control.repository.record_effect("original", "unknown", {"run_id": "original-run"})
    runtime = runtime_with_workers()
    entered = asyncio.Event()

    async def execution():
        with runtime.executions.track():
            async with bind_work_activation(control, bindings=runtime.bindings, scope_key="scope"):
                assert runtime.executions.active_count == 1
                entered.set()
                await asyncio.Event().wait()

    task = asyncio.create_task(execution())
    await entered.wait()
    await runtime.close()
    assert task.cancelled()
    assert runtime.executions.active_count == 0
    assert runtime.bindings.get("scope") is None
    assert not await control.repository.valid(control.lease)
    current = await control.repository.get(identity)
    assert current["model_requests"] == 2
    assert json.loads(current["checkpoint_json"])["keep"] == "cursor"
    async with database.sessions() as session:
        receipt = (
            (await session.execute(select(effects).where(effects.c.effect_key == "original")))
            .mappings()
            .one()
        )
    assert receipt["state"] == "unknown"
    assert json.loads(receipt["receipt_json"])["run_id"] == "original-run"


async def test_closing_rejects_activation_before_acquire_and_releases_acquire_race(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    bindings = ActiveWorkBindings()
    real_acquire = repository.acquire

    async def closing_acquire(*args):
        lease = await real_acquire(*args)
        bindings.executions.stop_admission()
        acquired.append(lease)
        return lease

    acquired = []
    monkeypatch.setattr(repository, "acquire", AsyncMock(side_effect=closing_acquire))
    with pytest.raises(asyncio.CancelledError, match="yuki_runtime_shutdown"):
        async with activate_work(
            repository,
            env.context.conversation_id,
            1,
            "source",
            {},
            AsyncMock(),
            bindings=bindings,
        ):
            pytest.fail("closing activation must not execute")
    assert not await repository.valid(acquired[0])
    repository.acquire.reset_mock()
    with pytest.raises(RuntimeError, match="yuki_runtime_closing"):
        async with activate_work(
            repository,
            env.context.conversation_id,
            1,
            "other",
            {},
            AsyncMock(),
            bindings=bindings,
        ):
            pytest.fail("closed activation must not execute")
    repository.acquire.assert_not_awaited()


async def test_nested_entry_during_worker_shutdown_keeps_original_work_retryable(
    database, tmp_path
):
    control = await _control(database, tmp_path)
    identity = control.current["id"]
    await control.repository.checkpoint(control.lease, identity, {"keep": "cursor"}, models=2)
    await control.repository.prepare_effect(control.lease, identity, "shutdown-race", "tool")
    await control.repository.record_effect("shutdown-race", "unknown", {"run_id": "original-run"})
    harness = build_harness(database, make_settings(database.url))
    runtime = harness.processor._chat.runtime
    entered, return_from_io = asyncio.Event(), asyncio.Event()
    closing_worker, release_worker = asyncio.Event(), asyncio.Event()

    class BlockingWorker(Worker):
        async def close(self):
            closing_worker.set()
            await release_worker.wait()
            await super().close()

    runtime.register_worker("blocking", BlockingWorker("blocking", []))
    await runtime.start()

    async def execution():
        with runtime.executions.track():
            async with bind_work_activation(control, bindings=runtime.bindings, scope_key="scope"):
                entered.set()
                await return_from_io.wait()
                await runtime.main_turns.run((), SimpleNamespace(), None)

    task = asyncio.create_task(execution())
    shutdown = None
    try:
        await entered.wait()
        shutdown = asyncio.create_task(runtime.close())
        await closing_worker.wait()
        return_from_io.set()
        with pytest.raises(asyncio.CancelledError, match="yuki_runtime_shutdown"):
            await task
        assert not shutdown.done()
        current = await control.repository.get(identity)
        assert current["state"] == "queued"
        assert current["model_requests"] == 2
        assert json.loads(current["checkpoint_json"])["keep"] == "cursor"
        assert not await control.repository.valid(control.lease)
        async with database.sessions() as session:
            receipt = (
                (
                    await session.execute(
                        select(effects).where(effects.c.effect_key == "shutdown-race")
                    )
                )
                .mappings()
                .one()
            )
        assert receipt["state"] == "unknown"
        assert json.loads(receipt["receipt_json"])["run_id"] == "original-run"
    finally:
        release_worker.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if shutdown is not None:
            await shutdown


async def test_shutdown_cancels_chat_preparation_without_work_and_blocks_new_model_entry(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    harness = build_harness(database, make_settings(database.url, runtime_work_enabled=False))
    chat = harness.processor._chat
    identity = ConversationScope.group("80001", "20001")
    state = await harness.conversation_scopes.get(identity)
    turn = ConversationTurnSnapshot(state.id, identity.key, 1, 1, 1)
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def prepare(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    prepare_spy = AsyncMock(side_effect=prepare)
    monkeypatch.setattr(chat, "_build_messages", prepare_spy)
    inbound = InboundMessage(
        message_id="inbound",
        source_event_id=1,
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity("10001"),
        text="hello",
        bot_user_id="80001",
        group_id="20001",
        person_id=env.person,
        space_id=env.space,
        conversation_id=env.context.conversation_id,
        presence_id=env.presence,
    )
    args = (
        inbound,
        identity,
        UserProfileSnapshot(user_id="10001", scope_type=ScopeType.GROUP),
        "hello",
        MemorySender(),
    )
    task = asyncio.create_task(chat.respond(*args, turn_snapshot=turn))
    await asyncio.wait_for(entered.wait(), 1)
    await chat.runtime.close()
    assert task.cancelled() and cleaned.is_set()
    assert chat.runtime.executions.active_count == 0
    assert harness.provider.requests == []
    with pytest.raises(RuntimeError, match="yuki_runtime_closing"):
        await chat.respond(*args, turn_snapshot=turn)
    prepare_spy.assert_awaited_once()
    with pytest.raises(RuntimeError, match="yuki_runtime_closing"):
        await chat.runtime.main_turns.run(
            (ChatMessage("user", "new call"),), SimpleNamespace(), None
        )


async def test_execution_can_close_its_runtime_without_cancelling_itself():
    runtime = runtime_with_workers()
    with runtime.executions.track():
        await runtime.close()
        assert asyncio.current_task().cancelling() == 0
        assert runtime.executions.active_count == 1
        with pytest.raises(RuntimeError, match="yuki_runtime_closing"):
            with runtime.executions.track():
                pytest.fail("new nested entry must be refused after close")
    assert runtime.executions.active_count == 0
