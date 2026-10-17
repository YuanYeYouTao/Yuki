"""Normal legacy QQ ledger -> account transfer -> real rebuild extraction."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.unit.test_dream_owner_projection import _scene
from tests.unit.test_memory_rebuild import _admin, _service

from yuki.domain.conversations import ScopeType
from yuki.identity.canonical_repository import find_identity_binding
from yuki.llm.fake import FakeLLMProvider
from yuki.memory.dream.db_models import MemoryDreamClusterPreviewModel
from yuki.memory.rebuild.models import MemoryRebuildSelection
from yuki.persistence.models import MemoryRebuildProposalModel
from yuki.persistence.people_repository import PeopleRepository


async def merge_people(database, source, target):
    from yuki.identity.person_identity import merge_persons

    return await database.apply_snapshot_write(
        lambda session: merge_persons(session, source, target)
    )


async def test_forget_whole_family_preserves_original_social_receipt_and_other_person(
    database, tmp_path
):
    from datetime import UTC, datetime

    from tests.support.social_identity_cases import social_env

    from yuki.domain.conversations import ConversationScope
    from yuki.identity.db_models import CanonicalPersonModel
    from yuki.persistence.models import ChatEventModel, PersonAliasModel
    from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
    from yuki.social.db_models import SocialOperationModel
    from yuki.social.service import SocialContext

    env = await social_env(database, tmp_path)
    incoming = (
        await ScopedEventLedgerUnitOfWork(database).append(
            scope=ConversationScope.private(env.bot.self_id, "10001"),
            platform_message_id="family-private-source",
            sender_user_id="10001",
            direction="inbound",
            content="请确认收到私人资料",
        )
    ).event
    context = SocialContext(
        "family-send",
        "family-call",
        incoming.canonical_conversation_id,
        person_refs={"current_speaker": env.person},
    )
    sent = await env.service.execute("send_message", {"text": "收到"}, context)
    assert sent["status"] == "succeeded"
    calls = tuple(env.bot.calls)
    unbound = str(uuid4())
    now = datetime.now(UTC)
    async with database.sessions() as reader:
        target = (await find_identity_binding(reader, "1002")).person_id
        other = (await find_identity_binding(reader, "1003")).person_id
    async with database.immediate_session() as writer:
        writer.add(
            CanonicalPersonModel(
                id=unbound, enabled=True, revision=1, created_at=now, updated_at=now
            )
        )
        await writer.flush()
        writer.add(
            PersonAliasModel(
                canonical_person_id=unbound,
                alias="无账号人物",
                alias_type="nickname",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
    await merge_people(database, env.person, target)
    await merge_people(database, unbound, target)
    assert await PeopleRepository(database).delete_person_id(env.person)
    async with database.sessions() as reader:
        assert all(
            [
                await reader.get(CanonicalPersonModel, person) is None
                for person in (env.person, target, unbound)
            ]
        )
        assert await reader.get(CanonicalPersonModel, other) is not None
        assert await reader.get(ChatEventModel, incoming.id) is None
        receipt = await reader.get(SocialOperationModel, sent["operation_id"])
        assert receipt is not None and receipt.status == "succeeded"
        assert receipt.source_conversation_id == incoming.canonical_conversation_id
    assert tuple(env.bot.calls) == calls


async def test_forget_cleans_sandbox_and_detaches_other_authors_event_references(database):
    from yuki.domain.conversations import ConversationScope
    from yuki.persistence.models import ChatEventModel
    from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
    from yuki.sandbox.db_models import SandboxTaskContinuationModel
    from yuki.sandbox.task_repository import SandboxTaskRepository

    ledger = ScopedEventLedgerUnitOfWork(database)
    origin = (
        await ledger.append(
            scope=ConversationScope.group("8000", "2001"),
            platform_message_id="forget-original-group",
            sender_user_id="1001",
            direction="inbound",
            content="原人物的消息",
        )
    ).event
    reply = (
        await ledger.append(
            scope=origin.scope,
            platform_message_id="other-person-reply",
            sender_user_id="1003",
            direction="inbound",
            content="另一人物的独立内容",
            reply_to_event_id=origin.id,
        )
    ).event
    caused = (
        await ledger.append(
            scope=origin.scope,
            platform_message_id="original-causal-response",
            sender_user_id="8000",
            sender_is_bot=True,
            direction="outbound",
            origin="system_task",
            content="公开回复",
            caused_by_event_id=origin.id,
        )
    ).event
    private = (
        await ledger.append(
            scope=ConversationScope.private("8000", "1001"),
            platform_message_id="sandbox-private-source",
            sender_user_id="1001",
            direction="inbound",
            content="私有任务内容",
        )
    ).event
    tasks = SandboxTaskRepository(database)
    request_id, run_id = str(uuid4()), str(uuid4())
    await tasks.prepare(
        request_id,
        {"task": "私有任务"},
        {
            "conversation_id": private.canonical_conversation_id,
            "trigger_event_id": private.id,
        },
    )
    await tasks.receive(
        {
            "request_id": request_id,
            "run_id": run_id,
            "result": {
                "run_id": run_id,
                "status": "succeeded",
                "pending": False,
                "output": "私有结果",
            },
        }
    )
    assert await PeopleRepository(database).delete_person_id(origin.author_person_id)
    assert await tasks.get(request_id) is None
    async with database.sessions() as reader:
        assert await reader.get(SandboxTaskContinuationModel, request_id) is None
        kept = await reader.get(ChatEventModel, reply.id)
        assert kept.content == reply.content and kept.reply_to_event_id is None
        kept_causal = await reader.get(ChatEventModel, caused.id)
        assert kept_causal.content == caused.content and kept_causal.caused_by_event_id is None
        assert await reader.get(ChatEventModel, origin.id) is None


async def test_current_memory_scope_reads_merged_person_and_private_self_without_new_binding(
    database,
):
    from yuki.domain.conversations import ConversationScope
    from yuki.memory.enums import (
        MemoryAuthority,
        MemoryEvidenceRelation,
        MemoryKind,
        MemoryScopeType,
        MemorySourceType,
        SelfMemoryVisibility,
    )
    from yuki.memory.models import MemoryEvidenceCreate, MemoryFactCreate, MemoryFactQuery
    from yuki.memory.read_scope import MemoryReadScopeResolver
    from yuki.memory.repository import MemoryFactRepository
    from yuki.memory.service import MemoryFactService
    from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    async with database.sessions() as reader:
        source = (await find_identity_binding(reader, "1001")).person_id
        target = (await find_identity_binding(reader, "1002")).person_id
        other = (await find_identity_binding(reader, "1003")).person_id
    event = (
        await ScopedEventLedgerUnitOfWork(database).append(
            scope=ConversationScope.private("8000", "1001"),
            platform_message_id="merge-memory-source",
            sender_user_id="1001",
            direction="inbound",
            content="原人物的偏好与私人自省",
        )
    ).event
    repository = MemoryFactRepository(database)
    service = MemoryFactService(repository)
    facts = []
    for private_self in (False, True):
        facts.append(
            await service.remember(
                MemoryFactCreate(
                    scope_type=MemoryScopeType.SELF if private_self else MemoryScopeType.PERSON,
                    canonical_subject_person_id=None if private_self else source,
                    visibility_type=SelfMemoryVisibility.PRIVATE if private_self else None,
                    canonical_visibility_person_id=source if private_self else None,
                    kind=MemoryKind.FACT,
                    memory_key="merge:original",
                    category="profile",
                    content=event.content,
                    source_type=MemorySourceType.AUTOMATIC,
                    authority=MemoryAuthority.SELF_REPORT,
                ),
                evidence=MemoryEvidenceCreate(
                    event_id=event.id,
                    source_speaker_user_id="1001",
                    relation=MemoryEvidenceRelation.SELF_STATEMENT,
                    authority=MemoryAuthority.SELF_REPORT,
                    excerpt=event.content,
                ),
            )
        )
    resolver = MemoryReadScopeResolver(database)
    assert not await repository.get_active_authorized(
        await resolver.authorized(target, private_scene=True), tuple(fact.id for fact in facts)
    )
    await merge_people(database, source, target)
    scope = await resolver.authorized(target, private_scene=True)
    found = await repository.get_active_authorized(scope, tuple(fact.id for fact in facts))
    assert {fact.id for fact in found} == {fact.id for fact in facts}
    assert not await repository.get_active_authorized(
        await resolver.authorized(other, private_scene=True), tuple(fact.id for fact in facts)
    )
    assert await service.person_owns_fact(facts[0], actor_person_id=target)
    assert not await service.person_owns_fact(facts[0], actor_person_id=other)
    selected = await repository.list_facts(
        MemoryFactQuery(
            scope_type=MemoryScopeType.PERSON,
            canonical_subject_person_id=target,
        )
    )
    assert [fact.id for fact in selected] == [facts[0].id]
    assert found[0].canonical_subject_person_id == source
    assert found[1].canonical_visibility_person_id == source
    assert len(await repository.list_evidence(facts[0].id)) == 1


@pytest.mark.parametrize("owner", ["person_group", "self_private"])
async def test_paid_dream_preview_survives_person_merge_with_original_proof(database, owner):
    from yuki.memory.dream.service import DreamBudgetExhausted

    provider = FakeLLMProvider(
        lambda _: '{"actions":[{"operation":"keep","source_refs":["memory_1","memory_2"]}]}'
    )
    service, facts, dreams, run, cluster, rows = await _scene(database, provider, owner=owner)
    from yuki.model_runtime.request_accounting import before_provider_request

    async def reserve_paid_request():
        assert await dreams.reserve_model_call(
            run_public_id=run.public_id, cluster_id=cluster.id, maximum=1
        )

    await reserve_paid_request()
    await service.preview_cluster(run.public_id, cluster.id)
    async with database.sessions() as reader:
        preview = await reader.scalar(select(MemoryDreamClusterPreviewModel))
        original = preview.source_fingerprint, preview.proposal_json
        source = (await find_identity_binding(reader, "1001")).person_id
        target = (await find_identity_binding(reader, "1002")).person_id
    await merge_people(database, source, target)

    async def no_new_request():
        raise DreamBudgetExhausted("all new requests exhausted")

    run = await dreams.get_run(run.public_id)
    assert run.model_calls == 1
    token = before_provider_request.set(no_new_request)
    try:
        assert await service.process_cluster(run, cluster) == (0, 1, True)
    finally:
        before_provider_request.reset(token)
    assert len(provider.requests) == 1
    async with database.sessions() as reader:
        preview = await reader.get(MemoryDreamClusterPreviewModel, preview.id)
        assert (preview.source_fingerprint, preview.proposal_json) == original
    async with database.sessions() as reader:
        originals = await facts.repository.get_facts(tuple(row.id for row in rows), session=reader)
    assert [row.canonical_subject_person_id for row in originals] == [
        row.canonical_subject_person_id for row in rows
    ]


async def test_rebuild_keeps_original_legacy_mention_after_account_transfer(database):
    async with database.sessions() as reader:
        old_owner = (await find_identity_binding(reader, "1001")).person_id

    def extract(request):
        payload = json.loads(request.messages[-1].content)
        member = next(
            s for s in payload["available_subjects"] if s["subject_ref"].startswith("member_")
        )
        return json.dumps(
            {
                "claims": [
                    {
                        "subject_ref": member["subject_ref"],
                        "scope_type": "person_group",
                        "kind": "fact",
                        "memory_key": "profile:city",
                        "category": "profile",
                        "content": "住在杭州",
                        "evidence_quote": "1001住在杭州",
                        "confidence": 0.9,
                    }
                ]
            }
        )

    provider = FakeLLMProvider(extract)
    _, ledger, _, _, service = await _service(
        database, provider=provider, people=PeopleRepository(database)
    )
    event, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id=str(uuid4()),
        scope_type=ScopeType.GROUP,
        group_id="2001",
        sender_user_id="1002",
        direction="inbound",
        content="1001住在杭州",
        segments=({"type": "at", "data": {"qq": "1001"}},),
    )
    assert not event.mentioned_person_ids  # Original pre-platform ledger has transport selectors.
    from tests.support.memory_identity import transfer_account

    async with database.sessions() as reader:
        current_owner = (await find_identity_binding(reader, "1003")).person_id
    await transfer_account(database, "1001", "1003")
    assert (await ledger.hydrate_rebuild_subjects(event)).mentioned_person_ids == (old_owner,)
    run = await service.plan(
        MemoryRebuildSelection(
            all_events=True,
            minimum_event_id=event.id,
            maximum_event_id=event.id,
            third_party_mode="trusted_metadata",
        ),
        actor_user_id=await _admin(database),
    )
    run = await service.start(run.public_id, actor_user_id=await _admin(database))
    assert await service.process_extraction_once(run) == 1
    async with database.sessions() as reader:
        proposal = await reader.scalar(select(MemoryRebuildProposalModel))
        assert proposal is not None
        assert proposal.canonical_subject_person_id == old_owner
        assert proposal.canonical_subject_person_id != current_owner
    assert await service.process_extraction_once(run) == 0
    assert (
        await service.set_review(
            run.public_id, "all", approved=True, actor_user_id=await _admin(database)
        )
        == 1
    )
    run = await service.commit(run.public_id, actor_user_id=await _admin(database))
    assert await service.process_commit_once(run) == 1
    from yuki.persistence.models import MemoryFactModel

    async with database.sessions() as reader:
        fact = await reader.scalar(select(MemoryFactModel))
        assert fact is not None
        assert fact.canonical_subject_person_id == old_owner != current_owner


async def test_new_dream_merges_same_person_original_facts_without_rewriting_source_owners(
    database,
):
    from tests.unit.test_dream_owner_projection import _service as dream_service
    from tests.unit.test_memory_dream import _empty_dream_statistics

    from yuki.conversation.canonical_db_models import CanonicalConversationModel
    from yuki.memory.dream.models import DreamRunMode
    from yuki.memory.enums import (
        MemoryAuthority,
        MemoryEvidenceRelation,
        MemoryKind,
        MemoryScopeType,
        MemorySourceType,
    )
    from yuki.memory.models import MemoryEvidenceCreate, MemoryFactCreate
    from yuki.memory.quality.audit import MemoryProductionQualityAudit

    provider = FakeLLMProvider(
        lambda _: (
            '{"actions":[{"operation":"merge","source_refs":["memory_1","memory_2"],"anchor_ref":"memory_1"}]}'
        )
    )
    service, facts, ledger, dreams = dream_service(database, provider)
    originals = []
    for account in ("1001", "1002", "1003"):
        await PeopleRepository(database).observe(user_id=account, nickname=account, group_id="3001")
        event, _ = await ledger.append(
            bot_user_id="8000",
            platform_message_id=f"cross-owner-{account}",
            scope_type=ScopeType.GROUP,
            group_id="3001",
            sender_user_id=account,
            direction="inbound",
            content="我喜欢玩围棋",
        )
        async with database.sessions() as reader:
            conversation = await reader.get(
                CanonicalConversationModel, event.canonical_conversation_id
            )
        originals.append(
            await facts.remember(
                MemoryFactCreate(
                    scope_type=MemoryScopeType.PERSON_GROUP,
                    canonical_subject_person_id=event.author_person_id,
                    canonical_subject_space_id=conversation.space_id,
                    kind=MemoryKind.FACT,
                    memory_key="hobby:go",
                    category="profile",
                    content=event.content,
                    source_type=MemorySourceType.AUTOMATIC,
                    authority=MemoryAuthority.SELF_REPORT,
                ),
                evidence=MemoryEvidenceCreate(
                    event_id=event.id,
                    source_speaker_user_id=account,
                    relation=MemoryEvidenceRelation.SELF_STATEMENT,
                    authority=MemoryAuthority.SELF_REPORT,
                    excerpt=event.content,
                ),
            )
        )
    await merge_people(
        database, originals[0].canonical_subject_person_id, originals[1].canonical_subject_person_id
    )
    from tests.unit.test_memory_retrieval import _query

    from yuki.memory.fts import SQLiteMemoryFTSIndex
    from yuki.memory.read_scope import MemoryReadScopeResolver
    from yuki.memory.retrieval import MemoryRetriever

    lexical = SQLiteMemoryFTSIndex(database)
    await lexical.rebuild()
    found = await MemoryRetriever(
        repository=facts.repository, lexical_index=lexical
    ).retrieve_authorized(
        _query("我喜欢玩围棋").model_copy(update={"semantic_enabled": False}),
        await MemoryReadScopeResolver(database).authorized(
            originals[1].canonical_subject_person_id
        ),
        limit=10,
    )
    assert len(found.blocks) == 2
    own = [
        hit
        for hit in found.hits
        if hit.target.canonical_subject_person_id == originals[1].canonical_subject_person_id
    ]
    assert {hit.fact.id for hit in own} == {row.id for row in originals[:2]}
    assert {hit.fact.canonical_subject_person_id for hit in own} == {
        row.canonical_subject_person_id for row in originals[:2]
    }
    selected = tuple(originals[:2])

    def cluster(rows):
        return (
            (
                str(uuid4()),
                "partition",
                "8000",
                "fact",
                tuple(row.id for row in rows),
                service._cluster_fingerprint(tuple(rows)),
            ),
        )

    with pytest.raises(ValueError, match="incomplete_dream_owner"):
        await dreams.create_run(
            mode=DreamRunMode.FULL,
            statistics=_empty_dream_statistics(),
            clusters=cluster((originals[0], originals[2])),
            snapshot_max_fact_id=originals[-1].id,
            actor_user_id=None,
            scheduled_slot=None,
        )
    run = await dreams.create_run(
        mode=DreamRunMode.FULL,
        statistics=_empty_dream_statistics(),
        clusters=cluster(selected),
        snapshot_max_fact_id=originals[-1].id,
        actor_user_id=None,
        scheduled_slot=None,
    )
    await dreams.start_run(run.public_id)
    claimed = await dreams.claim_next_cluster(run.public_id)
    assert await service.process_cluster(await dreams.get_run(run.public_id), claimed) == (
        0,
        1,
        True,
    )
    assert len(provider.requests) == 1
    async with database.sessions() as reader:
        kept = await facts.repository.get_facts(tuple(row.id for row in originals), session=reader)
        assert [row.canonical_subject_person_id for row in kept] == [
            row.canonical_subject_person_id for row in originals
        ]
        assert len(await facts.repository.list_evidence(originals[0].id, session=reader)) == 2
    audit = await MemoryProductionQualityAudit(database).run()
    assert not any(
        issue.count for issue in audit.issues if issue.issue_code == "cross_target_relation"
    )


async def test_forget_target_only_keeps_other_group_self_experience_and_tool_receipt(database):
    from tests.unit.test_memory_mutation import _service as mutation_service
    from tests.unit.test_self_initiative_memory import claim, record

    from yuki.conversation.autonomy_binding import InitiativeSource, InitiativeSourceKind
    from yuki.conversation.autonomy_db_models import (
        InitiativeFeedbackModel,
        InitiativeRunModel,
    )
    from yuki.conversation.autonomy_repository import AutonomyRepository
    from yuki.conversation.canonical_db_models import CanonicalConversationModel
    from yuki.conversation.initiative_sources import source_revision
    from yuki.memory.enums import MemoryKind, MemoryScopeType, SelfMemoryVisibility
    from yuki.memory.mutation.models import (
        MemoryDecisionActorType,
        MemoryMutationContext,
        MemoryMutationOperation,
        MemoryMutationRequest,
        MemoryMutationTarget,
    )
    from yuki.memory.subjects import ResolvedSubject
    from yuki.persistence.models import ChatEventModel, MemoryToolReceiptModel

    service, facts, ledger, _processor = mutation_service(database, self_memory_enabled=True)
    event, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id="other-group-source",
        scope_type=ScopeType.GROUP,
        group_id="3001",
        sender_user_id="1003",
        direction="inbound",
        content="另一个人的群内请求",
    )
    async with database.sessions() as reader:
        conversation = await reader.get(CanonicalConversationModel, event.canonical_conversation_id)
        target = (await find_identity_binding(reader, "1001")).person_id
        source_row = await reader.get(ChatEventModel, event.id)
        source = (
            InitiativeSource(
                InitiativeSourceKind.EVENT, str(event.id), str(source_revision(source_row))
            ),
        )
    autonomy = AutonomyRepository(database)
    binding = await autonomy.ensure_binding(conversation.id, conversation.generation)
    binding = await autonomy.transition(binding, master_enabled=True, external_enabled=True)
    admission = await autonomy.accept_host_proposal(
        proposal_id=str(uuid4()),
        binding=binding,
        owner=binding.effective_owner,
        space_id=conversation.space_id,
        presence_id=event.ingress_presence_id,
        sources=source,
        source_guard=source,
        target_person_id=target,
        support_refs=(f"event:{event.id}",),
    )
    assert admission.outcome == "accepted"
    run_id = admission.run.run_id
    await record(database, event, run_id)
    await autonomy.record_feedback(run_id, sequence=1, outcome="no_reply")
    (batch,) = await claim(database)
    result = await service.mutate_resolved(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(subject_ref="self", scope_type=MemoryScopeType.SELF),
            new_content="我独立完成了曲线绘图并校验了结果。",
            memory_key="self_episode:kept",
            category="self_episode",
            kind=MemoryKind.EPISODE,
            importance=4,
            reason="实际独立经历",
            evidence_quote="已生成并校验曲线绘图",
        ),
        MemoryMutationContext(
            event=None,
            conversation_key="unused",
            turn_origin="memory_self_reflection",
            delegation_mode="self_reflection",
            trigger_actor_user_id="",
            executed_by_bot_user_id="",
            decision_actor_type=MemoryDecisionActorType.REFLECTION,
            decision_actor_id="yuki_self_reflection",
            initiative_run_id=run_id,
            evidence_tool_receipt_id=batch.first_receipt_id,
        ),
        target=ResolvedSubject(
            MemoryScopeType.SELF, None, None, SelfMemoryVisibility.GROUP, None, "3001"
        ),
        self_reflection_result=(batch.run_id, "episode", 0),
    )
    assert result.ok, result.reason_code
    assert await PeopleRepository(database).delete_person_id(target)
    async with database.sessions() as reader:
        run = await reader.get(InitiativeRunModel, run_id)
        assert run is not None and run.target_person_id is None and run.state == "no_reply"
        assert await reader.get(InitiativeFeedbackModel, (run_id, 1)) is not None
        assert await reader.get(MemoryToolReceiptModel, batch.first_receipt_id) is not None
        assert await reader.get(ChatEventModel, event.id) is not None
        assert await facts.repository.get_fact(result.new_fact_id, session=reader) is not None
        assert len(await facts.repository.list_evidence(result.new_fact_id, session=reader)) == 1


@pytest.mark.parametrize("support_only", [False, True])
async def test_forget_removes_initiative_receipts_with_actually_erased_source(
    database, support_only
):
    from tests.unit.test_self_initiative_memory import record

    from yuki.conversation.autonomy_binding import InitiativeSource, InitiativeSourceKind
    from yuki.conversation.autonomy_db_models import (
        InitiativeFeedbackModel,
        InitiativeRunModel,
    )
    from yuki.conversation.autonomy_repository import AutonomyRepository
    from yuki.conversation.canonical_db_models import CanonicalConversationModel
    from yuki.conversation.initiative_sources import source_revision
    from yuki.domain.conversations import ConversationScope
    from yuki.persistence.models import ChatEventModel, MemoryToolReceiptModel
    from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    ledger = ScopedEventLedgerUnitOfWork(database)
    events = []
    for account in ("1001", "1003"):
        events.append(
            (
                await ledger.append(
                    scope=ConversationScope.group("8000", "3001"),
                    platform_message_id=f"erasure-source-{account}",
                    sender_user_id=account,
                    direction="inbound",
                    content=f"{account}的原始输入",
                )
            ).event
        )
    erased, retained = events
    focus = retained if support_only else erased
    async with database.sessions() as reader:
        conversation = await reader.get(CanonicalConversationModel, focus.canonical_conversation_id)
        source_row = await reader.get(ChatEventModel, focus.id)
        source = (
            InitiativeSource(
                InitiativeSourceKind.EVENT, str(focus.id), str(source_revision(source_row))
            ),
        )
    autonomy = AutonomyRepository(database)
    binding = await autonomy.ensure_binding(conversation.id, conversation.generation)
    binding = await autonomy.transition(binding, master_enabled=True, external_enabled=True)
    admission = await autonomy.accept_host_proposal(
        proposal_id=str(uuid4()),
        binding=binding,
        owner=binding.effective_owner,
        space_id=conversation.space_id,
        presence_id=focus.ingress_presence_id,
        sources=source,
        source_guard=source,
        support_refs=(f"event:{erased.id}",),
    )
    assert admission.outcome == "accepted"
    run_id = admission.run.run_id
    await record(database, focus, run_id)
    await autonomy.record_feedback(run_id, sequence=1, outcome="no_reply")
    async with database.sessions() as reader:
        receipt = await reader.scalar(
            select(MemoryToolReceiptModel).where(MemoryToolReceiptModel.initiative_run_id == run_id)
        )
        receipt_id = receipt.id
    assert await PeopleRepository(database).delete_person_id(erased.author_person_id)
    async with database.sessions() as reader:
        assert await reader.get(InitiativeRunModel, run_id) is None
        assert await reader.get(InitiativeFeedbackModel, (run_id, 1)) is None
        assert await reader.get(MemoryToolReceiptModel, receipt_id) is None
        assert (await reader.get(ChatEventModel, retained.id)).content == retained.content


async def test_reflection_keeps_each_original_person_stream_after_merge(database):
    from yuki.domain.conversations import ConversationScope
    from yuki.memory.self_reflection.repository import SelfReflectionRepository
    from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    ledger = ScopedEventLedgerUnitOfWork(database)
    sources = {}
    people = {}
    for index in range(2):
        for account in ("1001", "1002"):
            event = (
                await ledger.append(
                    scope=ConversationScope.private("8000", account),
                    platform_message_id=f"raw-reflection-{account}-{index}",
                    sender_user_id=account,
                    direction="inbound",
                    content=f"人物{account}的真实经历{index}",
                )
            ).event
            sources.setdefault(event.author_person_id, set()).add(event.id)
            people[account] = event.author_person_id
        assert await repository.scan_new_events() == 2
        if index == 0:
            await merge_people(database, people["1001"], people["1002"])
    observed = {}
    for index in range(2):
        (batch,) = await repository.claim_due(
            scheduled_slot=f"after-real-merge-{index}",
            event_threshold=1,
            character_threshold=1,
            max_wait_seconds=0,
            max_events=10,
            max_characters=16000,
            force=True,
        )
        observed[batch.state.canonical_person_id] = {event.id for event in batch.events}
        await repository.complete(batch, proposals=0, committed=0)
    assert observed == sources
    assert (
        await repository.claim_due(
            scheduled_slot="next-slot",
            event_threshold=1,
            character_threshold=1,
            max_wait_seconds=0,
            max_events=10,
            max_characters=16000,
            force=True,
        )
        == ()
    )
