"""Original canonical Memory subjects and paid decisions survive transport changes."""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update
from tests.unit.test_dream_owner_projection import _scene
from tests.unit.test_memory_rebuild import _admin, _service
from tests.unit.test_rebuild_receipt_finalization import _seed

from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.identity.canonical_repository import (
    create_person_binding,
    create_presence,
    find_identity_binding,
)
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.memory.dream.db_models import MemoryDreamClusterPreviewModel
from qq_ai_bot.memory.dream.models import DreamOutput
from qq_ai_bot.memory.dream.service import DreamBudgetExhausted
from qq_ai_bot.memory.rebuild.models import MemoryRebuildSelection
from qq_ai_bot.memory.rebuild.repository import MemoryRebuildRepository
from qq_ai_bot.model_runtime.request_accounting import before_provider_request
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemoryJobModel,
    MemoryRebuildItemModel,
    MemoryRebuildProposalModel,
)
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


async def transfer_qq(database):
    from tests.support.memory_identity import transfer_account

    await transfer_account(database, "1001")


async def test_qq_forget_preserves_same_number_telegram_rebuild_proposal(database):
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        binding = await create_person_binding(
            writer,
            external_account_id="1001",
            display_name="TG owner",
            now=now,
            platform="telegram",
        )
        presence = await create_presence(
            writer, external_account_id="telegram-bot", now=now, platform="telegram"
        )
        tg_owner, tg_presence = binding.person_id, presence.id
        qq_owner = (await find_identity_binding(writer, "1001", platform="qq")).person_id
    source = (
        await ScopedEventLedgerUnitOfWork(database).append(
            scope=ConversationScope.private(
                "telegram-bot", "1001", platform="telegram", presence_id=tg_presence
            ),
            sender_user_id="1001",
            platform_message_id="tg-source",
            direction="inbound",
            content="我住在杭州",
        )
    ).event
    _, _, _, provider, service = await _service(database)
    run = await service.plan(
        MemoryRebuildSelection(
            all_events=True, minimum_event_id=source.id, maximum_event_id=source.id
        ),
        actor_user_id=await _admin(database),
    )
    run = await service.start(run.public_id, actor_user_id=await _admin(database))
    assert await service.process_extraction_once(run) == 1
    assert provider.requests == 1
    async with database.sessions() as reader:
        proposal = await reader.scalar(select(MemoryRebuildProposalModel))
        proposal_id = proposal.id
        assert proposal.canonical_subject_person_id == tg_owner != qq_owner
    await transfer_qq(database)
    assert await PeopleRepository(database, memory_rebuilds=service.repository).delete_person_id(
        qq_owner
    )
    async with database.sessions() as reader:
        assert (await reader.get(ChatEventModel, source.id)).author_person_id == tg_owner
        assert (
            await reader.get(MemoryRebuildProposalModel, proposal_id)
        ).canonical_subject_person_id == tg_owner


@pytest.mark.parametrize("inject_rebuilds", [False, True])
async def test_forget_removes_third_party_proposal_by_its_original_person(
    database, inject_rebuilds
):
    async with database.sessions() as reader:
        target = (await find_identity_binding(reader, "1001")).person_id
    claim = {
        "subject_ref": f"member_{target}",
        "scope_type": "person_group",
        "kind": "fact",
        "memory_key": "profile:city",
        "category": "profile",
        "content": "住在杭州",
        "evidence_quote": "1001住在杭州",
        "confidence": 0.9,
    }
    provider = FakeLLMProvider(lambda _: json.dumps({"claims": [claim]}))
    _, ledger, _, _, service = await _service(
        database, provider=provider, people=PeopleRepository(database)
    )
    source, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id="third-party",
        scope_type=ScopeType.GROUP,
        group_id="2001",
        sender_user_id="1002",
        direction="inbound",
        content="1001住在杭州",
        segments=({"type": "at", "data": {"qq": "1001"}},),
    )
    run = await service.plan(
        MemoryRebuildSelection(
            all_events=True,
            minimum_event_id=source.id,
            maximum_event_id=source.id,
            third_party_mode="trusted_metadata",
        ),
        actor_user_id=await _admin(database),
    )
    run = await service.start(run.public_id, actor_user_id=await _admin(database))
    assert await service.process_extraction_once(run) == 1
    async with database.sessions() as reader:
        proposal = await reader.scalar(select(MemoryRebuildProposalModel))
        assert proposal is not None and proposal.canonical_subject_person_id == target
        proposal_id = proposal.id
        assert source.author_person_id != target
    people = PeopleRepository(
        database, memory_rebuilds=service.repository if inject_rebuilds else None
    )
    assert await people.delete_person_id(target)
    async with database.sessions() as reader:
        assert await reader.get(MemoryRebuildProposalModel, proposal_id) is None
        assert (
            await reader.get(ChatEventModel, source.id)
        ).author_person_id == source.author_person_id


