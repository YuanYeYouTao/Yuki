"""Real human ingress, admission races and mailbox publication share one source."""

import asyncio
import base64
from dataclasses import replace

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy import event as sql_event
from tests.conftest import MemorySender, build_harness, make_settings

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.initiative_sources import source_revision
from qq_ai_bot.conversation.ordinary_admission import (
    OrdinaryAdmissionConflict,
    OrdinaryAdmissionDuplicate,
    OrdinaryAdmissionRepository,
    OrdinaryParticipationBinding,
)
from qq_ai_bot.conversation.ordinary_admission_db_models import OrdinaryTurnAdmissionModel
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    AttachmentKind,
    ChatResponse,
    InboundMessage,
    MessageAttachment,
    SenderIdentity,
)
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import inputs, work
from qq_ai_bot.services.attachment_inputs import AttachmentInputService
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
from qq_ai_bot.services.media_resolver import MediaResolver
from qq_ai_bot.services.processor import VisualTurnState
from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator

pytestmark = pytest.mark.asyncio


def message(identity="ordinary-1", *, direct=False):
    return InboundMessage(
        message_id=identity,
        event_type="message",
        scope_type=ScopeType.GROUP,
        bot_user_id="8000",
        group_id="2001",
        sender=SenderIdentity("1001"),
        text="接着刚才的话说",
        mentions_bot=direct,
    )


class Participation:
    def __init__(self, harness, *, fast=False):
        self.harness = harness
        self.fast = fast
        self.admitted = []

    async def binding_for_event(self, event_id):
        row = await self.harness.ledger.get_event(event_id)
        scope = await self.harness.conversation_scopes.get(row.scope)
        return OrdinaryParticipationBinding(
            row.canonical_conversation_id,
            scope.generation,
            row.id,
            str(source_revision(row)),
            "discussion-original",
            row.author_person_id,
            ((f"event:{row.id}", 1),),
        )

    async def continuation_for_event(self, event_id):
        return await self.binding_for_event(event_id) if self.fast else None

    async def on_ordinary_admitted(self, binding, admission):
        self.admitted.append((binding, admission))


def harness_for(database, **overrides):
    return build_harness(
        database,
        make_settings(database.url, conversation_semantic_participation_enabled=True, **overrides),
        FakeLLMProvider(lambda _: ChatResponse("", 0)),
    )


async def test_direct_and_fast_continuation_use_ordinary_main_without_work(database):
    harness = harness_for(database, runtime_work_enabled=False)
    service = Participation(harness, fast=True)
    harness.processor.set_participation(service)
    sender = MemorySender()
    for number, direct in enumerate((True, False)):
        result = await harness.processor.handle(message(str(number), direct=direct), sender)
        assert result.reason == "chat"
    assert len(harness.provider.requests) == 2
    assert not sender.messages
    assert len(service.admitted) == 2
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(work)) == 0
        rows = (await session.scalars(select(OrdinaryTurnAdmissionModel))).all()
    assert len(rows) == 2
    assert all(r.route == "ordinary" and r.work_id is None and r.input_id is None for r in rows)
    assert all(binding.event_id == admission.event_id for binding, admission in service.admitted)


async def test_async_promotion_reuses_original_envelope_once_and_survives_snapshot_loss(database):
    harness = harness_for(database)
    service = Participation(harness)
    harness.processor.set_participation(service)
    sender = MemorySender()
    assert (await harness.processor.handle(message(), sender)).reason == "group_observed"
    row = (await harness.ledger.list_scope_recent(message().scope(), limit=10))[0]
    binding = await service.binding_for_event(row.id)
    assert await harness.processor.ordinary_admissions.get(row.id) is None
    results = await asyncio.gather(
        *(harness.processor.promote_committed_event(row.id, binding) for _ in range(3))
    )
    assert sum(r.reason == "chat" for r in results) == 1
    assert len(harness.provider.requests) == 1
    assert len(service.admitted) == 1
    admission = await harness.processor.ordinary_admissions.current(row.id, generation=1)
    assert admission.binding == binding
    assert admission.actor_person_id == row.author_person_id
    assert admission.presence_id == row.ingress_presence_id
    assert len(await harness.ledger.list_scope_recent(row.scope, limit=10)) == 1
    restarted = harness_for(database)
    result = await restarted.processor.promote_committed_event(row.id, binding)
    assert result.reason == "ordinary_already_admitted"
    assert not restarted.provider.requests


