"""Rollup wire budgets, protected scheduling and durable single-flight recovery."""

import asyncio
import json

import httpx
import pytest
from tests.unit.test_conversation_rollup_370 import _append, _policy
from tests.unit.test_conversation_rollup_llm_origins import _candidate, _event

from qq_ai_bot.conversation.rollup.service import ConversationRollupService
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatResponse
from qq_ai_bot.llm.base import LLMError
from qq_ai_bot.model_runtime.dispatch_guard import model_dispatch_guard
from qq_ai_bot.model_runtime.executor import BackgroundModelPreempted, TaskModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelTask,
)
from qq_ai_bot.model_runtime.models import (
    ModelExecutionPriority as Priority,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.services.concurrency import ConcurrencyManager, RequestCancelledError


def executor(pool, protocol="responses", *, max_concurrency=2, **overrides):
    profile = ModelProfile(
        id="rollup-test",
        provider="deepseek",
        protocol=ModelProtocol(protocol),
        base_url="https://rollup.example",
        api_key_env="TEST_KEY",
        model="deepseek-flash",
        timeout_seconds=30,
        max_retries=0,
        default_temperature=0.5,
        default_max_output_tokens=4096,
        capabilities=frozenset(ModelCapability) - {ModelCapability.NATIVE_WEB_SEARCH},
        **overrides,
    )
    return TaskModelExecutor(
        router=ModelRouter(
            ModelProfileCatalog(
                profiles={profile.id: profile},
                routes={task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask},
            )
        ),
        pool=pool,
        max_concurrency=max_concurrency,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2, 4])
async def test_single_admission_reserves_foreground_against_durable_background_and_maintenance(
    limit,
):
    entered = []
    changed = asyncio.Condition()
    release = asyncio.Event()
    active = 0
    background_active = 0
    maxima = [0, 0]

    class Provider:
        async def complete(self, request):
            nonlocal active, background_active
            text = request.messages[0].content
            background = text != "foreground"
            async with changed:
                entered.append(text)
                active += 1
                background_active += background
                maxima[0] = max(maxima[0], active)
                maxima[1] = max(maxima[1], background_active)
                changed.notify_all()
            try:
                if background:
                    await release.wait()
                return ChatResponse(content="done", latency_seconds=0)
            finally:
                active -= 1
                background_active -= background

        async def close(self):
            pass

    models = executor(
        ModelClientPool(injected_profiles={"rollup-test": Provider()}), max_concurrency=limit
    )
    cancellation = ConcurrencyManager(limit)

    def request(text):
        return ChatRequest(messages=(ChatMessage(role="user", content=text),))

    tasks = []
    try:
        tasks.append(
            asyncio.create_task(
                models.execute(
                    ModelTask.CONVERSATION_COMPACTION,
                    request("maintenance"),
                    priority=Priority.MAINTENANCE,
                )
            )
        )
        async with changed:
            await asyncio.wait_for(changed.wait_for(lambda: "maintenance" in entered), 2)
        for index in range(limit):
            tasks.append(
                asyncio.create_task(
                    cancellation.run_llm(
                        f"background-{index}",
                        lambda index=index: models.execute(
                            ModelTask.CHAT_AGENT,
                            request(f"background-{index}"),
                            priority=Priority.BACKGROUND,
                        ),
                    )
                )
            )
        if limit > 1:
            async with changed:
                await asyncio.wait_for(changed.wait_for(lambda: background_active == limit - 1), 2)
        foreground_queued = asyncio.Event()

        async def foreground_operation():
            foreground_queued.set()
            return await models.execute(ModelTask.CHAT_AGENT, request("foreground"))

        foreground = asyncio.create_task(cancellation.run_llm("foreground", foreground_operation))
        tasks.append(foreground)
        if limit > 1:
            await asyncio.wait_for(foreground, 2)
            assert all(not task.done() for task in tasks[:-1])
        else:
            # A single slot can only serialize; foreground wins the next admission.
            await asyncio.wait_for(foreground_queued.wait(), 2)
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(foreground, 2)
            assert entered[1] == "foreground"
        release.set()
        await asyncio.gather(*tasks)
        assert maxima[0] <= limit
        assert maxima[1] <= max(1, limit - 1)
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await models.close()


