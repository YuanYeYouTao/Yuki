
"""A historical rebuild keeps paid work while its original Person loses authorization."""

import asyncio
from contextlib import asynccontextmanager

from sqlalchemy import select
from tests.unit.test_memory_rebuild import _admin, _event, _ExtractionProvider, _service

from yuki.identity.db_models import CanonicalPersonModel
from yuki.memory.enums import MemoryRebuildCommitStatus, MemoryRebuildRunStatus
from yuki.memory.rebuild.models import MemoryRebuildSelection
from yuki.memory.rebuild.worker import MemoryRebuildWorker
from yuki.model_runtime.dispatch_guard import check_model_dispatch
from yuki.persistence.models import MemoryRebuildItemModel, MemoryRebuildProposalModel


async def qualification(database, person_id, value):
    async with database.immediate_session() as writer:
        person = await writer.get(CanonicalPersonModel, person_id)
        person.is_superuser = value


async def test_rebuild_revocation_pauses_before_next_paid_dispatch(database):
    settings, ledger, _facts, provider, service = await _service(database)
    owner = await _admin(database)
    await _event(ledger, message_id="role-revoked-before-dispatch")
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id=owner)
    await service.start(run.public_id, actor_user_id=owner)
    await qualification(database, owner, False)
    worker = MemoryRebuildWorker(
        service, interval_seconds=settings.memory_rebuild_worker_interval_seconds
    )
    assert await worker.process_once() == 0
    current = await service.repository.get_run(run.public_id)
    assert current.status is MemoryRebuildRunStatus.EXTRACTION_PAUSED
    assert current.error_category == "initiator_permission_denied"
    assert provider.requests == 0
    await qualification(database, owner, True)
    await service.resume(run.public_id, actor_user_id=owner)
    assert await worker.process_once() == 1
    assert provider.requests == 1


async def test_rebuild_rechecks_revocation_after_actual_model_queue_wait(database):
    entered, release = asyncio.Event(), asyncio.Event()

    class QueuedProvider(_ExtractionProvider):
        async def complete(self, request):
            entered.set()
            await release.wait()
            await check_model_dispatch()
            return await super().complete(request)

    provider = QueuedProvider()
    settings, ledger, _facts, _provider, service = await _service(database, provider=provider)
    owner = await _admin(database)
    await _event(ledger, message_id="revoked-in-model-queue")
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id=owner)
    await service.start(run.public_id, actor_user_id=owner)
    worker = MemoryRebuildWorker(
        service, interval_seconds=settings.memory_rebuild_worker_interval_seconds
    )
    processing = asyncio.create_task(worker.process_once())
    await asyncio.wait_for(entered.wait(), 5)
    await qualification(database, owner, False)
    release.set()
    assert await processing == 0
    assert provider.requests == 0
    assert (
        await service.repository.get_run(run.public_id)
    ).status is MemoryRebuildRunStatus.EXTRACTION_PAUSED
    assert (await service.repository.statistics(run.public_id))["extraction_requests"] == 0
    async with database.read_session() as reader:
        item = await reader.scalar(select(MemoryRebuildItemModel))
        assert item.status == "pending"


async def test_rebuild_writer_rechecks_role_and_preserves_paid_staging(database, monkeypatch):
    settings, ledger, facts, provider, service = await _service(database)
    owner = await _admin(database)
    await _event(ledger, message_id="role-revoked-before-commit")
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id=owner)
    await service.start(run.public_id, actor_user_id=owner)
    worker = MemoryRebuildWorker(
        service, interval_seconds=settings.memory_rebuild_worker_interval_seconds
    )
    assert await worker.process_once() == 1
    assert await worker.process_once() == 0
    await service.set_review(run.public_id, "all", approved=True, actor_user_id=owner)
    await service.commit(run.public_id, actor_user_id=owner)
    apply = facts.repository.write_session

    @asynccontextmanager
    async def revoke_before_writer(prepared):
        await qualification(database, owner, False)
        async with apply(prepared) as writer:
            yield writer

    with monkeypatch.context() as revoked:
        revoked.setattr(facts.repository, "write_session", revoke_before_writer)
        assert await worker.process_once() == 0
    paused = await service.repository.get_run(run.public_id)
    assert paused.status is MemoryRebuildRunStatus.COMMIT_PAUSED
    assert paused.error_category == "initiator_permission_denied"
    assert await facts.list_person("1001") == ()
    async with database.read_session() as reader:
        proposal = await reader.scalar(select(MemoryRebuildProposalModel))
        assert proposal.commit_status == MemoryRebuildCommitStatus.PENDING.value
    assert (await service.repository.statistics(run.public_id))["extraction_requests"] == 1
    await qualification(database, owner, True)
    await service.resume(run.public_id, actor_user_id=owner)
    assert await worker.process_once() == 1
    assert len(await facts.list_person("1001")) == 1
    assert provider.requests == 1
    assert (
        await service.repository.get_run(run.public_id)
    ).status is MemoryRebuildRunStatus.COMPLETED
