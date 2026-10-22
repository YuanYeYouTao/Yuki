"""A queued local-agent write cannot authorize dispatch after its source expires."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import event, select
from tests.unit.test_external_agent_repository import connect, register_device
from tests.unit.test_external_agent_work import case as work_case

from yuki.external_agents.db_models import AgentOperationModel
from yuki.external_agents.service import ExternalAgentService
from yuki.external_agents.transport import DeviceConnection
from yuki.runtime.work_repository import WorkConflict
from yuki.runtime.work_schema_v1 import work
from yuki.sandbox.db_models import SandboxTaskRunModel

case = work_case


async def test_source_revoked_after_queued_prepare_is_never_first_dispatched(
    database, case, monkeypatch
):
    native = await register_device(database, case.source["actor_person_id"])
    native.service = ExternalAgentService(database, case.tasks)
    commands = []

    class Socket:
        async def send_json(self, command):
            commands.append(command)
            connection.respond(
                {
                    "id": command["id"],
                    "result": {"admitted": True, "status": "running", "pending": True},
                }
            )

        async def close(self, code=1000, reason=None):
            pass

    connection = DeviceConnection(native.device["device_id"], Socket())
    await connect(native, connection)
    repository, lease = case.control.repository, case.control.lease

    async def validate_original_source():
        async with database.sessions() as reader:
            await repository._assert_lease_readonly(reader, lease)

    case.control.validate = validate_original_source
    original_writer = database.immediate_session
    queued = asyncio.Event()
    prepared = asyncio.Event()
    dispatch_allowed = asyncio.Event()
    dispatch = None
    prepared_once = False

    @asynccontextmanager
    async def pause_after_real_prepare():
        # Only schedule the interleaving; both writes and every source check stay real.
        nonlocal prepared_once
        preparing = asyncio.current_task() is dispatch and not prepared_once
        async with original_writer() as session:
            yield session
        if preparing:
            prepared_once = True
            prepared.set()
            await dispatch_allowed.wait()

    def observe_writer_attempt(_connection, _cursor, statement, *_):
        if asyncio.current_task() is dispatch and statement == "BEGIN IMMEDIATE":
            queued.set()

    monkeypatch.setattr(database, "immediate_session", pause_after_real_prepare)
    event.listen(database.engine.sync_engine, "before_cursor_execute", observe_writer_attempt)
    arguments = {"action": "prompt", "instance_id": native.instance, "prompt": "isolated task"}
    request_id = "queued-source-original-request"
    try:
        async with asyncio.timeout(5):
            # A real unrelated SQLite writer makes the original prepare wait.
            async with original_writer():
                dispatch = asyncio.create_task(
                    native.service.execute(arguments, request_id, case.source)
                )
                await queued.wait()
                assert not dispatch.done() and not prepared.is_set()
                assert commands == []
            await prepared.wait()
            async with database.sessions() as reader:
                original = await reader.get(AgentOperationModel, request_id)
                assert original is not None and original.result_json is None
            # The real hard boundary wins after durable prepare, before first native send.
            await repository.cancel(lease.conversation_id, generation=lease.generation + 1)
            assert not await repository.valid(lease)
            dispatch_allowed.set()
            with pytest.raises(WorkConflict, match="work_activation_obsolete"):
                await dispatch
        assert commands == []
        async with database.sessions() as reader:
            original = await reader.get(AgentOperationModel, request_id)
            receipt = json.loads(original.result_json)
            task = await reader.get(SandboxTaskRunModel, request_id)
            assert (
                await reader.scalar(select(work.c.state).where(work.c.id == original.work_id))
            ) == "cancelled"
        assert receipt["error"] == "source_changed_before_dispatch"
        assert receipt["error_type"] == "WorkConflict"
        assert receipt["executed"] is False and receipt["admitted"] is False
        assert receipt["pending"] is False
        assert task.status == "completed"
        assert json.loads(task.completion_json)["admitted"] is False
        recovered = await native.service.execute(arguments, request_id, case.source)
        assert recovered["pending"] is False and recovered["admitted"] is False
        assert recovered["executed"] is False and commands == []
    finally:
        dispatch_allowed.set()
        if dispatch is not None and not dispatch.done():
            dispatch.cancel()
            await asyncio.gather(dispatch, return_exceptions=True)
        event.remove(database.engine.sync_engine, "before_cursor_execute", observe_writer_attempt)
        await native.service.close()
