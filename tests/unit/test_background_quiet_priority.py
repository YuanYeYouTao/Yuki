"""SELF yields to chat admission and tools, then waits for a quiet interval."""

import asyncio
import time

import pytest

from yuki.domain.messages import ChatMessage, ChatRequest
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.executor import BackgroundModelPreempted, TaskModelExecutor
from yuki.model_runtime.models import ModelExecutionPriority, ModelProfile, ModelRoute, ModelTask
from yuki.model_runtime.pool import ModelClientPool
from yuki.model_runtime.profiles import ModelProfileCatalog
from yuki.model_runtime.routes import ModelRouter


def _executor(provider, *, quiet_seconds=0):
    profile = ModelProfile(
        id="offline",
        provider="fake",
        model="offline",
        timeout_seconds=1,
        max_retries=0,
        default_temperature=0.1,
        default_max_output_tokens=128,
    )
    catalog = ModelProfileCatalog(
        profiles={profile.id: profile},
        routes={task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask},
    )
    return TaskModelExecutor(
        router=ModelRouter(catalog),
        pool=ModelClientPool(injected_profiles={profile.id: provider}),
        self_reflection_quiet_seconds=quiet_seconds,
    )


async def _reflection(models):
    return await models.execute(
        ModelTask.MEMORY_SELF_REFLECTION,
        ChatRequest((ChatMessage("user", "reflection"),)),
        priority=ModelExecutionPriority.BEST_EFFORT_BACKGROUND,
    )


async def test_background_waits_through_foreground_tool_gap():
    provider = FakeLLMProvider(lambda _: "done")
    models = _executor(provider)
    background = None
    try:
        async with models.foreground_activity():
            background = asyncio.create_task(_reflection(models))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not provider.requests
            # An ordinary model call finishes, but chat is still running a tool.
            await models.execute(ModelTask.CHAT_AGENT, ChatRequest((ChatMessage("user", "chat"),)))
            await asyncio.sleep(0)
            assert len(provider.requests) == 1
            assert not background.done()
        assert (await asyncio.wait_for(background, 1)).content == "done"
        assert len(provider.requests) == 2
    finally:
        if background is not None:
            background.cancel()
            await asyncio.gather(background, return_exceptions=True)
        await models.close()


async def test_incoming_chat_preempts_self_before_its_first_model_call():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingProvider(FakeLLMProvider):
        async def _complete(self, request):
            if request.messages[-1].content == "reflection":
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return await super()._complete(request)

    models = _executor(BlockingProvider(lambda _: "done"))
    background = asyncio.create_task(_reflection(models))
    try:
        await asyncio.wait_for(started.wait(), 1)
        async with models.foreground_activity():
            # Only admission has begun; no foreground provider request exists yet.
            with pytest.raises(BackgroundModelPreempted):
                await asyncio.wait_for(background, 1)
            assert cancelled.is_set()
        await asyncio.wait_for(models.wait_for_background_idle(), 1)
    finally:
        background.cancel()
        await asyncio.gather(background, return_exceptions=True)
        await models.close()


async def test_new_chat_restarts_quiet_interval_after_the_turn_finishes():
    quiet = 0.06
    models = _executor(FakeLLMProvider(), quiet_seconds=quiet)
    idle = asyncio.create_task(models.wait_for_background_idle())
    try:
        await asyncio.sleep(quiet / 2)
        async with models.foreground_activity():
            await asyncio.sleep(quiet)
            assert not idle.done()
        ended = time.monotonic()
        await asyncio.wait_for(idle, 1)
        assert time.monotonic() - ended >= quiet * 0.8
    finally:
        idle.cancel()
        await asyncio.gather(idle, return_exceptions=True)
        await models.close()


async def test_cancelled_chat_releases_background_admission():
    models = _executor(FakeLLMProvider())
    entered = asyncio.Event()

    async def chat():
        async with models.foreground_activity():
            entered.set()
            await asyncio.Event().wait()

    turn = asyncio.create_task(chat())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        await asyncio.wait_for(models.wait_for_background_idle(), 1)
        assert (await asyncio.wait_for(_reflection(models), 1)).content
    finally:
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        await models.close()


async def test_foreground_and_maintenance_keep_slots_while_background_yields_and_cancel_releases():
    entered = {name: asyncio.Event() for name in ("maintenance", "foreground", "background")}
    release = {name: asyncio.Event() for name in entered}

    class ControlledProvider(FakeLLMProvider):
        async def _complete(self, request):
            name = request.messages[-1].content
            entered[name].set()
            await release[name].wait()
            return await super()._complete(request)

    models = _executor(ControlledProvider(lambda _: "done"))
    models._max_concurrency = 2

    async def run(name, priority):
        return await models.execute(
            ModelTask.CHAT_AGENT,
            ChatRequest((ChatMessage("user", name),)),
            priority=priority,
        )

    tasks = []
    try:
        maintenance = asyncio.create_task(run("maintenance", ModelExecutionPriority.MAINTENANCE))
        tasks.append(maintenance)
        await asyncio.wait_for(entered["maintenance"].wait(), 1)
        background = asyncio.create_task(run("background", ModelExecutionPriority.BACKGROUND))
        tasks.append(background)
        foreground = asyncio.create_task(run("foreground", ModelExecutionPriority.FOREGROUND))
        tasks.append(foreground)
        await asyncio.wait_for(entered["foreground"].wait(), 1)
        assert not entered["background"].is_set()
        foreground.cancel()
        with pytest.raises(asyncio.CancelledError):
            await foreground
        assert not entered["background"].is_set()
        release["maintenance"].set()
        assert (await asyncio.wait_for(maintenance, 1)).content == "done"
        await asyncio.wait_for(entered["background"].wait(), 1)
        release["background"].set()
        assert (await asyncio.wait_for(background, 1)).content == "done"
        await asyncio.wait_for(models.wait_for_background_idle(), 1)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await models.close()