@pytest.mark.asyncio
async def test_queued_cancellation_and_invalid_source_never_dispatch_or_leak_capacity():
    started, release = asyncio.Event(), asyncio.Event()
    seen = []

    class Provider:
        async def complete(self, request):
            text = request.messages[0].content
            seen.append(text)
            if text == "maintenance":
                started.set()
                await release.wait()
            return ChatResponse(content="done", latency_seconds=0)

        async def close(self):
            pass

    models = executor(
        ModelClientPool(injected_profiles={"rollup-test": Provider()}), max_concurrency=1
    )
    cancellation = ConcurrencyManager(1)

    def request(text):
        return ChatRequest(messages=(ChatMessage(role="user", content=text),))

    tasks = []
    checks = 0

    async def rejected():
        nonlocal checks
        checks += 1
        raise LLMError("source_expired_after_queue")

    queued_started = asyncio.Event()

    async def queued():
        queued_started.set()
        with model_dispatch_guard(rejected):
            return await models.execute(ModelTask.CHAT_AGENT, request("stale"))

    try:
        maintenance = asyncio.create_task(
            models.execute(
                ModelTask.CONVERSATION_COMPACTION,
                request("maintenance"),
                priority=Priority.MAINTENANCE,
            )
        )
        tasks.append(maintenance)
        await asyncio.wait_for(started.wait(), 2)
        cancelled = asyncio.create_task(cancellation.run_llm("cancelled", queued))
        tasks.append(cancelled)
        await asyncio.wait_for(queued_started.wait(), 2)
        assert await cancellation.cancel("cancelled")
        with pytest.raises(RequestCancelledError):
            await cancelled
        assert checks == 0
        queued_started.clear()
        stale = asyncio.create_task(cancellation.run_llm("stale", queued))
        tasks.append(stale)
        await asyncio.wait_for(queued_started.wait(), 2)
        release.set()
        with pytest.raises(LLMError, match="source_expired_after_queue"):
            await stale
        assert checks == 1
        assert seen == ["maintenance"]
        await models.execute(ModelTask.CHAT_AGENT, request("fresh"), priority=Priority.REQUIRED)
        assert seen == ["maintenance", "fresh"]
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await models.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
async def test_rollup_wire_budget_and_transport_timeout_are_independent(protocol):
    seen = []

    def transport(request):
        seen.append((json.loads(request.content), request.extensions["timeout"]))
        truncated = len(seen) == 3
        reasoning_only = len(seen) == 4
        if protocol == "responses":
            payload = {
                "id": "r",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "summary"}],
                    }
                ],
            }
            if truncated:
                payload.update(
                    status="incomplete", incomplete_details={"reason": "max_output_tokens"}
                )
            if reasoning_only:
                payload["output"] = [
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "private reasoning"}],
                    }
                ]
        else:
            payload = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "" if reasoning_only else "summary",
                            "reasoning_content": "private reasoning",
                        },
                        "finish_reason": "length" if truncated else "stop",
                    }
                ]
            }
        return httpx.Response(200, json=payload)

    pool = ModelClientPool(secret_overrides={"TEST_KEY": "test"})
    client = httpx.AsyncClient(
        base_url="https://rollup.example", transport=httpx.MockTransport(transport)
    )
    pool._connection_pools[("deepseek", "https://rollup.example", "TEST_KEY")] = client
    models = executor(pool, protocol)
    service = ConversationRollupService(models=models, config=_policy(), timeout_seconds=90)
    try:
        await service.summarize_candidate(_candidate((_event(1, origin="user_message"),)))
        await models.execute(
            ModelTask.CHAT_AGENT, ChatRequest(messages=(ChatMessage(role="user", content="hello"),))
        )
        body, timeout = seen[0]
        assert body.get("max_output_tokens", body.get("max_tokens")) == 16384
        assert timeout["read"] == 90
        assert seen[1][1]["read"] == 30
        if protocol == "responses":
            assert body["reasoning"]["effort"] == "low"
        else:
            assert "enabled" in json.dumps(body) and "low" in json.dumps(body)
        assert pool.connection_pool_count == 1
        from qq_ai_bot.conversation.rollup.errors import model_failure_error_category
        from qq_ai_bot.llm.base import LLMEmptyResponseError, LLMIncompleteResponseError

        with pytest.raises(LLMIncompleteResponseError):
            await service.summarize_candidate(_candidate((_event(1, origin="user_message"),)))
        with pytest.raises(LLMEmptyResponseError) as caught:
            await service.summarize_candidate(_candidate((_event(1, origin="user_message"),)))
        assert model_failure_error_category(caught.value) == "model_reasoning_only"
    finally:
        await models.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("exclusive", [False, True])
