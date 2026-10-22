"""Publication capacity reads active projections, not invalidated history."""

from datetime import UTC, datetime

from sqlalchemy import insert, select, update
from tests.support.sqlite_cost import capture_reads, read_vm_steps
from tests.unit.test_history_missing_body import scene
from tests.unit.test_projection_chunk_storage import _prepare

from yuki.conversation.frozen_fragments import FrozenFragments
from yuki.conversation.projection_models import PromptProjectionModel
from yuki.conversation.projections import PromptProjectionRepository


async def test_publication_capacity_ignores_invalidated_history(database):
    _ledger, context, _seed = await scene(database, 1)
    repository = PromptProjectionRepository(database, maximum_views=1)
    source = FrozenFragments.load([]).extend_history(
        context.history_fragments, context.history_event_fragments
    )
    table = PromptProjectionModel.__table__
    costs, previous = [], 0
    for size in (16, 640):
        # These rows represent expired replaceable views. The original event
        # ledger and current view are left untouched between measurements.
        async with database.immediate_session() as writer:
            await writer.execute(
                insert(table),
                [
                    dict(
                        view_key=f"invalidated-{i}",
                        conversation_id=context.read_version.conversation_id,
                        generation=1,
                        source_revision=0,
                        starts_after_event_id=0,
                        epoch_id=f"old-{i}",
                        context_key="old",
                        revision=1,
                        rebuild_reason="bootstrap",
                        invalidated_reason="capacity",
                        tail_ordinal=0,
                        item_count=0,
                        byte_size=2,
                        updated_at=datetime.now(UTC),
                    )
                    for i in range(previous, size)
                ],
            )
            # An actual active victim exercises both the aggregate and ordered
            # eviction query during the next real publication.
            await writer.execute(
                update(table)
                .where(table.c.view_key == "invalidated-0")
                .values(invalidated_reason=None)
            )
        existing = await repository.read(context.read_version.conversation_id)
        publish = await _prepare(repository, context, source, existing, reason="bootstrap")
        with capture_reads(database) as reads:
            async with database.immediate_session() as writer:
                current = await publish(writer)
        costs.append(read_vm_steps(database, reads))
        async with database.read_session() as reader:
            assert (
                await reader.scalar(
                    select(table.c.invalidated_reason).where(table.c.view_key == "invalidated-0")
                )
                == "capacity"
            )
        assert tuple(current.items()) == source.items
        previous = size
    assert costs[1] <= costs[0] * 4, costs