class AccountingProvider(FakeLLMProvider):
    async def complete(self, request):
        account = before_provider_request.get()
        if account is not None:
            await account()
        return await super().complete(request)


@pytest.mark.parametrize("budget_spent", [False, True])
async def test_paid_dream_preview_survives_account_transfer_without_another_call(
    database, budget_spent
):
    provider = AccountingProvider(
        lambda _: '{"actions":[{"operation":"keep","source_refs":["memory_1","memory_2"]}]}'
    )
    service, _, dreams, run, cluster, rows = await _scene(database, provider, owner="person_group")
    preview = await service.preview_cluster(run.public_id, cluster.id)
    before_fingerprint = (await service._input(rows))[2]
    if budget_spent:
        assert await dreams.reserve_model_call(
            run_public_id=run.public_id, cluster_id=cluster.id, maximum=1
        )
    await transfer_qq(database)
    current = await dreams.cluster_facts(cluster)
    assert current[0].subject_user_id is None
    assert (await service._input(current))[2] == before_fingerprint
    assert await service.process_cluster(await dreams.get_run(run.public_id), cluster) == (
        0,
        1,
        True,
    )
    assert len(provider.requests) == 1
    async with database.sessions() as reader:
        saved = await reader.scalar(
            select(MemoryDreamClusterPreviewModel).where(
                MemoryDreamClusterPreviewModel.public_id == preview.preview_public_id
            )
        )
        assert saved.source_fingerprint == before_fingerprint and saved.status == "applied"
    assert (await dreams.get_run(run.public_id)).model_calls == int(budget_spent)


@pytest.mark.parametrize("legacy", [False, True])
async def test_changed_dream_source_cannot_reuse_paid_preview_at_request_limit(database, legacy):
    provider = AccountingProvider(
        lambda _: '{"actions":[{"operation":"keep","source_refs":["memory_1","memory_2"]}]}'
    )
    service, facts, dreams, run, cluster, rows = await _scene(
        database, provider, owner="person_group"
    )
    preview = await service.preview_cluster(run.public_id, cluster.id)
    preview_id = preview.preview_public_id
    if legacy:
        _, _, legacy_fingerprint = await service._input(
            rows,
            legacy_owners={
                "subject_user_id": rows[0].subject_user_id,
                "group_id": rows[0].group_id,
                "visibility_user_id": None,
                "visibility_group_id": None,
            },
        )
        preview_id = await dreams.save_preview(
            cluster_id=cluster.id,
            source_fingerprint=legacy_fingerprint,
            proposal=DreamOutput(actions=preview.actions),
            model_calls=1,
            source_characters=sum(len(row.content) for row in rows),
            output_characters=0,
        )
    assert await dreams.reserve_model_call(
        run_public_id=run.public_id, cluster_id=cluster.id, maximum=1
    )
    evidence = (await facts.list_evidence(rows[0].id))[0]
    async with database.immediate_session() as writer:
        await writer.execute(
            update(MemoryEvidenceModel)
            .where(MemoryEvidenceModel.id == evidence.id)
            .values(excerpt="原证据已经修改")
        )
    with pytest.raises(DreamBudgetExhausted):
        await service.process_cluster(await dreams.get_run(run.public_id), cluster)
    assert len(provider.requests) == 1
    assert (await dreams.get_run(run.public_id)).model_calls == 1
    async with database.sessions() as reader:
        saved = await reader.scalar(
            select(MemoryDreamClusterPreviewModel).where(
                MemoryDreamClusterPreviewModel.public_id == preview_id
            )
        )
        assert saved.status == "stale"


async def test_rebuild_finalization_keeps_original_owner_and_receipt_after_transfer(database):
    seeded = await _seed(database, 1, proposals=True)
    async with database.sessions() as reader:
        owner = (await reader.get(ChatEventModel, seeded.event_ids[0])).author_person_id
    await transfer_qq(database)
    repository = MemoryRebuildRepository(database)
    assert (
        await repository.complete_item_receipts(seeded.public_id, include_failed_live_jobs=False)
        == 1
    )
    async with database.sessions() as reader:
        item = await reader.get(MemoryRebuildItemModel, seeded.item_ids[0])
        receipt = await reader.scalar(
            select(MemoryJobModel).where(MemoryJobModel.event_id == item.event_id)
        )
        assert item.status == "committed" and item.error_category is None
        assert receipt.canonical_person_id == owner and receipt.status == "done"
        receipt_id = receipt.id
    assert (
        await repository.complete_item_receipts(seeded.public_id, include_failed_live_jobs=False)
        == 0
    )
    async with database.sessions() as reader:
        assert (await reader.get(MemoryJobModel, receipt_id)).canonical_person_id == owner
