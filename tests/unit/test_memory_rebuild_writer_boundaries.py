"""Original extraction receipts and source semantics survive writer handoffs."""

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from tests.unit.test_memory_rebuild import _admin, _event, _service

from yuki.domain.audio import AudioTranscript, serialize_transcripts
from yuki.memory.rebuild.models import MemoryRebuildSelection
from yuki.memory.rebuild.service import prepare_rebuild_core
from yuki.memory.rebuild.worker import MemoryRebuildWorker
from yuki.persistence.models import ChatEventModel, MemoryJobModel, MemoryRebuildItemModel


async def test_staged_extraction_is_not_reopened_after_commit_acknowledgement_error(
    database, monkeypatch
):
    _settings, ledger, _facts, provider, service = await _service(database)
    await _event(ledger, message_id="staged-acknowledgement")
    run = await service.plan(
        MemoryRebuildSelection(all_events=True), actor_user_id=await _admin(database)
    )
    await service.start(run.public_id, actor_user_id=await _admin(database))
    original = service.repository.stage_claims

    async def uncertain(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("commit acknowledgement lost")

    monkeypatch.setattr(service.repository, "stage_claims", uncertain)
    worker = MemoryRebuildWorker(service, interval_seconds=1)
    await worker.process_once()
    async with database.read_session() as reader:
        item = await reader.scalar(select(MemoryRebuildItemModel))
        assert item.status == "staged"
    await worker.process_once()
    assert provider.requests == 1
    statistics = await service.repository.statistics(run.public_id)
    assert (
        statistics["extraction_requests"],
        statistics["input_tokens"],
        statistics["output_tokens"],
    ) == (1, 11, 7)


async def test_prepared_plan_uses_supplied_writer_for_current_authority(database, monkeypatch):
    settings, ledger, _facts, _provider, service = await _service(database)
    await _event(ledger, message_id="prepared-plan-owner")
    actor = await _admin(database)
    selection = MemoryRebuildSelection(all_events=True)
    prepared = await prepare_rebuild_core(settings=settings, ledger=ledger, selection=selection)

    def no_reader():
        raise AssertionError("supplied writer must not open another reader")

    async with database.immediate_session() as writer:
        monkeypatch.setattr(database, "read_session", no_reader)
        run = await service.plan(selection, actor_user_id=actor, prepared=prepared, session=writer)
        assert run.snapshot_max_event_id == prepared.snapshot


@pytest.mark.parametrize("field", ["content", "audio_transcript"])
async def test_receipt_finalization_rechecks_original_body_before_first_write(
    database, monkeypatch, field
):
    _settings, ledger, _facts, _provider, service = await _service(database)
    source = await _event(ledger, message_id=f"receipt-source-{field}")
    run = await service.plan(
        MemoryRebuildSelection(all_events=True), actor_user_id=await _admin(database)
    )
    await service.start(run.public_id, actor_user_id=await _admin(database))
    worker = MemoryRebuildWorker(service, interval_seconds=1)
    await worker.process_once()
    await worker.process_once()
    await service.set_review(
        run.public_id, "all", approved=False, actor_user_id=await _admin(database)
    )
    await service.commit(run.public_id, actor_user_id=await _admin(database))
    original = database.immediate_session
    changed = False

    @asynccontextmanager
    async def race():
        nonlocal changed
        if not changed:
            changed = True
            async with original() as writer:
                event = await writer.get(ChatEventModel, source.id)
                setattr(
                    event,
                    field,
                    serialize_transcripts((AudioTranscript("current", 0, "现在来源已经改变"),))
                    if field == "audio_transcript"
                    else "现在来源已经改变",
                )
        async with original() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", race)
    await service.repository.complete_item_receipts(run.public_id, include_failed_live_jobs=False)
    async with database.read_session() as reader:
        assert await reader.scalar(select(MemoryJobModel.id)) is None
        assert (await reader.scalar(select(MemoryRebuildItemModel))).status == "staged"
    await service.repository.complete_item_receipts(run.public_id, include_failed_live_jobs=False)
    async with database.read_session() as reader:
        item = await reader.scalar(select(MemoryRebuildItemModel))
        assert (item.status, item.error_category) == ("skipped", "source_event_changed")
