"""Hot model saves retire clients only after inherited activations finish."""

from __future__ import annotations

import asyncio
import gc

from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatResponse
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelRoute,
    ModelTask,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.model_runtime.routes import ModelRouter


def catalog(model: str) -> ModelProfileCatalog:
    profile = ModelProfile(
        id="main",
        provider="fake",
        model=model,
        timeout_seconds=2,
        max_retries=0,
        default_temperature=0.1,
        default_max_output_tokens=100,
        capabilities=frozenset(ModelCapability) - {ModelCapability.NATIVE_WEB_SEARCH},
    )
    return ModelProfileCatalog(
        profiles={"main": profile},
        routes={task: ModelRoute(task=task, profile_id="main") for task in ModelTask},
    )


class TrackingPool(ModelClientPool):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1
        await super().close()


async def wait_closed(pool: TrackingPool) -> None:
    for _ in range(30):
        gc.collect()
        await asyncio.sleep(0.01)
        if pool.close_count:
            return
    raise AssertionError("retired model pool did not close")


async def test_repeated_hot_saves_close_unpinned_retired_pools():
    first = TrackingPool()
    executor = TaskModelExecutor(router=ModelRouter(catalog("old")), pool=first)
    pools = [first]
    try:
        for index in range(8):
            next_pool = TrackingPool()
            executor.apply_catalog(catalog(f"new-{index}"), next_pool)
            pools.append(next_pool)
            await wait_closed(pools[-2])
            assert pools[-2].close_count == 1
        assert pools[-1].close_count == 0
        assert not executor._retired_pools
    finally:
        await executor.close()
    assert all(pool.close_count == 1 for pool in pools)


async def test_inherited_child_keeps_old_pool_after_parent_pin_exits():
    first = TrackingPool()
    executor = TaskModelExecutor(router=ModelRouter(catalog("old")), pool=first)
    next_pool = TrackingPool()
    gate = asyncio.Event()

    async def child() -> str:
        await gate.wait()
        return executor.model_name(ModelTask.CHAT_AGENT)

    try:
        with executor.pin():
            inherited = asyncio.create_task(child())
            executor.apply_catalog(catalog("new"), next_pool)
        await asyncio.sleep(0)
        assert first.close_count == 0
        gate.set()
        assert await inherited == "old"
        assert first.close_count == 0  # The retained Task still owns its ContextVar.
        del inherited
        await wait_closed(first)
        assert executor.model_name(ModelTask.CHAT_AGENT) == "new"
    finally:
        gate.set()
        await executor.close()


async def test_direct_execute_keeps_old_pool_during_hot_save():
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider(LLMProvider):
        async def complete(self, _request: ChatRequest) -> ChatResponse:
            started.set()
            await release.wait()
            return ChatResponse(content="finished", latency_seconds=0)

    first = TrackingPool(injected_profiles={"main": BlockingProvider()})
    executor = TaskModelExecutor(router=ModelRouter(catalog("old")), pool=first)
    next_pool = TrackingPool()
    try:
        invocation = asyncio.create_task(
            executor.execute(
                ModelTask.CHAT_AGENT,
                ChatRequest(messages=(ChatMessage(role="user", content="hello"),)),
            )
        )
        await started.wait()
        executor.apply_catalog(catalog("new"), next_pool)
        await asyncio.sleep(0)
        assert first.close_count == 0
        release.set()
        assert (await invocation).content == "finished"
        del invocation
        await wait_closed(first)
    finally:
        release.set()
        await executor.close()