async def test_started_maintenance_survives_chat_but_yields_to_exclusive(exclusive):
    started, finish = asyncio.Event(), asyncio.Event()

    class Provider:
        async def complete(self, request):
            if request.messages[0].content == "rollup":
                started.set()
                await finish.wait()
            return ChatResponse(content="done", latency_seconds=0)

        async def close(self):
            pass

    models = executor(ModelClientPool(injected_profiles={"rollup-test": Provider()}))

    def request(text):
        return ChatRequest(messages=(ChatMessage(role="user", content=text),))

    task = asyncio.create_task(
        models.execute(
            ModelTask.CONVERSATION_COMPACTION, request("rollup"), priority=Priority.MAINTENANCE
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        response = await asyncio.wait_for(
            models.execute(
                ModelTask.CHAT_AGENT,
                request("chat"),
                priority=Priority.EXCLUSIVE if exclusive else Priority.FOREGROUND,
            ),
            1,
        )
        assert response.content == "done"
        if exclusive:
            with pytest.raises(BackgroundModelPreempted):
                await task
        else:
            assert not task.done()
            finish.set()
            assert (await task).content == "done"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await models.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("expires", [False, True])
async def test_required_rollup_joins_existing_claim_and_has_bounded_fallback(
    database, expires, monkeypatch
):
    from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository
    from qq_ai_bot.conversation.rollup.worker import ConversationRollupWorker
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    config = _policy()
    repository = ConversationRollupRepository(database, config=config)
    scope = ConversationScope.private("bot-a", "join")
    await _append(ScopedEventLedgerUnitOfWork(database, config=config), scope, 8)
    started, finish = asyncio.Event(), asyncio.Event()

    class Models:
        calls = 0

        async def execute(self, *args, **kwargs):
            self.calls += 1
            started.set()
            await finish.wait()
            return ChatResponse(content="semantic", latency_seconds=0)

    models = Models()
    service = ConversationRollupService(models=models, config=config, timeout_seconds=1)
    worker = ConversationRollupWorker(
        repository=repository,
        service=service,
        enabled=True,
        concurrency=1,
        poll_seconds=0.01,
        lease_seconds=2,
        heartbeat_seconds=0.05,
        retry_max_seconds=60,
        max_batches_per_claim=1,
    )
    await worker.start()
    try:
        await asyncio.wait_for(started.wait(), 2)
        # Hold the worker until the required caller has captured the initial
        # coverage. Merely creating its task does not guarantee it has started.
        initial_read = asyncio.Event()
        original_status = repository.status

        async def observed_status(*args, **kwargs):
            result = await original_status(*args, **kwargs)
            if asyncio.current_task() is required:
                initial_read.set()
            return result

        monkeypatch.setattr(repository, "status", observed_status)
        required = asyncio.create_task(
            service.ensure_required_coverage(
                repository=repository,
                scope=scope,
                lease_seconds=2,
                max_batches=1,
                deadline=asyncio.get_running_loop().time() + (0.05 if expires else 1),
            )
        )
        await asyncio.wait_for(initial_read.wait(), 2)
        if not expires:
            finish.set()
        assert await asyncio.wait_for(required, 2) == 1
        assert models.calls == 1
        status = await repository.detailed_status(scope)
        if expires:
            assert status.semantic is None and status.overlay is not None
        else:
            assert status.semantic is not None
    finally:
        await worker.close()
    assert not service._active


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["reasoning", "long", "truncated", "cap"])
async def test_rollup_rejects_invalid_output_without_committing_reasoning(case):
    from qq_ai_bot.conversation.rollup.errors import model_failure_error_category
    from qq_ai_bot.domain.messages import ModelResponseStatus

    class Provider:
        calls = 0

        async def complete(self, request):
            self.calls += 1
            return ChatResponse(
                content="" if case == "reasoning" else "x" * (3000 if case == "long" else 1),
                reasoning_content="private reasoning",
                latency_seconds=0,
                status=ModelResponseStatus.INCOMPLETE
                if case == "truncated"
                else ModelResponseStatus.COMPLETED,
            )

        async def close(self):
            pass

    provider = Provider()
    models = executor(
        ModelClientPool(injected_profiles={"rollup-test": provider}),
        max_output_tokens_limit=8192 if case == "cap" else 32768,
    )
    service = ConversationRollupService(models=models, config=_policy(), timeout_seconds=90)
    try:
        with pytest.raises((RuntimeError, ValueError)) as caught:
            await service.summarize_candidate(_candidate((_event(1, origin="user_message"),)))
        assert (
            model_failure_error_category(caught.value)
            == {
                "reasoning": "model_reasoning_only",
                "long": "model_summary_too_long",
                "truncated": "model_truncated",
                "cap": "LLMUnsupportedFeatureError",
            }[case]
        )
        assert provider.calls == (0 if case == "cap" else 1)
    finally:
        await models.close()


@pytest.mark.asyncio
async def test_required_can_start_semantic_work_without_blocking_database_writes(database):
    from sqlalchemy import text

    from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    config = _policy()
    repository = ConversationRollupRepository(database, config=config)
    scope = ConversationScope.private("bot-a", "required")
    await _append(ScopedEventLedgerUnitOfWork(database, config=config), scope, 8)

    class Models:
        async def execute(self, task, request, **kwargs):
            assert kwargs["priority"] is Priority.REQUIRED
            async with database.immediate_session() as session:
                await session.execute(text("SELECT 1"))
            return ChatResponse(content="required semantic", latency_seconds=0)

    service = ConversationRollupService(models=Models(), config=config, timeout_seconds=1)
    assert (
        await asyncio.wait_for(
            service.ensure_required_coverage(
                repository=repository,
                scope=scope,
                lease_seconds=2,
                max_batches=1,
                deadline=asyncio.get_running_loop().time() + 1,
            ),
            2,
        )
        == 1
    )
    state = await repository.detailed_status(scope)
    assert state.semantic is not None and state.overlay is None
