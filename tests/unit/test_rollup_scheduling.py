"""Rollup wire budgets, protected scheduling and durable single-flight recovery."""

import asyncio
import json

import httpx
import pytest
from tests.unit.test_conversation_rollup_370 import _append, _policy
from tests.unit.test_conversation_rollup_llm_origins import _candidate, _event

from qq_ai_bot.conversation.rollup.service import ConversationRollupService
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatResponse
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


def executor(pool, protocol="responses", **overrides):
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
        capabilities=frozenset(ModelCapability),
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
        max_concurrency=2,
    )


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
async def test_required_rollup_joins_existing_claim_and_has_bounded_fallback(database, expires):
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
        required = asyncio.create_task(
            service.ensure_required_coverage(
                repository=repository,
                scope=scope,
                lease_seconds=2,
                max_batches=1,
                deadline=asyncio.get_running_loop().time() + (0.05 if expires else 1),
            )
        )
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
