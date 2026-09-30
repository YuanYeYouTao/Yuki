"""Dream decisions cannot reuse hidden model evidence when its count stays equal."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, select, update
from tests.conftest import make_settings
from tests.unit.test_memory_dream import _empty_dream_statistics, _fact_with_evidence, _services

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.dream.db_models import MemoryDreamOperationModel
from qq_ai_bot.memory.dream.models import DreamAction, DreamOperationType, DreamOutput, DreamRunMode
from qq_ai_bot.memory.dream.service import DreamService
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemoryMutationReceiptModel,
)


@pytest.mark.parametrize("window", ["model", "retry"])
async def test_dream_rejects_hidden_input_even_when_readable_evidence_count_is_unchanged(
    database, monkeypatch, window
):
    mutations, facts, ledger, dreams = _services(database)
    sources = tuple(
        [
            await _fact_with_evidence(
                facts,
                ledger,
                message_id=f"dream-input-{index}",
                memory_key=f"dream-input:{index}",
                content=f"original model evidence {index}",
            )
            for index in range(2)
        ]
    )
    original = (await facts.list_evidence(sources[0].id))[0]
    alternate, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id="dream-input-alternate",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content="different evidence which the model never saw",
        group_id="3001",
    )
    async with database.immediate_session() as session:
        await session.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == alternate.id)
            .values(suppression_status="duplicate", utterance_fingerprint="b" * 64)
        )
        session.add(
            MemoryEvidenceModel(
                fact_id=sources[0].id,
                event_id=alternate.id,
                source_speaker_user_id=original.source_speaker_user_id,
                relation=original.relation.value,
                confidence=original.confidence,
                authority=original.authority.value,
                excerpt="different evidence which the model never saw",
                created_at=datetime.now(UTC),
            )
        )
    assert (await facts.get_fact(sources[0].id)).evidence_count == 1
    service = object.__new__(DreamService)
    service._settings = make_settings(database.url)
    service._facts, service._mutations, service._repository = facts, mutations, dreams
    run = await dreams.create_run(
        mode=DreamRunMode.FULL,
        statistics=_empty_dream_statistics(),
        clusters=(
            (
                "input-snapshot",
                "partition",
                "8000",
                "fact",
                tuple(row.id for row in sources),
                service._cluster_fingerprint(sources),
            ),
        ),
        snapshot_max_fact_id=sources[-1].id,
        actor_user_id=None,
        scheduled_slot=None,
    )
    assert await dreams.start_run(run.public_id)
    cluster = await dreams.claim_next_cluster(run.public_id)
    calls = 0

    async def swap():
        async with database.immediate_session() as writer:
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == original.event_id)
                .values(suppression_status="duplicate", utterance_fingerprint="a" * 64)
            )
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == alternate.id)
                .values(suppression_status="keeper")
            )
        assert (await facts.get_fact(sources[0].id)).evidence_count == 1

    prepare = facts.prepare_evidence_write
    prepared_once = False

    async def prepare_and_swap(fact_ids, *, session, targets=()):
        nonlocal prepared_once
        await prepare(fact_ids, session=session, targets=targets)
        if window == "retry" and not prepared_once:
            prepared_once = True
            await swap()

    monkeypatch.setattr(facts, "prepare_evidence_write", prepare_and_swap)

    async def decide(payload, **_kwargs):
        nonlocal calls
        calls += 1
        assert payload.memories[0].evidence[0].excerpt == original.excerpt
        if window == "model":
            await swap()
        return DreamOutput(
            actions=(
                DreamAction(
                    operation=DreamOperationType.SYNTHESIZE,
                    source_refs=("memory_1", "memory_2"),
                    anchor_ref="memory_1",
                    content="a decision derived from the old hidden evidence",
                ),
            )
        ), 1

    monkeypatch.setattr(service, "_decide", decide)
    native_errors = []

    def handle_error(error_context):
        native_errors.append(getattr(error_context.original_exception, "sqlite_errorcode", None))

    engine = database.engine.sync_engine
    event.listen(engine, "handle_error", handle_error)
    try:
        with pytest.raises(RuntimeError, match="dream_input_snapshot_changed"):
            await service.process_cluster(run, cluster)
    finally:
        event.remove(engine, "handle_error", handle_error)
    assert native_errors == ([517] if window == "retry" else [])
    assert calls == 1
    async with database.sessions() as reader:
        assert await reader.scalar(select(func.count()).select_from(MemoryDreamOperationModel)) == 0
        assert (
            await reader.scalar(select(func.count()).select_from(MemoryMutationReceiptModel)) == 0
        )
    assert tuple([(await facts.get_fact(row.id)).status.value for row in sources]) == (
        "active",
        "active",
    )


async def test_dream_saved_input_proof_reuses_preview_and_commits_actual_merge(
    database, monkeypatch
):
    mutations, facts, ledger, dreams = _services(database)
    sources = tuple(
        [
            await _fact_with_evidence(
                facts,
                ledger,
                message_id=f"dream-proof-merge-{index}",
                memory_key=f"dream-proof-merge:{index}",
                content=f"merge evidence {index}",
            )
            for index in range(2)
        ]
    )
    service = object.__new__(DreamService)
    service._settings = make_settings(database.url)
    service._facts, service._mutations, service._repository = facts, mutations, dreams
    run = await dreams.create_run(
        mode=DreamRunMode.FULL,
        statistics=_empty_dream_statistics(),
        clusters=(
            (
                "proof-merge",
                "partition",
                "8000",
                "fact",
                tuple(row.id for row in sources),
                service._cluster_fingerprint(sources),
            ),
        ),
        snapshot_max_fact_id=sources[-1].id,
        actor_user_id=None,
        scheduled_slot=None,
    )
    calls = 0

    async def decide(payload, **_kwargs):
        nonlocal calls
        calls += 1
        assert len(payload.memories) == 2
        return DreamOutput(
            actions=(
                DreamAction(
                    operation=DreamOperationType.MERGE,
                    source_refs=("memory_1", "memory_2"),
                    anchor_ref="memory_1",
                ),
            )
        ), 1

    async def unexpected_decide(*_args, **_kwargs):
        raise AssertionError("a matching saved preview must not repeat the model")

    monkeypatch.setattr(service, "_preview_decide", decide)
    monkeypatch.setattr(service, "_decide", unexpected_decide)
    assert await dreams.start_run(run.public_id)
    cluster = await dreams.claim_next_cluster(run.public_id)
    preview = await service.preview_cluster(run.public_id, cluster.id)
    assert preview.preview_public_id is not None
    assert await service.process_cluster(run, cluster) == (0, 1, True)
    assert calls == 1
    async with database.sessions() as reader:
        assert await reader.scalar(select(func.count()).select_from(MemoryDreamOperationModel)) == 1
        assert (
            await reader.scalar(select(func.count()).select_from(MemoryMutationReceiptModel)) == 1
        )
    current = [await facts.get_fact(row.id) for row in sources]
    assert sorted(row.status.value for row in current) == ["active", "superseded"]
    assert next(row for row in current if row.status.value == "active").evidence_count == 2
