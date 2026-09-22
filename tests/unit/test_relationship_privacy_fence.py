"""Relationship evidence expires across queued requests, HTTP retries and commits."""

import asyncio

import httpx
from sqlalchemy import func, select
from tests.conftest import make_settings
from tests.unit.test_relationships import append_user_event
from tests.unit.test_rollup_scheduling import executor

from qq_ai_bot.conversation.hydrate import bump_canonical_generation
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatResponse
from qq_ai_bot.domain.relationships import RelationshipEvaluation
from qq_ai_bot.identity.canonical_repository import ensure_person
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.llm.openai_responses import OpenAIResponsesProvider
from qq_ai_bot.model_runtime.dispatch_guard import check_model_dispatch
from qq_ai_bot.model_runtime.models import ModelTask, StructuredOutputMode
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.persistence.models import (
    PersonRelationshipModel,
    RelationshipEventModel,
    RelationshipJobModel,
)
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.persistence.relationship_repository import (
    RelationshipJobRepository,
    RelationshipRepository,
)
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.relationship_evaluator import LLMRelationshipEvaluator
from qq_ai_bot.services.relationship_worker import RelationshipWorker


async def group_job(database, key):
    async with database.immediate_session() as session:
        for user in ("1001", "1002"):
            await ensure_person(session, user)
    ledger = EventLedgerRepository(database)
    for user, content in (("1002", "hello"), ("1001", "1002 helped me")):
        trigger, _ = await ledger.append(
            bot_user_id="8000",
            platform_message_id=f"{key}-{user}",
            scope_type=ScopeType.GROUP,
            group_id="2001",
            sender_user_id=user,
            direction="inbound",
            content=content,
        )
    jobs = RelationshipJobRepository(database)
    await jobs.enqueue(trigger_event_id=trigger.id, user_id="1001", conversation_key="group:2001")
    return jobs, trigger


async def invalidate(database, trigger, kind):
    if kind == "reset":
        async with database.immediate_session() as session:
            await bump_canonical_generation(
                session, trigger.canonical_conversation_id, event_id=trigger.id
            )
    else:
        assert await PeopleRepository(database).delete_person(
            "1002" if kind == "forget_other" else "1001"
        )


async def assert_expired(database, trigger):
    async with database.sessions() as session:
        row = await session.scalar(
            select(RelationshipJobModel).where(RelationshipJobModel.trigger_event_id == trigger.id)
        )
        assert row.status == "failed" and row.attempts == 0
        assert row.error_category == "conversation_generation_changed"
        assert await session.scalar(select(func.count()).select_from(RelationshipEventModel)) == 0
        assert await session.scalar(select(func.count()).select_from(PersonRelationshipModel)) == 0


async def test_queued_relationship_cannot_dispatch_expired_evidence(database, monkeypatch):
    # Exercise both the background priority queue and the final provider semaphore.
    for index, (lane, kind) in enumerate((("priority", "reset"), ("semaphore", "forget_other"))):
        jobs, trigger = await group_job(database, f"queued-{index}")
        other = await append_user_event(database, message_id=f"unaffected-{index}", user_id="1003")
        await jobs.enqueue(trigger_event_id=other, user_id="1003", conversation_key="private:1003")
        queued = asyncio.Event()
        calls = []

        class Provider:
            async def complete(self, request, *, calls=calls):
                calls.append(request)
                return ChatResponse(content='{"evaluations":[]}', latency_seconds=0)

            async def close(self):
                pass

        models = executor(
            ModelClientPool(injected_profiles={"rollup-test": Provider()}),
            structured_output_mode=StructuredOutputMode.JSON_SCHEMA,
        )
        gate = models._background_slot if lane == "priority" else models._semaphore
        await gate.acquire()
        if lane == "semaphore":
            await gate.acquire()
        method = "_execute_background_provider" if lane == "priority" else "_complete_provider"
        original = getattr(models, method)

        async def waiting(*args, original=original, queued=queued):
            queued.set()
            return await original(*args)

        monkeypatch.setattr(models, method, waiting)
        settings = make_settings(database.url)
        worker = RelationshipWorker(
            settings=settings,
            jobs=jobs,
            relationships=RelationshipRepository(database),
            evaluator=LLMRelationshipEvaluator(
                settings=settings, model_executor=models, concurrency=ConcurrencyManager(2)
            ),
        )
        task = asyncio.create_task(worker.process_once())
        try:
            await asyncio.wait_for(queued.wait(), 2)
            await invalidate(database, trigger, kind)
            gate.release()
            if lane == "semaphore":
                gate.release()
            assert await asyncio.wait_for(task, 2) == 0
            assert calls == []
            await assert_expired(database, trigger)
            async with database.sessions() as session:
                unaffected = await session.scalar(
                    select(RelationshipJobModel).where(
                        RelationshipJobModel.trigger_event_id == other
                    )
                )
                assert unaffected.status == "pending" and unaffected.attempts == 0
                assert unaffected.error_category is None
            # The scoped check must not leak into the next unrelated request.
            await models.execute(
                ModelTask.CHAT_AGENT,
                ChatRequest(messages=(ChatMessage(role="user", content="hello"),)),
            )
            assert len(calls) == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await models.close()


