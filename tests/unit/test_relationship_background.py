"""Relationship work is a peer of attribution, not a foreground request."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select, update
from tests.conftest import make_settings
from tests.unit.test_relationships import append_user_event
from tests.unit.test_rollup_scheduling import executor

from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatResponse
from qq_ai_bot.domain.relationships import RelationshipEvaluation
from qq_ai_bot.model_runtime.executor import BackgroundModelPreempted
from qq_ai_bot.model_runtime.models import ModelExecutionPriority, ModelTask, StructuredOutputMode
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.persistence.models import (
    PersonRelationshipModel,
    RelationshipEventModel,
    RelationshipJobModel,
)
from qq_ai_bot.persistence.relationship_repository import (
    RelationshipClaimLost,
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
        # A genuine foreground request still preempts attribution promptly.
        started.clear()
        release.clear()
        attribution = asyncio.create_task(
            models.execute(
                ModelTask.MEMORY_ATTRIBUTION,
                ChatRequest(messages=(ChatMessage(role="user", content="attribution"),)),
                priority=ModelExecutionPriority.BEST_EFFORT_BACKGROUND,
            )
        )
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(
            models.execute(
                ModelTask.CHAT_AGENT,
                ChatRequest(messages=(ChatMessage(role="user", content="foreground"),)),
            ),
            2,
        )
        with pytest.raises(BackgroundModelPreempted):
            await attribution
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


async def test_shutdown_cancels_waiting_and_active_evaluations_without_failure(database):
    for limit in (1, 2, 4):
        for waiting in (True, False):
            repository = RelationshipJobRepository(database)
            identity = await append_user_event(database, message_id=f"stop-{limit}-{waiting}")
            await repository.enqueue(
                trigger_event_id=identity, user_id="1001", conversation_key="private:1001"
            )
            entered, model_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

            class Provider:
                async def complete(self, request, *, model_started=model_started, release=release):
                    model_started.set()
                    await release.wait()
                    return ChatResponse(json.dumps({"evaluations": []}), 0)

                async def close(self):
                    pass

            concurrency = ConcurrencyManager(limit)
            models = executor(
                ModelClientPool(injected_profiles={"rollup-test": Provider()}),
                structured_output_mode=StructuredOutputMode.JSON_SCHEMA,
            )
            models._semaphore = asyncio.Semaphore(limit)
            settings = make_settings(database.url, global_llm_concurrency=limit)
            evaluator = LLMRelationshipEvaluator(
                settings=settings, model_executor=models, concurrency=concurrency
            )
            evaluate = evaluator.evaluate

            async def tracked(jobs, *, entered=entered, evaluate=evaluate):
                entered.set()
                return await evaluate(jobs)

            evaluator.evaluate = tracked
            worker = RelationshipWorker(
                settings=settings,
                jobs=repository,
                relationships=RelationshipRepository(database),
                evaluator=evaluator,
            )
            foreground = None
            try:
                if waiting:
                    foreground = asyncio.create_task(
                        concurrency.run_llm(
                            "foreground",
                            lambda models=models: models.execute(
                                ModelTask.CHAT_AGENT,
                                ChatRequest(messages=(ChatMessage(role="user", content="busy"),)),
                            ),
                        )
                    )
                    await asyncio.wait_for(model_started.wait(), 2)
                await worker.start()
                worker._wake.set()
                await asyncio.wait_for(entered.wait(), 2)
                if not waiting:
                    await asyncio.wait_for(model_started.wait(), 2)
                await asyncio.wait_for(worker.close(), 2)
                assert foreground is None or not foreground.done()
                async with database.sessions() as session:
                    row = await session.scalar(
                        select(RelationshipJobModel).where(
                            RelationshipJobModel.trigger_event_id == identity
                        )
                    )
                    assert row.status == "pending" and row.attempts == 0
                    assert row.error_category is None
            finally:
                release.set()
                await worker.close()
                if foreground is not None:
                    await foreground
                await models.close()


async def test_claim_owns_score_and_terminal_transitions_atomically(database):
    repository = RelationshipJobRepository(database)
    relationships = RelationshipRepository(database)
    identity = await append_user_event(database, message_id="claim-effects")
    await repository.enqueue(
        trigger_event_id=identity, user_id="1001", conversation_key="private:1001"
    )
    old = (await repository.claim())[0]
    async with database.sessions.begin() as session:
        await session.execute(
            update(RelationshipJobModel)
            .where(RelationshipJobModel.id == old.job_id)
            .values(updated_at=datetime.now(UTC) - timedelta(minutes=6))
        )
    current = (await repository.claim())[0]
    evaluation = RelationshipEvaluation(1, 1, "care", 0.99)
    await repository.defer((old,))
    await repository.complete((old,))
    await repository.fail(old, "late_failure")
    with pytest.raises(RelationshipClaimLost):
        await relationships.apply_automatic(
            user_id="1001", source_event_id=identity, evaluation=evaluation, claim=old
        )

    statements = []

    def fail_event_insert(conn, cursor, sql, params, context, many):
        statements.append(sql.lstrip().upper())
        if statements[-1].startswith("INSERT INTO RELATIONSHIP_EVENTS"):
            raise RuntimeError("synthetic event failure")

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", fail_event_insert)
    try:
        with pytest.raises(RuntimeError, match="synthetic event failure"):
            await relationships.apply_automatic(
                user_id="1001",
                source_event_id=identity,
                evaluation=evaluation,
                daily_positive_cap=1,
                claim=current,
            )
    finally:
        event.remove(engine, "before_cursor_execute", fail_event_insert)
    first_write = next(
        index for index, sql in enumerate(statements) if sql.startswith(("UPDATE", "INSERT"))
    )
    assert not any(sql.startswith("SELECT") for sql in statements[first_write:])
    async with database.sessions() as session:
        row = await session.get(RelationshipJobModel, current.job_id)
        assert row.status == "processing" and row.attempts == 0
        assert await session.scalar(select(func.count()).select_from(RelationshipEventModel)) == 0
        assert await session.scalar(select(func.count()).select_from(PersonRelationshipModel)) == 0
    snapshot, changed = await relationships.apply_automatic(
        user_id="1001", source_event_id=identity, evaluation=evaluation, claim=current
    )
    assert changed and snapshot.affection_score == 51
    await repository.fail(old, "late_failure_after_commit")
    await repository.defer((current,))
    async with database.sessions() as session:
        row = await session.get(RelationshipJobModel, current.job_id)
        assert row.status == "completed" and row.attempts == 0
        assert await session.scalar(select(func.count()).select_from(RelationshipEventModel)) == 1


async def test_concurrent_person_scores_retry_without_lost_changes_or_daily_cap_bypass(
    database, monkeypatch
):
    import qq_ai_bot.persistence.relationship_repository as module

    repository = RelationshipJobRepository(database)
    relationships = RelationshipRepository(database)
    complete = module._complete_claim
    for cap, user_id in ((0, "1001"), (1, "1002")):
        for index in range(2):
            identity = await append_user_event(
                database, message_id=f"same-person-{cap}-{index}", user_id=user_id
            )
            await repository.enqueue(
                trigger_event_id=identity,
                user_id=user_id,
                conversation_key=f"private:{user_id}",
            )
        await relationships.get_or_create(user_id)
        jobs = await repository.claim()
        prepared = asyncio.Event()
        arrivals = 0

        async def overlap(session, job, *, prepared=prepared):
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                prepared.set()
            await asyncio.wait_for(prepared.wait(), 2)
            return await complete(session, job)

        monkeypatch.setattr(module, "_complete_claim", overlap)

        async def apply(job, *, cap=cap):
            return await relationships.apply_automatic(
                user_id=job.user_id,
                source_event_id=job.trigger_event.id,
                evaluation=RelationshipEvaluation(1, 1, "care", 0.99),
                daily_positive_cap=cap,
                claim=job,
            )

        outcomes = await asyncio.gather(*(apply(job) for job in jobs), return_exceptions=True)
        assert sum(isinstance(result, RuntimeError) for result in outcomes) == 1
        loser = next(
            job
            for job, result in zip(jobs, outcomes, strict=True)
            if isinstance(result, RuntimeError)
        )
        monkeypatch.setattr(module, "_complete_claim", complete)
        # Its earlier claim completion rolled back, so the same owner can recompute
        # from fresh state; production uses the bounded fail/reclaim path instead.
        snapshot, changed = await apply(loser)
        expected = 52 if cap == 0 else 51
        assert changed and snapshot.affection_score == expected and snapshot.trust_score == expected
    async with database.sessions() as session:
        rows = list(await session.scalars(select(RelationshipJobModel)))
        assert all(row.status == "completed" for row in rows)
        assert await session.scalar(select(func.count()).select_from(RelationshipEventModel)) == 4
