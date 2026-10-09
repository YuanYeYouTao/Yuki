"""Unified Memory V2 mutation service behavior tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from tests.conftest import make_settings

from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.models import AdminActor
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.identity.canonical_repository import active_space_id_for, ensure_person, ensure_space
from qq_ai_bot.identity.db_models import CanonicalSpaceModel
from qq_ai_bot.memory.audit import MemoryAuditService
from qq_ai_bot.memory.claim_processor import MemoryClaimProcessor, MemoryProcessingContext
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryClaimOperation,
    MemoryConflictState,
    MemoryEvidenceRelation,
    MemoryKind,
    MemoryProcessingSource,
    MemoryScopeType,
    MemorySourceType,
    MemoryStatus,
    SelfMemoryVisibility,
)
from qq_ai_bot.memory.extraction import MemoryClaim
from qq_ai_bot.memory.models import (
    MemoryEvidenceCreate,
    MemoryFactCreate,
    MemoryFactQuery,
)
from qq_ai_bot.memory.mutation.models import (
    MemoryDecisionActorType,
    MemoryMutationAppliedOperation,
    MemoryMutationContext,
    MemoryMutationOperation,
    MemoryMutationOutcome,
    MemoryMutationRequest,
    MemoryMutationSelector,
    MemoryMutationTarget,
    SelfMemoryVisibilityMode,
)
from qq_ai_bot.memory.mutation.service import MemoryMutationService
from qq_ai_bot.memory.quality.audit import MemoryProductionQualityAudit
from qq_ai_bot.memory.quality.hygiene import MemoryProvenanceHygiene
from qq_ai_bot.memory.repository import MemoryFactRepository, MemoryJobRepository
from qq_ai_bot.memory.self_reflection.repository import SelfReflectionRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.subjects import ResolvedSubject
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    MemoryFactModel,
    MemoryMutationReceiptModel,
    MemoryToolReceiptModel,
)
from qq_ai_bot.persistence.repositories import (
    AgentActionRepository,
    EventLedgerRepository,
    PeopleRepository,
)
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.services.admin.memory_admin import MemoryAdminService
from qq_ai_bot.services.agent_tools import AgentToolService, ToolRuntime


def _service(
    database: Database,
    *,
    self_memory_enabled: bool = False,
) -> tuple[
    MemoryMutationService,
    MemoryFactService,
    EventLedgerRepository,
    MemoryClaimProcessor,
]:
    settings = make_settings(
        "sqlite+aiosqlite:///:memory:",
        self_memory_enabled=self_memory_enabled,
    )
    repository = MemoryFactRepository(database)
    facts = MemoryFactService(repository)
    ledger = EventLedgerRepository(database)
    processor = MemoryClaimProcessor(
        settings=settings,
        facts=facts,
    )
    return (
        MemoryMutationService(
            settings=settings,
            facts=facts,
            processor=processor,
            ledger=ledger,
        ),
        facts,
        ledger,
        processor,
    )


@pytest.mark.asyncio
async def test_agent_can_create_current_private_yuki_self_memory(database: Database) -> None:
    service, facts, ledger, _processor = _service(database, self_memory_enabled=True)
    event = await _event(
        ledger,
        message_id="yuki-self-private",
        sender_user_id="1001",
        content="我觉得你回答复杂问题时更喜欢先想清楚再说",
    )
    result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="self",
                scope_type=MemoryScopeType.SELF,
            ),
            visibility=SelfMemoryVisibilityMode.CURRENT_SCOPE,
            new_content="面对复杂问题时，我偏好先想清楚再回答",
            memory_key="preference:deliberate_answers",
            category="self_preference",
            kind=MemoryKind.PREFERENCE,
            reason="Yuki 接受了当前用户反馈并形成自我判断",
            confidence=0.82,
        ),
        _context(event),
    )

    assert result.ok and result.new_fact_id is not None
    fact = await facts.get_fact(result.new_fact_id)
    assert fact is not None
    assert fact.scope_type is MemoryScopeType.SELF
    assert fact.subject_user_id is None and fact.group_id is None
    assert fact.visibility_type is SelfMemoryVisibility.PRIVATE
    assert fact.visibility_user_id == "1001"
    assert fact.visibility_group_id is None
    assert fact.authority is MemoryAuthority.AGENT_REFLECTION
    evidence = await facts.list_evidence(fact.id, limit=10)
    assert len(evidence) == 1
    assert evidence[0].relation is MemoryEvidenceRelation.AGENT_REFLECTION
    assert evidence[0].event_id == event.id


@pytest.mark.asyncio
async def test_agent_can_correct_its_visible_self_memory(database: Database) -> None:
    service, facts, ledger, _processor = _service(database, self_memory_enabled=True)
    first_event = await _event(
        ledger,
        message_id="yuki-self-before-correction",
        sender_user_id="1001",
        content="你似乎偏好回答得快一些",
    )
    created = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(subject_ref="self", scope_type=MemoryScopeType.SELF),
            new_content="我偏好尽快回答",
            memory_key="preference:answer_style",
            category="self_preference",
            kind=MemoryKind.PREFERENCE,
            reason="形成初始自我判断",
        ),
        _context(first_event),
    )
    assert created.new_fact_id is not None

    correction_event = await _event(
        ledger,
        message_id="yuki-self-correction",
        sender_user_id="1001",
        content="准确说，你更在意回答准确，而不是单纯追求速度",
    )
    corrected = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CORRECT,
            fact_id=created.new_fact_id,
            target=MemoryMutationTarget(subject_ref="self", scope_type=MemoryScopeType.SELF),
            new_content="我更在意回答准确，而不是单纯追求速度",
            memory_key="preference:answer_style",
            category="self_preference",
            kind=MemoryKind.PREFERENCE,
            reason="Yuki 接受反馈并纠正自己的判断",
        ),
        _context(correction_event),
    )
    assert corrected.ok and corrected.new_fact_id is not None
    assert corrected.applied_operation is MemoryMutationAppliedOperation.CORRECT
    old = await facts.get_fact(created.new_fact_id)
    new = await facts.get_fact(corrected.new_fact_id)
    assert old is not None and old.status is MemoryStatus.SUPERSEDED
    assert new is not None and new.status is MemoryStatus.ACTIVE
    assert new.content == "我更在意回答准确，而不是单纯追求速度"
    assert new.visibility_user_id == "1001"

    # Historical lineage gaps and asynchronous expiry remain visible diagnostics.
    async with database.immediate_session() as session:
        await session.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id == new.id)
            .values(supersedes_id=None, valid_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    health = await MemoryAuditService(facts.repository).health()
    assert health.superseded_without_chain_count == 1
    assert health.expired_active_count == 1
    assert health.healthy
    assert not await facts.repository.list_facts(
        MemoryFactQuery(
            scope_type=MemoryScopeType.SELF,
            visibility_type=SelfMemoryVisibility.PRIVATE,
            visibility_user_id="1001",
        )
    )


async def _event(
    ledger: EventLedgerRepository,
    *,
    message_id: str,
    sender_user_id: str,
    content: str,
    group_id: str | None = None,
    mentioned_user_ids: tuple[str, ...] = (),
    direction: str = "inbound",
    sender_is_bot: bool = False,
    bot_user_id: str = "8000",
) -> EventRecord:
    segments = (
        {
            "type": "yuki_context",
            "data": {
                "mentioned_user_ids": list(mentioned_user_ids),
                "reply_sender_user_id": None,
            },
        },
    )
    event, _ = await ledger.append(
        bot_user_id=bot_user_id,
        platform_message_id=message_id,
        scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
        sender_user_id=sender_user_id,
        direction=direction,
        content=content,
        segments=segments,
        group_id=group_id,
        private_peer_user_id=sender_user_id if group_id is None else None,
        sender_is_bot=sender_is_bot,
    )
    return event


def _context(event: EventRecord) -> MemoryMutationContext:
    return MemoryMutationContext(
        event=event,
        conversation_key=(
            f"group:{event.group_id}:user:{event.sender_user_id}"
            if event.group_id
            else f"private:{event.sender_user_id}"
        ),
        turn_origin="user_message",
        delegation_mode="main_agent",
        trigger_actor_user_id=event.sender_user_id,
        decision_actor_type=MemoryDecisionActorType.AGENT,
        decision_actor_id="yuki-main-agent",
        executed_by_bot_user_id=event.bot_user_id,
    )


@pytest.mark.asyncio
async def test_mutation_selector_cannot_cross_resolved_identity_target(database: Database) -> None:
    service, facts, ledger, _processor = _service(database)
    other = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="2002",
            kind=MemoryKind.FACT,
            memory_key="private:cat",
            category="profile",
            content="养了一只猫",
            source_type=MemorySourceType.EXPLICIT,
        )
    )
    event = await _event(
        ledger,
        message_id="selector-isolation",
        sender_user_id="1001",
        content="撤回养猫记录",
    )

    result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.INVALIDATE,
            selector=MemoryMutationSelector(memory_key="private:cat"),
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
        ),
        _context(event),
    )

    assert not result.ok
    assert result.reason_code == "memory_candidate_not_found"
    assert not result.candidates
    assert (await facts.get_fact(other.id)).status is MemoryStatus.ACTIVE  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_self_reflection_can_commit_tool_receipt_evidence(database: Database) -> None:
    service, facts, ledger, _processor = _service(database, self_memory_enabled=True)
    event = await _event(
        ledger,
        message_id="reflection-tool-trigger",
        sender_user_id="1001",
        content="请检查刚才的真实工具结果",
        group_id="3001",
    )
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        canonical_space_id = await active_space_id_for(session, "3001")
        assert canonical_space_id is not None
        receipt = MemoryToolReceiptModel(
            canonical_space_id=canonical_space_id,
            conversation_key_hash=hashlib.sha256(b"group:3001").hexdigest(),
            trigger_event_id=event.id,
            bot_user_id="8000",
            provider_id="test",
            tool_name="doctor",
            success=True,
            result_excerpt="修复后检查成功",
            result_characters=7,
            created_at=now,
            expires_at=now + timedelta(days=7),
        )
        session.add(receipt)
        await session.flush()
        receipt_id = receipt.id

    result = await service.mutate_resolved(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="self",
                scope_type=MemoryScopeType.SELF,
            ),
            visibility=SelfMemoryVisibilityMode.CURRENT_SCOPE,
            new_content="Yuki 会在修复后用真实工具结果复查",
            memory_key="principle:verify_after_fix",
            category="self_principle",
            kind=MemoryKind.PREFERENCE,
            reason="self_reflection_verified_tool_result",
            importance=3,
            evidence_quote="修复后检查成功",
        ),
        MemoryMutationContext(
            event=event,
            conversation_key="group:3001:self-reflection",
            turn_origin="memory_self_reflection",
            delegation_mode="self_reflection",
            trigger_actor_user_id="1001",
            decision_actor_type=MemoryDecisionActorType.REFLECTION,
            decision_actor_id="yuki_self_reflection",
            executed_by_bot_user_id="8000",
            evidence_tool_receipt_id=receipt_id,
        ),
        target=ResolvedSubject(
            MemoryScopeType.SELF,
            None,
            None,
            SelfMemoryVisibility.GROUP,
            None,
            "3001",
        ),
    )

    assert result.ok and result.new_fact_id is not None
    fact = await facts.get_fact(result.new_fact_id)
    evidence = await facts.list_evidence(result.new_fact_id)
    assert fact is not None and fact.authority is MemoryAuthority.AGENT_REFLECTION
    assert evidence[0].event_id is None
    assert evidence[0].tool_receipt_id == receipt_id

    async with database.sessions() as session, session.begin():
        linked = await session.get(MemoryToolReceiptModel, receipt_id)
        assert linked is not None
        linked.expires_at = now - timedelta(seconds=1)
        session.add(
            MemoryToolReceiptModel(
                canonical_space_id=canonical_space_id,
                conversation_key_hash=hashlib.sha256(b"group:3001").hexdigest(),
                trigger_event_id=event.id,
                bot_user_id="8000",
                provider_id="test",
                tool_name="unused",
                success=True,
                result_excerpt="未被正式记忆引用",
                result_characters=9,
                created_at=now - timedelta(days=8),
                expires_at=now - timedelta(seconds=1),
            )
        )

    assert await SelfReflectionRepository(database).cleanup_receipts() == 1
    async with database.sessions() as session:
        assert await session.get(MemoryToolReceiptModel, receipt_id) is not None
    assert (await facts.list_evidence(result.new_fact_id))[0].tool_receipt_id == receipt_id

    # Expiration excludes new reflection input, not retained committed evidence.
    audited = await MemoryProductionQualityAudit(database).run()
    assert (
        next(
            item.count
            for item in audited.issues
            if item.issue_code == "evidence_source_event_missing"
        )
        == 0
    )
    assert (
        next(
            item.count
            for item in audited.issues
            if item.issue_code == "evidence_tool_source_invalid"
        )
        == 0
    )
    assert (
        result.new_fact_id not in (await MemoryProvenanceHygiene(database).scan()).invalid_fact_ids
    )

    # A receipt ID alone is insufficient: its stored result must support the excerpt.
    async with database.immediate_session() as session:
        await session.execute(
            update(MemoryToolReceiptModel)
            .where(MemoryToolReceiptModel.id == receipt_id)
            .values(result_excerpt="unrelated synthetic result")
        )
    audited = await MemoryProductionQualityAudit(database).run()
    assert (
        next(
            item.count
            for item in audited.issues
            if item.issue_code == "evidence_tool_source_invalid"
        )
        == 1
    )
    assert result.new_fact_id in (await MemoryProvenanceHygiene(database).scan()).invalid_fact_ids

    async with database.immediate_session() as session:
        await session.execute(
            update(MemoryToolReceiptModel)
            .where(MemoryToolReceiptModel.id == receipt_id)
            .values(result_excerpt="修复后检查成功")
        )
    # Versioning must preserve a tool source, not reconstruct an empty event source.
    replacement = await facts.version_fact(
        result.new_fact_id,
        replacement=MemoryFactCreate(
            scope_type=MemoryScopeType.SELF,
            visibility_type=SelfMemoryVisibility.GROUP,
            visibility_group_id="3001",
            kind=MemoryKind.PREFERENCE,
            memory_key=fact.memory_key,
            category="self_principle",
            content="Yuki 会用实际检查回执确认修复结果",
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.AGENT_REFLECTION,
        ),
        evidence=MemoryEvidenceCreate(
            tool_receipt_id=receipt_id,
            source_speaker_user_id="8000",
            relation=MemoryEvidenceRelation.AGENT_REFLECTION,
            authority=MemoryAuthority.AGENT_REFLECTION,
            excerpt="修复后检查成功",
        ),
        actor_user_id="8000",
        reason_code="reflection_version_test",
        copy_existing_evidence=True,
        confirmed_at=now,
    )
    assert replacement is not None and replacement.supersedes_id == result.new_fact_id
    retained = await facts.list_evidence(replacement.id)
    assert len(retained) == 1
    assert retained[0].tool_receipt_id == receipt_id and retained[0].event_id is None
    old = await facts.get_fact(result.new_fact_id)
    assert old is not None and old.status is MemoryStatus.SUPERSEDED


@pytest.mark.asyncio
async def test_self_reflection_evidence_still_rejects_another_canonical_conversation(
    database: Database,
) -> None:
    service, _facts, ledger, _processor = _service(database, self_memory_enabled=True)
    evidence_event = await _event(
        ledger,
        message_id="reflection-other-conversation",
        sender_user_id="1001",
        content="这是另一个群里的内容。",
        group_id="3002",
        bot_user_id="8000",
    )
    anchor = await _event(
        ledger,
        message_id="reflection-current-conversation",
        sender_user_id="8001",
        content="这是当前群里的回应。",
        group_id="3001",
        direction="outbound",
        sender_is_bot=True,
        bot_user_id="8001",
    )

    result = await service.mutate_resolved(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(subject_ref="self", scope_type=MemoryScopeType.SELF),
            visibility=SelfMemoryVisibilityMode.CURRENT_SCOPE,
            new_content="我不会把另一个会话的证据混入当前经历。",
            memory_key="self_episode:cross-conversation-guard",
            category="self_episode",
            kind=MemoryKind.EPISODE,
            reason="self_reflection_episode",
            evidence_quote=anchor.content,
        ),
        MemoryMutationContext(
            event=anchor,
            conversation_key="group:3001:self-reflection",
            turn_origin="memory_self_reflection",
            delegation_mode=f"self_episode:{evidence_event.id}:{anchor.id}",
            trigger_actor_user_id=anchor.sender_user_id,
            decision_actor_type=MemoryDecisionActorType.REFLECTION,
            decision_actor_id="yuki_self_reflection",
            executed_by_bot_user_id=anchor.bot_user_id,
        ),
        target=ResolvedSubject(
            MemoryScopeType.SELF,
            None,
            None,
            SelfMemoryVisibility.GROUP,
            None,
            "3001",
        ),
        additional_evidence=(
            MemoryEvidenceCreate(
                event_id=evidence_event.id,
                source_speaker_user_id=evidence_event.sender_user_id,
                relation=MemoryEvidenceRelation.AGENT_REFLECTION,
                confidence=0.9,
                authority=MemoryAuthority.AGENT_REFLECTION,
                excerpt=evidence_event.content,
            ),
        ),
    )

    assert not result.ok
    assert result.reason_code == "cross_conversation_evidence"


@pytest.mark.asyncio
async def test_self_create_is_atomic_receipted_and_deduplicated(database: Database) -> None:
    service, facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id="self-create",
        sender_user_id="1001",
        content="记住我现在住在上海",
    )
    request = MemoryMutationRequest(
        operation=MemoryMutationOperation.CREATE,
        target=MemoryMutationTarget(
            subject_ref="current_speaker",
            scope_type=MemoryScopeType.PERSON,
        ),
        new_content="现在住在上海",
        memory_key="location:home",
        category="location",
        reason="用户明确要求记住当前住址",
        confidence=0.96,
    )

    first = await service.mutate(request, _context(event))
    second = await service.mutate(request, _context(event))

    assert first.ok
    assert first.applied_operation is MemoryMutationAppliedOperation.CREATE
    assert first.outcome is MemoryMutationOutcome.COMMITTED
    assert second.ok
    assert second.deduplicated
    assert second.mutation_id == first.mutation_id
    rows = await facts.list_person("1001", limit=20)
    assert len(rows) == 1
    assert rows[0].content == "现在住在上海"
    assert rows[0].authority is MemoryAuthority.EXPLICIT
    async with database.sessions() as session:
        receipt_count = int(
            await session.scalar(select(func.count()).select_from(MemoryMutationReceiptModel)) or 0
        )
    assert receipt_count == 1

    # Fact and evidence preserve their independent authority values.
    reported = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            memory_key="location:reported",
            category="location",
            content="现在住在上海",
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.SELF_REPORT,
        ),
        evidence=MemoryEvidenceCreate(
            event_id=event.id,
            source_speaker_user_id="1001",
            relation=MemoryEvidenceRelation.EXPLICIT_COMMAND,
            authority=MemoryAuthority.EXPLICIT,
            excerpt=event.content,
        ),
    )
    assert reported.authority is MemoryAuthority.SELF_REPORT
    assert (await facts.list_evidence(reported.id))[0].authority is MemoryAuthority.EXPLICIT
    group_event = await _event(
        ledger,
        message_id="group-correction",
        sender_user_id="1001",
        group_id="3001",
        content="本群现在周五晚讨论项目",
    )
    claim = MemoryClaim(
        operation=MemoryClaimOperation.CORRECT,
        subject_ref="group",
        scope_type=MemoryScopeType.GROUP,
        memory_key="schedule",
        category="group_schedule",
        content=group_event.content,
        evidence_quote=group_event.content,
    )
    corrected = await _processor.process(
        claim, MemoryProcessingContext(source=MemoryProcessingSource.LIVE, event=group_event)
    )
    group_fact = await facts.get_fact(corrected.fact_id)
    group_evidence = (await facts.list_evidence(group_fact.id))[0]
    assert group_fact.scope_type is MemoryScopeType.GROUP and group_fact.group_id == "3001"
    assert group_evidence.relation is MemoryEvidenceRelation.CORRECTION
    assert group_evidence.authority is MemoryAuthority.GROUP_REPORT
    assert (
        group_evidence.event_id == group_event.id and group_evidence.excerpt == group_event.content
    )
    unrelated_quote = claim.model_copy(update={"evidence_quote": "不存在的引用"})
    assert (
        _processor.validate_result(unrelated_quote, group_event).reason_code
        == "evidence_quote_not_in_event"
    )
    assert (await MemoryProductionQualityAudit(database).run()).error_count == 0


@pytest.mark.asyncio
async def test_bot_event_cannot_become_user_memory_evidence(database: Database) -> None:
    service, _facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id="bot-event",
        sender_user_id="8000",
        content="记住用户住在上海",
        group_id="3001",
        direction="outbound",
        sender_is_bot=True,
    )
    request = MemoryMutationRequest(
        operation=MemoryMutationOperation.CREATE,
        target=MemoryMutationTarget(
            subject_ref="current_speaker",
            scope_type=MemoryScopeType.PERSON,
        ),
        new_content="住在上海",
        memory_key="location:home",
        category="location",
        reason="不可信的 Bot 消息",
    )

    result = await service.mutate(request, _context(event))

    assert not result.ok
    assert result.reason_code == "untrusted_trigger_event"
    async with database.sessions() as session:
        assert (
            int(
                await session.scalar(select(func.count()).select_from(MemoryMutationReceiptModel))
                or 0
            )
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("has_event_id", [True, False])
async def test_agent_tool_and_worker_share_one_claim_receipt(
    database: Database, has_event_id: bool
) -> None:
    service, facts, ledger, processor = _service(database)
    event = await _event(
        ledger,
        message_id="agent-worker-dedupe",
        sender_user_id="1001",
        content="记住我现在住在上海",
    )
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    tools = AgentToolService(
        settings=settings,
        ledger=ledger,
        memories=facts,
        memory_mutations=service,
        actions=AgentActionRepository(database),
    )
    inbound = InboundMessage(
        message_id=event.platform_message_id,
        event_type="message",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=event.sender_user_id),
        text=event.content,
        bot_user_id=event.bot_user_id,
        person_id=event.author_person_id,
        conversation_id=event.canonical_conversation_id,
        presence_id=event.ingress_presence_id,
    )
    runtime = ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
        conversation_key="private:1001",
        trigger_message_id=event.platform_message_id,
        trigger_event_id=event.id if has_event_id else None,
        origin=TurnOrigin.USER_MESSAGE,
    )
    assert "memory_change" in {tool.name for tool in tools.definitions(runtime)}
    response = (
        await tools.execute(
            "memory_change",
            json.dumps(
                {
                    "operation": "create",
                    "target": {
                        "subject_ref": "current_speaker",
                        "scope_type": "person",
                    },
                    "new_content": "现在住在上海",
                    "memory_key": "location:home",
                    "category": "location",
                    "reason": "当前消息明确要求记忆",
                    "confidence": 0.96,
                },
                ensure_ascii=False,
            ),
            runtime,
        )
    ).model_payload()
    if not has_event_id:
        assert response["error_code"] == "trigger_event_not_found"
        async with database.sessions() as session:
            assert (
                await session.scalar(select(func.count()).select_from(MemoryMutationReceiptModel))
                == 0
            )
        return
    assert response["ok"]
    assert response["data"]["outcome"] == "committed"

    claim = MemoryClaim(
        operation=MemoryClaimOperation.ASSERT,
        subject_ref="speaker",
        scope_type=MemoryScopeType.PERSON,
        kind=MemoryKind.FACT,
        memory_key="location:home",
        category="location",
        content="现在住在上海",
        evidence_quote=event.content,
        importance=3,
        confidence=0.96,
        source_type=MemorySourceType.EXPLICIT,
    )
    validated = processor.validate(claim, event)
    assert validated is not None
    jobs = MemoryJobRepository(database)
    assert await jobs.enqueue(event.id, "private:1001")
    (job,) = await jobs.claim()
    worker_result = await service.mutate_validated_claim(
        validated,
        MemoryProcessingContext(source=MemoryProcessingSource.LIVE, event=event),
        conversation_key="private:1001",
        job=job,
    )

    assert worker_result.ok
    assert worker_result.deduplicated
    assert worker_result.mutation_id == response["data"]["mutation_id"]
    assert len(await facts.list_person("1001", limit=20)) == 1


@pytest.mark.asyncio
async def test_self_correction_creates_a_new_version(database: Database) -> None:
    service, facts, ledger, _processor = _service(database)
    original_event = await _event(
        ledger,
        message_id="version-original",
        sender_user_id="1001",
        content="记住我住在北京",
    )
    original = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            new_content="住在北京",
            memory_key="location:home",
            category="location",
            reason="original_self_report",
        ),
        _context(original_event),
    )
    assert original.new_fact_id is not None
    correction_event = await _event(
        ledger,
        message_id="version-correction",
        sender_user_id="1001",
        content="我已经搬到上海了",
    )
    correction = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CORRECT,
            fact_id=original.new_fact_id,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            new_content="已经搬到上海",
            reason="current_self_correction",
            expected_fact_state=MemoryStatus.ACTIVE,
        ),
        _context(correction_event),
    )

    assert correction.ok
    assert correction.applied_operation is MemoryMutationAppliedOperation.CORRECT
    assert correction.new_fact_id not in {None, original.new_fact_id}
    old = await facts.get_fact(original.new_fact_id)
    new = await facts.get_fact(correction.new_fact_id)
    assert old is not None and old.status is MemoryStatus.SUPERSEDED
    assert new is not None and new.status is MemoryStatus.ACTIVE
    assert new.supersedes_id == old.id


@pytest.mark.parametrize(
    "content",
    (
        "江环是@鬼頭桃菜，你记错了",
        "@鬼頭桃菜是江环，这次请按这个主体纠正",
    ),
)
@pytest.mark.asyncio
async def test_mentioned_subject_is_not_rejected_by_chinese_word_order(
    database: Database,
    content: str,
) -> None:
    service, facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id=f"mentioned-word-order-{hashlib.sha256(content.encode()).hexdigest()[:8]}",
        sender_user_id="1001",
        content=content,
        group_id="3001",
        mentioned_user_ids=("2002",),
    )

    result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="mentioned_user",
                scope_type=MemoryScopeType.PERSON_GROUP,
            ),
            new_content="江环是鬼頭桃菜",
            memory_key="identity:jianghuan",
            category="identity",
            evidence_quote=content,
        ),
        _context(event),
    )

    assert result.ok and result.new_fact_id is not None
    fact = await facts.get_fact(result.new_fact_id)
    assert fact is not None
    assert fact.subject_user_id == "2002"
    assert fact.authority is MemoryAuthority.THIRD_PARTY


@pytest.mark.asyncio
async def test_reassign_is_one_atomic_versioned_group_operation(database: Database) -> None:
    service, facts, ledger, _processor = _service(database)
    original_event = await _event(
        ledger,
        message_id="reassign-original",
        sender_user_id="1001",
        content="我喜欢摄影",
        group_id="3001",
    )
    original = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON_GROUP,
            ),
            new_content="喜欢摄影",
            memory_key="hobby:photography",
            category="hobby",
            reason="initial_attribution",
        ),
        _context(original_event),
    )
    assert original.new_fact_id is not None
    event = await _event(
        ledger,
        message_id="reassign-correction",
        sender_user_id="1001",
        content="刚才那条其实说的是小明喜欢摄影",
        group_id="3001",
        mentioned_user_ids=("2002",),
    )
    result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.REASSIGN,
            fact_id=original.new_fact_id,
            target=MemoryMutationTarget(
                subject_ref="mentioned_user",
                scope_type=MemoryScopeType.PERSON_GROUP,
            ),
            reason="misattributed_subject",
        ),
        _context(event),
    )

    assert result.ok
    assert result.applied_operation is MemoryMutationAppliedOperation.REASSIGN
    assert result.new_fact_id is not None
    old = await facts.get_fact(original.new_fact_id)
    reassigned = await facts.get_fact(result.new_fact_id)
    assert old is not None and old.status is MemoryStatus.SUPERSEDED
    assert reassigned is not None and reassigned.status is MemoryStatus.ACTIVE
    assert reassigned.subject_user_id == "2002"
    assert reassigned.group_id == "3001"
    assert reassigned.authority is MemoryAuthority.THIRD_PARTY


@pytest.mark.asyncio
async def test_concurrent_duplicate_requests_commit_once(database: Database) -> None:
    service, facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id="concurrent-dedupe",
        sender_user_id="1001",
        content="记住我喜欢爵士乐",
    )
    request = MemoryMutationRequest(
        operation=MemoryMutationOperation.CREATE,
        target=MemoryMutationTarget(
            subject_ref="current_speaker",
            scope_type=MemoryScopeType.PERSON,
        ),
        new_content="喜欢爵士乐",
        memory_key="music:jazz",
        category="music",
        reason="concurrent_same_request",
    )

    first, second = await asyncio.gather(
        service.mutate(request, _context(event)),
        service.mutate(request, _context(event)),
    )

    assert {first.deduplicated, second.deduplicated} == {False, True}
    assert first.mutation_id == second.mutation_id
    assert len(await facts.list_person("1001", limit=20)) == 1


@pytest.mark.asyncio
async def test_receipt_failure_rolls_back_fact(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id="rollback-receipt",
        sender_user_id="1001",
        content="记住我喜欢蓝色",
    )

    async def fail_finalize(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("forced receipt failure")

    monkeypatch.setattr(service._receipts, "finalize", fail_finalize)
    with pytest.raises(RuntimeError, match="forced receipt failure"):
        await service.mutate(
            MemoryMutationRequest(
                operation=MemoryMutationOperation.CREATE,
                target=MemoryMutationTarget(
                    subject_ref="current_speaker",
                    scope_type=MemoryScopeType.PERSON,
                ),
                new_content="喜欢蓝色",
                memory_key="color:favorite",
                category="preference",
                reason="rollback_test",
            ),
            _context(event),
        )

    assert await facts.list_person("1001", limit=20) == ()
    async with database.sessions() as session:
        assert (
            int(
                await session.scalar(select(func.count()).select_from(MemoryMutationReceiptModel))
                or 0
            )
            == 0
        )


@pytest.mark.asyncio
async def test_embedding_schedule_failure_keeps_committed_fact(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id="embedding-failure",
        sender_user_id="1001",
        content="记住我喜欢绿色",
    )

    async def fail_embedding(_fact_id: int) -> None:
        raise RuntimeError("embedding unavailable")

    monkeypatch.setattr(facts, "schedule_embedding", fail_embedding)
    result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            new_content="喜欢绿色",
            memory_key="color:favorite",
            category="preference",
            reason="embedding_failure_test",
        ),
        _context(event),
    )

    assert result.ok and result.new_fact_id is not None
    assert await facts.get_fact(result.new_fact_id) is not None


@pytest.mark.asyncio
async def test_merge_metadata_contest_invalidate_and_restore_operations(
    database: Database,
) -> None:
    service, facts, ledger, _processor = _service(database)

    async def create(message_id: str, text: str, key: str, content: str) -> int:
        event = await _event(
            ledger,
            message_id=message_id,
            sender_user_id="1001",
            content=text,
        )
        result = await service.mutate(
            MemoryMutationRequest(
                operation=MemoryMutationOperation.CREATE,
                target=MemoryMutationTarget(
                    subject_ref="current_speaker",
                    scope_type=MemoryScopeType.PERSON,
                ),
                new_content=content,
                memory_key=key,
                category="music",
                reason="operation_fixture",
            ),
            _context(event),
        )
        assert result.new_fact_id is not None
        return result.new_fact_id

    source_id = await create(
        "merge-source",
        "我喜欢 Jazz",
        "music:jazz",
        "喜欢 Jazz",
    )
    target_id = await create(
        "merge-target",
        "我喜欢爵士乐",
        "music:favorite",
        "喜欢爵士乐",
    )
    merge_event = await _event(
        ledger,
        message_id="merge-operation",
        sender_user_id="1001",
        content="这两条其实是同一个音乐偏好",
    )
    merged = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.MERGE,
            fact_id=source_id,
            merge_fact_id=target_id,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            reason="equivalent_music_preferences",
        ),
        _context(merge_event),
    )
    assert merged.ok
    assert merged.applied_operation is MemoryMutationAppliedOperation.MERGE
    assert (await facts.get_fact(source_id)).status is MemoryStatus.SUPERSEDED  # type: ignore[union-attr]

    metadata_event = await _event(
        ledger,
        message_id="metadata-operation",
        sender_user_id="1001",
        content="把它归类为音乐偏好，重要度四级",
    )
    metadata = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.UPDATE_METADATA,
            fact_id=target_id,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            category="preference",
            kind=MemoryKind.PREFERENCE,
            importance=4,
            reason="metadata_reclassification",
        ),
        _context(metadata_event),
    )
    assert metadata.ok and metadata.new_fact_id is not None
    current_id = metadata.new_fact_id
    current = await facts.get_fact(current_id)
    assert current is not None
    assert current.kind is MemoryKind.PREFERENCE
    assert current.category == "preference"
    assert current.supersedes_id == target_id

    contest_event = await _event(
        ledger,
        message_id="contest-operation",
        sender_user_id="1001",
        content="这条记忆需要先标为有争议",
    )
    contested = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CONTEST,
            fact_id=current_id,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            reason="user_requested_review",
        ),
        _context(contest_event),
    )
    assert contested.ok
    assert contested.applied_operation is MemoryMutationAppliedOperation.CONTEST
    assert (await facts.get_fact(current_id)).conflict_state is (  # type: ignore[union-attr]
        MemoryConflictState.CONTESTED
    )

    invalidate_event = await _event(
        ledger,
        message_id="invalidate-operation",
        sender_user_id="1001",
        content="撤销这条音乐偏好记忆",
    )
    invalidated = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.INVALIDATE,
            fact_id=current_id,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            reason="user_retracted",
        ),
        _context(invalidate_event),
    )
    assert invalidated.ok
    assert (await facts.get_fact(current_id)).status is MemoryStatus.INVALIDATED  # type: ignore[union-attr]

    restore_event = await _event(
        ledger,
        message_id="restore-operation",
        sender_user_id="1001",
        content="恢复这条音乐偏好记忆",
    )
    restored = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.RESTORE,
            fact_id=current_id,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            reason="user_requested_restore",
        ),
        _context(restore_event),
    )
    assert restored.ok
    assert restored.applied_operation is MemoryMutationAppliedOperation.RESTORE
    assert (await facts.get_fact(current_id)).status is MemoryStatus.ACTIVE  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_historical_social_read_policy_is_consistent_without_evidence_expansion(
    database: Database,
) -> None:
    service, facts, ledger, _processor = _service(database)
    del service
    people = PeopleRepository(database)
    await people.observe(user_id="1001", nickname="请求者", group_id="3001")
    await people.observe(
        user_id="2002",
        nickname="Diana",
        group_id="3001",
        group_card="Diana",
    )
    current_group_event = await _event(
        ledger,
        message_id="member-in-group",
        sender_user_id="2002",
        content="我喜欢天文",
        group_id="3001",
    )
    private_source = await _event(
        ledger,
        message_id="member-private-fact",
        sender_user_id="2002",
        content="跨群私人事实",
    )
    global_fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="2002",
            kind=MemoryKind.FACT,
            memory_key="private:secret",
            category="private",
            content="跨群私人事实",
            importance=5,
            confidence=1,
            source_type=MemorySourceType.EXPLICIT,
            authority=MemoryAuthority.EXPLICIT,
        ),
        evidence=MemoryEvidenceCreate(
            event_id=private_source.id,
            source_speaker_user_id="2002",
            relation=MemoryEvidenceRelation.SELF_STATEMENT,
            confidence=1,
            authority=MemoryAuthority.SELF_REPORT,
            excerpt="跨群私人事实",
        ),
    )
    group_fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON_GROUP,
            subject_user_id="2002",
            group_id="3001",
            kind=MemoryKind.FACT,
            memory_key="role:photographer",
            category="role",
            content="在本群负责摄影",
            importance=3,
            confidence=0.8,
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.THIRD_PARTY,
        )
    )
    projected_fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="2002",
            kind=MemoryKind.FACT,
            memory_key="hobby:astronomy",
            category="hobby",
            content="喜欢天文",
            importance=4,
            confidence=0.9,
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.SELF_REPORT,
        ),
        evidence=MemoryEvidenceCreate(
            event_id=current_group_event.id,
            source_speaker_user_id="2002",
            relation=MemoryEvidenceRelation.SELF_STATEMENT,
            confidence=0.9,
            authority=MemoryAuthority.SELF_REPORT,
            excerpt="我喜欢天文",
        ),
    )
    other_group_event = await _event(
        ledger,
        message_id="member-other-group",
        sender_user_id="2002",
        content="我喜欢围棋",
        group_id="3002",
    )
    await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="2002",
            kind=MemoryKind.FACT,
            memory_key="hobby:go",
            category="hobby",
            content="喜欢围棋",
            importance=4,
            confidence=0.9,
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.SELF_REPORT,
        ),
        evidence=MemoryEvidenceCreate(
            event_id=other_group_event.id,
            source_speaker_user_id="2002",
            relation=MemoryEvidenceRelation.SELF_STATEMENT,
            confidence=0.9,
            authority=MemoryAuthority.SELF_REPORT,
            excerpt="我喜欢围棋",
        ),
    )
    # An old shared group outside the current scene still participates, while
    # an unrelated person cannot enter the no-target search.
    async with database.sessions() as session, session.begin():
        await ensure_space(session, "3003")
        await ensure_space(session, "3004")
        await ensure_person(session, "4004")
        await ensure_person(session, "5005")
    await people.observe(user_id="1001", nickname="请求者", group_id="3003")
    await people.observe(user_id="4004", nickname="旧群友", group_id="3003")
    await people.observe(user_id="5005", nickname="陌生人", group_id="3004")
    historical_fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="4004",
            memory_key="hobby:specimens",
            category="hobby",
            content="喜欢昆虫标本",
            source_type=MemorySourceType.AUTOMATIC,
        )
    )
    late_high_rank_fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="4004",
            memory_key="hobby:astronomy",
            category="hobby",
            content="旧群友喜欢天文",
            importance=5,
            source_type=MemorySourceType.AUTOMATIC,
        )
    )
    inaccessible_fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="5005",
            memory_key="hobby:specimens",
            category="hobby",
            content="喜欢昆虫标本",
            source_type=MemorySourceType.AUTOMATIC,
        )
    )
    group_only_fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.GROUP,
            group_id="3001",
            memory_key="event:hike",
            category="event",
            content="群活动远足",
            source_type=MemorySourceType.AUTOMATIC,
        )
    )
    inbound = InboundMessage(
        message_id="member-read",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001"),
        text="小明在这个群负责什么",
        bot_user_id="8000",
        group_id="3001",
        mentioned_user_ids=("2002",),
    )
    tools = AgentToolService(
        settings=make_settings("sqlite+aiosqlite:///:memory:"),
        ledger=ledger,
        memories=facts,
        actions=AgentActionRepository(database),
    )
    runtime = ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
    )
    definitions = tools.definitions(runtime)
    assert not {"get_person_memories", "get_group_memories", "get_self_memories"} & {
        tool.name for tool in definitions
    }
    definition = next(tool for tool in definitions if tool.name == "search_memory")
    properties = definition.parameters["properties"]
    assert definition.parameters["required"] == ["query"]
    assert set(properties) >= {"query", "target", "purpose", "entities"}  # type: ignore[arg-type]

    global_search = (
        await tools.execute("search_memory", json.dumps({"query": "喜欢天文"}), runtime)
    ).model_payload()
    assert global_search["ok"], global_search
    assert global_search["data"]["result_scope"] == "authorized_maximum"
    assert global_search["data"]["partial_reason"] == "semantic_not_configured"
    explicit_missing = (
        await tools.execute(
            "search_memory",
            json.dumps(
                {
                    "query": "zzzznonexistentmemoryzzzz",
                    "target": {"scope": "person", "subject_ref": "current_speaker"},
                }
            ),
            runtime,
        )
    ).model_payload()
    assert explicit_missing["ok"]
    assert explicit_missing["data"]["result_scope"] == "explicit_targets"
    assert explicit_missing["data"]["returned_count"] == 0
    assert explicit_missing["data"]["truncated"] is False
    assert explicit_missing["data"]["exhaustive"] is False
    assert explicit_missing["data"]["partial_reason"] == "semantic_not_configured"
    assert projected_fact.id in {row["fact_id"] for row in global_search["data"]["memories"]}
    top_one = (
        await tools.execute(
            "search_memory", json.dumps({"query": "旧群友喜欢天文", "limit": 1}), runtime
        )
    ).model_payload()
    assert [row["fact_id"] for row in top_one["data"]["memories"]] == [late_high_rank_fact.id]
    historical_search = (
        await tools.execute("search_memory", json.dumps({"query": "昆虫标本"}), runtime)
    ).model_payload()
    historical_ids = {row["fact_id"] for row in historical_search["data"]["memories"]}
    assert historical_fact.id in historical_ids
    assert inaccessible_fact.id not in historical_ids
    # A historical canonical owner remains searchable after its transport Binding ends.
    from qq_ai_bot.identity.db_models import IdentityBindingModel

    async with database.sessions() as session, session.begin():
        await session.execute(
            update(IdentityBindingModel)
            .where(IdentityBindingModel.external_account_id == "4004")
            .values(status="disabled")
        )
    unbound_search = (
        await tools.execute("search_memory", json.dumps({"query": "昆虫标本"}), runtime)
    ).model_payload()
    assert unbound_search["ok"], unbound_search
    assert historical_fact.id in {row["fact_id"] for row in unbound_search["data"]["memories"]}
    person_only = replace(
        runtime,
        origin=TurnOrigin.PLUGIN_SESSION,
        actor_context=None,
        memory_allowed_scopes=(MemoryScopeType.PERSON, MemoryScopeType.PERSON_GROUP),
    )
    group_only = replace(
        runtime,
        origin=TurnOrigin.PLUGIN_SESSION,
        actor_context=None,
        memory_allowed_scopes=(MemoryScopeType.GROUP,),
    )
    person_result = (
        await tools.execute("search_memory", json.dumps({"query": "喜欢天文"}), person_only)
    ).model_payload()
    assert projected_fact.id in {row["fact_id"] for row in person_result["data"]["memories"]}
    assert group_only_fact.id not in {row["fact_id"] for row in person_result["data"]["memories"]}
    group_result = (
        await tools.execute("search_memory", json.dumps({"query": "群活动远足"}), group_only)
    ).model_payload()
    assert group_only_fact.id in {row["fact_id"] for row in group_result["data"]["memories"]}
    assert (
        await tools.execute("get_memory_fact", json.dumps({"fact_id": group_fact.id}), group_only)
    ).model_payload()["error_code"] == "memory_not_found"
    assert (
        await tools.execute(
            "get_memory_fact", json.dumps({"fact_id": group_only_fact.id}), group_only
        )
    ).model_payload()["ok"]
    strict_person_only = replace(person_only, memory_allowed_scopes=(MemoryScopeType.PERSON,))
    strict_person_result = (
        await tools.execute(
            "search_memory", json.dumps({"query": "在本群负责摄影"}), strict_person_only
        )
    ).model_payload()
    assert group_fact.id not in {row["fact_id"] for row in strict_person_result["data"]["memories"]}
    self_actor = ToolActor(
        user_id="",
        bot_user_id="8000",
        group_id="3001",
        origin=TurnOrigin.SELF_INITIATIVE,
        instruction="检查群记忆",
        execution_id="self-memory-audit",
        conversation_id="self-memory-conversation",
        presence_id="self-memory-presence",
        principal_kind="self",
        initiative_run_id="self-memory-run",
    )
    self_runtime = replace(
        runtime,
        inbound=None,
        actor_context=self_actor,
        origin=TurnOrigin.SELF_INITIATIVE,
        execution_id=self_actor.execution_id,
        initiative_run_id=self_actor.initiative_run_id,
        conversation_id=self_actor.conversation_id,
        scope_type=ScopeType.GROUP,
    )
    self_search = (
        await tools.execute("search_memory", json.dumps({"query": "在本群负责摄影"}), self_runtime)
    ).model_payload()
    assert group_fact.id not in {row["fact_id"] for row in self_search["data"]["memories"]}
    assert (
        await tools.execute("get_memory_fact", json.dumps({"fact_id": group_fact.id}), self_runtime)
    ).model_payload()["error_code"] == "memory_not_found"
    assert (
        await tools.execute(
            "get_memory_fact", json.dumps({"fact_id": global_fact.id}), self_runtime
        )
    ).model_payload()["error_code"] == "memory_not_found"
    assert (
        await tools.execute(
            "get_memory_fact", json.dumps({"fact_id": group_only_fact.id}), self_runtime
        )
    ).model_payload()["ok"]
    assert (
        await tools.execute(
            "search_memory",
            json.dumps({"query": "喜欢天文", "target": {"scope": "person", "user_id": "2002"}}),
            group_only,
        )
    ).model_payload()["error_code"] == "permission_denied"
    scoped_search = (
        await tools.execute(
            "search_memory",
            json.dumps(
                {
                    "query": "喜欢天文",
                    "target": {"scope": "person", "subject_ref": "member_2002"},
                }
            ),
            runtime,
        )
    ).model_payload()
    assert scoped_search["ok"]
    assert scoped_search["data"]["result_scope"] == "explicit_targets"
    denied_search = (
        await tools.execute(
            "search_memory",
            json.dumps({"query": "喜欢围棋", "target": {"scope": "group", "group_id": "3002"}}),
            runtime,
        )
    ).model_payload()
    assert denied_search["error_code"] == "permission_denied"

    group_lookup = (
        await tools.execute(
            "get_memory_fact",
            json.dumps({"fact_id": group_fact.id}),
            runtime,
        )
    ).model_payload()
    global_lookup = (
        await tools.execute(
            "get_memory_fact",
            json.dumps({"fact_id": global_fact.id}),
            runtime,
        )
    ).model_payload()
    projected_lookup = (
        await tools.execute(
            "get_memory_fact",
            json.dumps({"fact_id": projected_fact.id}),
            runtime,
        )
    ).model_payload()
    assert group_lookup["ok"]
    assert global_lookup["ok"]
    assert projected_lookup["ok"]

    # The same direct historical relation works privately and from another group,
    # even after a Presence change. It never grants evidence or transitive access.
    private_runtime = replace(
        runtime,
        inbound=replace(inbound, scope_type=ScopeType.PRIVATE, group_id=None, bot_user_id="8001"),
        actor_context=None,
    )
    evidence = (
        await tools.execute(
            "get_memory_evidence", json.dumps({"fact_id": projected_fact.id}), private_runtime
        )
    ).model_payload()
    assert not evidence["ok"]
    background = (
        await tools.execute(
            "search_memory",
            json.dumps({"query": "天文", "target": {"scope": "person", "user_id": "2002"}}),
            replace(private_runtime, origin=TurnOrigin.PLUGIN_BACKGROUND, actor_context=None),
        )
    ).model_payload()
    # Origin labels do not revoke a retained real actor; the common query plane
    # still applies the same person and scope read authorization.
    assert background["ok"]
    await people.observe(user_id="2003", nickname="间接关系", group_id="3002")
    indirect = (
        await tools.execute(
            "search_memory",
            json.dumps({"query": "天文", "target": {"scope": "person", "user_id": "2003"}}),
            private_runtime,
        )
    ).model_payload()
    assert not indirect["ok"] and indirect["retryable"] is False
    unrelated_group = (
        await tools.execute(
            "search_memory",
            json.dumps({"query": "群活动", "target": {"scope": "group", "group_id": "3002"}}),
            private_runtime,
        )
    ).model_payload()
    assert not unrelated_group["ok"]
    historical_group = (
        await tools.execute(
            "search_memory",
            json.dumps({"query": "群活动", "target": {"scope": "group", "group_id": "3001"}}),
            private_runtime,
        )
    ).model_payload()
    assert historical_group["ok"]
    assert {row["fact_id"] for row in historical_group["data"]["memories"]} == {group_only_fact.id}

    async with database.sessions() as session, session.begin():
        old_space = await active_space_id_for(session, "3001")
        await session.execute(
            update(CanonicalSpaceModel)
            .where(CanonicalSpaceModel.id == old_space)
            .values(enabled=False)
        )
    complete_search = (
        await tools.execute("search_memory", json.dumps({"query": "昆虫标本"}), private_runtime)
    ).model_payload()
    assert complete_search["data"]["partial_reason"] == "semantic_not_configured"
    assert complete_search["data"]["partial_reason"] != "owner_projection_unavailable"
    cross_group = replace(runtime, inbound=replace(inbound, group_id="3002"), actor_context=None)
    assert (
        await tools.execute("get_memory_fact", json.dumps({"fact_id": group_fact.id}), cross_group)
    ).model_payload()["ok"]
    private_runtime = replace(
        private_runtime,
        runtime_config=await tools._runtime_config.snapshot(user_id="1001", group_id=None),
    )
    assert await people.delete_person("1001")
    forgotten = (
        await tools.execute(
            "get_memory_fact", json.dumps({"fact_id": global_fact.id}), private_runtime
        )
    ).model_payload()
    assert not forgotten["ok"]


@pytest.mark.asyncio
async def test_deterministic_memory_admin_uses_unified_mutation_receipt(
    database: Database,
) -> None:
    service, facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id="command-memory-add",
        sender_user_id="1001",
        content="/ai memory add 我喜欢天文",
    )
    admin = MemoryAdminService(
        settings=make_settings("sqlite+aiosqlite:///:memory:"),
        memories=facts,
        audit=AdminAuditService(database),
        mutations=service,
        ledger=ledger,
    )
    row = await admin.add_memory(
        AdminActor(
            user_id="1001",
            is_superuser=False,
            trigger_message_id=event.platform_message_id,
            trigger_event_id=event.id,
            conversation_key="private:1001",
            current_message_text=event.content,
            bot_user_id=event.bot_user_id,
            decision_actor_type="command",
        ),
        "1001",
        "我喜欢天文",
    )

    assert row.content == "我喜欢天文"
    async with database.sessions() as session:
        receipt = await session.scalar(select(MemoryMutationReceiptModel))
    assert receipt is not None
    assert receipt.trigger_event_id == event.id
    assert receipt.decision_actor_type == "command"
    assert receipt.new_fact_id == row.id