async def test_async_file_promotion_preserves_media_permission_isolation(database):
    harness = harness_for(database, runtime_work_enabled=False)
    service = Participation(harness)
    harness.processor.set_participation(service)
    resolver = MediaResolver()
    harness.processor._native_images = AttachmentInputService(
        resolver,
        ImagePreprocessor(),
        concurrency=1,
        pending_limit=2,
        timeout=2,
        max_bytes=100_000,
        images_enabled=lambda: False,
    )
    harness.provider._responder = lambda _: ChatResponse(
        '<yuki-state>{"engage":"quiet"}</yuki-state>', 0
    )
    runtimes = []

    async def observe(runtime, _sequence, _response, _delta):
        runtimes.append(runtime)

    harness.processor._chat.observe_main_response = observe
    inbound = replace(
        message("original-file"),
        sender=SenderIdentity("9000"),
        attachments=(
            MessageAttachment(
                AttachmentKind.FILE,
                "original document",
                file="base64://" + base64.b64encode(b"original document contents").decode(),
                filename="original.txt",
            ),
        ),
    )
    try:
        assert (await harness.processor.handle(inbound, MemorySender())).reason == "group_observed"
        row = (await harness.ledger.list_scope_recent(inbound.scope(), limit=10))[0]
        result = await harness.processor.promote_committed_event(
            row.id, await service.binding_for_event(row.id)
        )
        assert result.reason == "chat" and len(harness.provider.requests) == 1
        assert len(runtimes) == 1
        runtime = runtimes[0]
        assert runtime.actor_is_superuser and runtime.effective_trigger_event_id == row.id
        assert not runtime.allow_generic_onebot
        assert not runtime.allow_admin_actions
        assert not runtime.allow_automation
    finally:
        await resolver.close()


async def test_shared_observation_version_is_not_event_identity_or_busy_consumption():
    coordinator = ConversationTurnCoordinator()
    direct = await coordinator.notify_message("group", protect_from_observations=True)
    async with coordinator.hold("group"):
        b = await coordinator.notify_message("group", observation=True)
        c = await coordinator.notify_message("group", observation=True)
        assert b.version == c.version == direct.version
        assert await coordinator.promote_observation(b) is None
    promoted = await coordinator.promote_observation(b)
    assert promoted.origin == TurnOrigin.USER_MESSAGE
    assert promoted.version > direct.version
    assert await coordinator.promote_observation(c) is None
    newer = await coordinator.notify_message("group", protect_from_observations=True)
    assert coordinator.is_current(newer) and not coordinator.is_current(promoted)


@pytest.mark.parametrize("change", ["content", "generation", "suppressed", "deleted"])
async def test_source_race_rejects_prepared_admission_without_writing(database, change):
    harness = harness_for(database)
    await harness.processor.handle(message(), MemorySender())
    row = (await harness.ledger.list_scope_recent(message().scope(), limit=10))[0]
    repository = OrdinaryAdmissionRepository(database)
    prepared = repository.prepare(row, 1, 1)
    async with database.immediate_session() as session:
        if change == "generation":
            await session.execute(
                update(CanonicalConversationModel)
                .where(CanonicalConversationModel.id == row.canonical_conversation_id)
                .values(generation=2)
            )
        elif change == "deleted":
            await session.execute(delete(ChatEventModel).where(ChatEventModel.id == row.id))
        else:
            await session.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == row.id)
                .values(
                    **(
                        {"content": "修订"}
                        if change == "content"
                        else {"suppression_status": "duplicate", "utterance_fingerprint": "a" * 64}
                    )
                )
            )
    with pytest.raises(OrdinaryAdmissionConflict, match="source_changed"):
        await repository.commit(prepared)
    assert await repository.get(row.id) is None


async def test_competing_claim_and_existing_claim_return_read_only_under_held_writer(database):
    harness = harness_for(database)
    await harness.processor.handle(message(), MemorySender())
    row = (await harness.ledger.list_scope_recent(message().scope(), limit=10))[0]
    repository = OrdinaryAdmissionRepository(database)
    prepared = repository.prepare(row, 1, 1)
    assert sorted(
        await asyncio.gather(repository.commit(prepared), repository.commit(prepared))
    ) == [False, True]
    statements = []

    def capture(_conn, _cursor, statement, *_):
        statements.append(statement.upper().lstrip())

    async with database.immediate_session():
        sql_event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
        try:
            assert not await asyncio.wait_for(repository.commit(prepared), 1)
            assert await repository.admitted_event_ids(
                row.canonical_conversation_id, 1, (row.id,)
            ) == {row.id}
        finally:
            sql_event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert not any(
        s.startswith(("BEGIN IMMEDIATE", "INSERT", "UPDATE", "DELETE")) for s in statements
    )


