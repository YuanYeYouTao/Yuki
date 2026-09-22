"""Relationship work is a peer of attribution, not a foreground request."""

import asyncio
import json
from datetime import timedelta

from sqlalchemy import select, update
from tests.conftest import make_settings
from tests.unit.test_relationships import append_user_event
from tests.unit.test_rollup_scheduling import executor

from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatResponse
from qq_ai_bot.model_runtime.executor import BackgroundModelPreempted
from qq_ai_bot.model_runtime.models import ModelExecutionPriority, ModelTask, StructuredOutputMode
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.persistence.models import RelationshipJobModel
from qq_ai_bot.persistence.relationship_repository import (
    RelationshipJobRepository,
    RelationshipRepository,
)
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.relationship_evaluator import LLMRelationshipEvaluator
from qq_ai_bot.services.relationship_worker import RelationshipWorker


async def test_relationship_waits_for_attribution_instead_of_preempting(database):
    identity = await append_user_event(database, message_id="background-order")
    repository = RelationshipJobRepository(database)
    await repository.enqueue(
        trigger_event_id=identity, user_id="1001", conversation_key="private:1001"
    )
    jobs = await repository.claim()
    started, release, relationship_admitted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    order = []

    class Provider:
        async def complete(self, request):
            if request.messages[0].content == "attribution":
                order.append("attribution-start")
                started.set()
                await release.wait()
                order.append("attribution-end")
                return ChatResponse(content="done", latency_seconds=0)
            order.append("relationship")
            return ChatResponse(
                content=json.dumps(
                    {
                        "evaluations": [
                            {
                                "job_id": jobs[0].job_id,
                                "affection_delta": 0,
                                "trust_delta": 0,
                                "reason_code": "neutral",
                                "confidence": 0.9,
                            }
                        ]
                    }
                ),
                latency_seconds=0,
            )

        async def close(self):
            pass

    class Concurrency(ConcurrencyManager):
        async def run_llm(self, key, operation, **kwargs):
            assert kwargs["background"] is True
            relationship_admitted.set()
            return await super().run_llm(key, operation, **kwargs)

    models = executor(
        ModelClientPool(injected_profiles={"rollup-test": Provider()}),
        structured_output_mode=StructuredOutputMode.JSON_SCHEMA,
    )
    execute = models.execute

    async def checked_execute(task, request, **kwargs):
        if task is ModelTask.RELATIONSHIP_EVALUATION:
            assert kwargs["priority"] is ModelExecutionPriority.BEST_EFFORT_BACKGROUND
        return await execute(task, request, **kwargs)

    models.execute = checked_execute
    evaluator = LLMRelationshipEvaluator(
        settings=make_settings(database.url), model_executor=models, concurrency=Concurrency(2)
    )
    attribution = asyncio.create_task(
        models.execute(
            ModelTask.MEMORY_ATTRIBUTION,
            ChatRequest(messages=(ChatMessage(role="user", content="attribution"),)),
            priority=ModelExecutionPriority.BEST_EFFORT_BACKGROUND,
        )
    )
    relationship = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        relationship = asyncio.create_task(evaluator.evaluate(jobs))
        await asyncio.wait_for(relationship_admitted.wait(), 2)
        release.set()
        await asyncio.wait_for(asyncio.gather(attribution, relationship), 2)
        assert order == ["attribution-start", "attribution-end", "relationship"]
    finally:
        for task in (attribution, relationship):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(t for t in (attribution, relationship) if t), return_exceptions=True)
        await models.close()


async def test_foreground_preemption_requeues_without_consuming_failure_attempt(database):
    identity = await append_user_event(database, message_id="preempt-relationship")
    repository = RelationshipJobRepository(database)
    await repository.enqueue(
        trigger_event_id=identity, user_id="1001", conversation_key="private:1001"
    )

    class Preempted:
        async def evaluate(self, jobs):
            raise BackgroundModelPreempted("foreground")

    worker = RelationshipWorker(
        settings=make_settings(database.url),
        jobs=repository,
        relationships=RelationshipRepository(database),
        evaluator=Preempted(),
    )
    assert await worker.process_once() == 0
    async with database.sessions() as session:
        job = await session.scalar(select(RelationshipJobModel))
        assert job.status == "pending"
        assert job.attempts == 0
        assert job.error_category is None
        assert job.next_attempt_at > job.updated_at


async def test_late_preemption_does_not_release_a_newer_claim(database):
    identity = await append_user_event(database, message_id="late-preemption")
    repository = RelationshipJobRepository(database)
    await repository.enqueue(
        trigger_event_id=identity, user_id="1001", conversation_key="private:1001"
    )
    old = await repository.claim()
    replacement_time = old[0].claimed_at + timedelta(minutes=6)
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(RelationshipJobModel)
            .where(RelationshipJobModel.id == old[0].job_id)
            .values(updated_at=replacement_time)
        )
    await repository.defer(old)
    async with database.sessions() as session:
        job = await session.get(RelationshipJobModel, old[0].job_id)
        assert job.status == "processing"
        assert job.updated_at.replace(tzinfo=None) == replacement_time.replace(tzinfo=None)