async def test_http_retry_revalidates_relationship_claim_for_all_protocols(database):
    for index, provider_type in enumerate(
        (OpenAICompatibleProvider, DeepSeekResponsesProvider, OpenAIResponsesProvider)
    ):
        jobs, trigger = await group_job(database, f"retry-{index}")
        requests = []

        async def respond(request, *, trigger=trigger, requests=requests):
            requests.append(request)
            # This request was already dispatched; the subsequent retry must stop.
            await invalidate(database, trigger, "reset")
            return httpx.Response(503, json={"error": {"message": "try later"}})

        async with httpx.AsyncClient(
            base_url="https://example.test", transport=httpx.MockTransport(respond)
        ) as client:
            provider = provider_type(
                base_url="https://example.test",
                api_key="test",
                timeout_seconds=2,
                max_retries=2,
                client=client,
            )
            settings = make_settings(database.url)
            worker = RelationshipWorker(
                settings=settings,
                jobs=jobs,
                relationships=RelationshipRepository(database),
                evaluator=LLMRelationshipEvaluator(
                    settings=settings, provider=provider, concurrency=ConcurrencyManager(2)
                ),
            )
            assert await worker.process_once() == 0
            await check_model_dispatch()
        assert len(requests) == 1
        await assert_expired(database, trigger)


async def test_relationship_commit_rechecks_generation_after_preparing_score(database, monkeypatch):
    import qq_ai_bot.persistence.relationship_repository as module

    complete = module._complete_claim
    for index, kind in enumerate(("reset", "forget_other", "forget_target")):
        jobs, trigger = await group_job(database, f"commit-{index}")

        class Evaluator:
            async def evaluate(self, claimed):
                return {job.job_id: RelationshipEvaluation(1, 1, "care", 0.99) for job in claimed}

        async def change_before_cas(session, job, *, trigger=trigger, kind=kind):
            # Separate writer succeeds: the result transaction has not taken a write lock.
            await invalidate(database, trigger, kind)
            return await complete(session, job)

        monkeypatch.setattr(module, "_complete_claim", change_before_cas)
        worker = RelationshipWorker(
            settings=make_settings(database.url),
            jobs=jobs,
            relationships=RelationshipRepository(database),
            evaluator=Evaluator(),
        )
        assert await worker.process_once() == 0
        if kind != "forget_target":
            await assert_expired(database, trigger)
        else:
            async with database.sessions() as session:
                assert (
                    await session.scalar(select(func.count()).select_from(RelationshipJobModel))
                    == 0
                )
                assert (
                    await session.scalar(select(func.count()).select_from(RelationshipEventModel))
                    == 0
                )
                assert (
                    await session.scalar(select(func.count()).select_from(PersonRelationshipModel))
                    == 0
                )
