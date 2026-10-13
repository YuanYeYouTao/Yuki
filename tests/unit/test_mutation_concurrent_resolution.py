"""Independent memory changes prepare concurrently and retain writer receipts."""

import asyncio

import pytest
from tests.unit.test_memory_mutation import _context, _event, _service
from tests.unit.test_memory_v2 import _claim

from qq_ai_bot.memory.claim_processor import MemoryProcessingContext
from qq_ai_bot.memory.enums import MemoryScopeType
from qq_ai_bot.memory.mutation.models import (
    MemoryMutationOperation,
    MemoryMutationRequest,
    MemoryMutationTarget,
)
from qq_ai_bot.memory.repository import MemoryJobRepository


@pytest.mark.parametrize("entry", ["tool", "worker"])
async def test_independent_resolutions_do_not_hold_a_global_mutation_lock(
    database, monkeypatch, entry
):
    service, facts, ledger, processor = _service(database)
    sources = [
        await _event(
            ledger, message_id=f"parallel-{user}", sender_user_id=user, content="我准备考研"
        )
        for user in ("1001", "2002")
    ]
    both_entered = asyncio.Event()
    count = 0
    original = service._receipts.find

    async def find(*args, **kwargs):
        original_result = await original(*args, **kwargs)
        if kwargs.get("session") is not None:
            return original_result
        nonlocal count
        count += 1
        if count == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=3)
        return original_result

    monkeypatch.setattr(service._receipts, "find", find)
    if entry == "tool":
        request = MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="current_speaker", scope_type=MemoryScopeType.PERSON
            ),
            new_content="准备考研",
            memory_key="education:plan",
            category="education",
        )
        tasks = [service.mutate(request, _context(source)) for source in sources]
    else:
        jobs = MemoryJobRepository(database)
        for source in sources:
            assert await jobs.enqueue(source.id, f"private:{source.sender_user_id}")
        claimed = {job.event_id: job for job in await jobs.claim()}
        tasks = [
            service.mutate_validated_claim(
                processor.validate(_claim(), source),
                MemoryProcessingContext(event=source),
                conversation_key=f"private:{source.sender_user_id}",
                job=claimed[source.id],
            )
            for source in sources
        ]
    running = [asyncio.create_task(task) for task in tasks]
    try:
        results = await asyncio.gather(*running)
    finally:
        for task in running:
            if not task.done():
                task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
    assert all(result.ok and not result.deduplicated for result in results)
    assert results[0].mutation_id != results[1].mutation_id
    for source in sources:
        assert len(await facts.list_person(source.sender_user_id)) == 1
