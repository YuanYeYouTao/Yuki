"""E9: where a person's message stands in the shared model admission queue.

Reproduced with the production executor and a gated provider double (capacity 1):
- a background Work request (child worker, automation, SELF) already waiting yields
  to a person's later request (the executor's existing foreground-first rule);
- a resumed root Work (for example a parent woken by a delegation card) runs with
  its original foreground priority, so it competes with a person's message in the
  same class; their relative order is whatever the condition variable wakes first,
  not first-come-first-served (recorded, not asserted: it is an implementation
  detail, not a contract).
Q6 made people-first conditional on E9 showing first-come-first-served; it did not,
so no ordering change is made here (taskbook §9 E9 record).
Artifact: test-results/model-admission/e9.json.
"""

import asyncio
import json
from pathlib import Path

from yuki.domain.messages import ChatMessage, ChatRequest
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.executor import TaskModelExecutor
from yuki.model_runtime.models import ModelExecutionPriority, ModelProfile, ModelRoute, ModelTask
from yuki.model_runtime.pool import ModelClientPool
from yuki.model_runtime.profiles import ModelProfileCatalog
from yuki.model_runtime.routes import ModelRouter

ARTIFACT = Path(__file__).resolve().parents[2] / "test-results" / "model-admission"


class GatedProvider(FakeLLMProvider):
    """Holds the first request until released; records admission order."""

    def __init__(self) -> None:
        super().__init__(lambda _: "done")
        self.order: list[str] = []
        self.release = asyncio.Event()

    async def _complete(self, request):
        label = request.messages[-1].content
        self.order.append(label)
        if label == "occupying":
            await self.release.wait()
        return await super()._complete(request)


def _executor(provider, concurrency=1):
    profile = ModelProfile(
        id="offline",
        provider="fake",
        model="offline",
        timeout_seconds=5,
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
        max_concurrency=concurrency,
    )


def _call(models, label, priority):
    return asyncio.create_task(
        models.execute(
            ModelTask.CHAT_AGENT, ChatRequest((ChatMessage("user", label),)), priority=priority
        )
    )


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def _order(arrivals):
    provider = GatedProvider()
    models = _executor(provider)
    try:
        tasks = [_call(models, "occupying", ModelExecutionPriority.FOREGROUND)]
        await _settle()
        for label, priority in arrivals:
            tasks.append(_call(models, label, priority))
            await _settle()
        provider.release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
        return provider.order
    finally:
        await models.close()


async def test_e9_admission_order_with_a_person_arriving_last():
    background = await _order(
        [
            ("background-work", ModelExecutionPriority.BACKGROUND),
            ("person", ModelExecutionPriority.FOREGROUND),
        ]
    )
    # Guaranteed by the executor: a waiting background request yields to a person.
    assert background == ["occupying", "person", "background-work"]
    resumed = await _order(
        [
            ("resumed-work", ModelExecutionPriority.FOREGROUND),
            ("person", ModelExecutionPriority.FOREGROUND),
        ]
    )
    assert sorted(resumed) == ["occupying", "person", "resumed-work"]
    ARTIFACT.mkdir(parents=True, exist_ok=True)
    (ARTIFACT / "e9.json").write_text(
        json.dumps(
            {
                "max_concurrency": 1,
                "background_then_person": {
                    "arrival": ["occupying", "background-work", "person"],
                    "admitted": background,
                },
                "resumed_root_work_then_person": {
                    "arrival": ["occupying", "resumed-work", "person"],
                    "admitted": resumed,
                    "note": "same foreground class; order is not first-come-first-served",
                },
            },
            indent=2,
        )
        + "\n"
    )