async def test_work_ready_publication_and_admission_rollback_preserve_budget(
    database,
):
    harness = harness_for(database)
    await harness.processor.handle(message(), MemorySender())
    row = (await harness.ledger.list_scope_recent(message().scope(), limit=10))[0]
    repository = WorkRepository(database)
    lease = await repository.acquire(row.canonical_conversation_id, 1)
    item = await repository.accept(
        lease,
        source_key="original-work",
        source={
            "origin": "user_message",
            "actor_person_id": row.author_person_id,
            "trigger_event_id": row.id,
        },
        goal="原任务",
    )
    input_id = await repository.enqueue(
        row.canonical_conversation_id,
        1,
        "original-input",
        kind="message",
        event_id=row.id,
        work_id=item["id"],
        ready=False,
    )
    prepared = OrdinaryAdmissionRepository.prepare(row, 1, 1)
    publisher = harness.processor._chat._admission_publisher(prepared)

    async def fail_after_claim(session, identity):
        await publisher(session, identity)
        raise RuntimeError("injected publication failure")

    with pytest.raises(RuntimeError, match="publication failure"):
        await repository.prepare_input(input_id, {"text": "补充"}, before_publish=fail_after_claim)
    async with database.sessions() as session:
        assert not await session.scalar(select(inputs.c.ready).where(inputs.c.id == input_id))
    assert await harness.processor.ordinary_admissions.get(row.id) is None
    assert await repository.prepare_input(input_id, {"text": "补充"}, before_publish=publisher)
    admission = await harness.processor.ordinary_admissions.get(row.id)
    assert (
        admission.route == "work"
        and admission.work_id == item["id"]
        and admission.input_id == input_id
    )
    assert await repository.prepare_input(input_id, {"text": "补充"}, before_publish=publisher)
    other_input = await repository.enqueue(
        row.canonical_conversation_id,
        1,
        "different-mailbox-publication",
        kind="message",
        event_id=row.id,
        work_id=item["id"],
        ready=False,
    )
    with pytest.raises(OrdinaryAdmissionDuplicate, match="ordinary_already_admitted"):
        await repository.prepare_input(other_input, {"text": "补充"}, before_publish=publisher)
    async with database.sessions() as session:
        assert not await session.scalar(select(inputs.c.ready).where(inputs.c.id == other_input))
    original = await repository.get(item["id"])
    assert (original["model_requests"], original["tool_calls"], original["active_seconds"]) == (
        0,
        0,
        0,
    )
    await repository.release(lease)


async def test_active_work_continuation_stages_media_before_publish_without_main(database):
    harness = harness_for(database)
    service = Participation(harness, fast=False)
    harness.processor.set_participation(service)
    sender = MemorySender()
    await harness.processor.handle(message("work-source"), sender)
    original = (await harness.ledger.list_scope_recent(message().scope(), limit=10))[0]
    envelope = harness.processor._observed_human_turns[original.id]
    repository = WorkRepository(database)
    lease = await repository.acquire(original.canonical_conversation_id, 1)
    source = {
        "origin": "user_message",
        "actor_user_id": "1001",
        "actor_person_id": original.author_person_id,
        "trigger_event_id": original.id,
    }

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(repository, lease, "original-work", source, validate)
    control.current = await repository.accept(
        lease, source_key="original-work", source=source, goal="原目标"
    )
    async with database.immediate_session() as session:
        await session.execute(
            update(work)
            .where(work.c.id == control.current["id"])
            .values(model_requests=7, tool_calls=3)
        )
    service.fast = True

    async def prepare_media(**kwargs):
        async with database.sessions() as session:
            row = (
                (
                    await session.execute(
                        select(inputs).where(inputs.c.event_id == kwargs["source_event_id"])
                    )
                )
                .mappings()
                .one()
            )
            assert row["work_id"] == control.current["id"] and not row["ready"]
            assert await session.get(OrdinaryTurnAdmissionModel, kwargs["source_event_id"]) is None
        return VisualTurnState(attachment_text="prepared original media")

    harness.processor._analyze_visual_input = prepare_media
    with harness.processor._chat.runtime.bindings.bind(envelope.snapshot.scope_key, control):
        async with harness.processor._turn_coordinator.hold(envelope.snapshot.scope_key):
            result = await harness.processor.handle(message("new-input"), sender)
    assert result.reason == "work_input_queued"
    assert not harness.provider.requests
    current = await repository.get(control.current["id"])
    assert current["source_json"] == control.current["source_json"]
    assert current["goal"] == "原目标"
    assert (current["model_requests"], current["tool_calls"]) == (7, 3)
    new_event = (await harness.ledger.list_scope_recent(message().scope(), limit=10))[-1]
    admission = await harness.processor.ordinary_admissions.current(new_event.id, generation=1)
    assert admission and admission.route == "work" and admission.work_id == control.current["id"]
    await repository.release(lease)
