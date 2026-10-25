import asyncio

from sqlalchemy import update
from tests.unit.test_external_agent_repository import prepare
from tests.unit.test_external_agent_supervision import Socket
from tests.unit.test_external_agent_work import case as case
from tests.unit.test_external_agent_writer_contract import owned

from yuki.external_agents.service import ExternalAgentService
from yuki.external_agents.transport import DeviceConnection
from yuki.runtime.work_schema_v1 import work


async def test_retirement_crosses_pages_and_does_not_duplicate_active_native_operations(
    case, monkeypatch
):
    env, original = await owned(case)
    expected = {original.id}
    for index in range(130):
        row, _ = await prepare(
            env,
            identity=f"retire-page-{index:03}",
            action="prompt",
            native_execution_id=f"native-{index}",
            lease=case.control.lease,
        )
        expected.add(row.id)
    async with case.db.immediate_session() as writer:
        await writer.execute(
            update(work).where(work.c.id == original.work_id).values(state="completed")
        )
    service = ExternalAgentService(case.db, case.tasks)
    seen = []
    release = asyncio.Event()

    async def cleanup(identities, operation_id):
        seen.extend(identities)
        await release.wait()
        return []

    monkeypatch.setattr(service, "cleanup_tasks", cleanup)
    await service.reconcile()
    assert not seen and not service._retiring
    await service.connections.attach(DeviceConnection(env.device["device_id"], Socket()))
    try:
        await service.reconcile()
        await asyncio.sleep(0)
        assert set(seen) == expected and len(seen) == len(expected)
        await service.reconcile()
        await asyncio.sleep(0)
        assert len(seen) == len(expected)
    finally:
        release.set()
        await asyncio.gather(*service._retiring.values())
        await service.close()
