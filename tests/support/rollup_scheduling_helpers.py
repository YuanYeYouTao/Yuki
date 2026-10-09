"""Rollup wire budgets, protected scheduling and durable single-flight recovery."""

from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelTask,
)
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.model_runtime.routes import ModelRouter


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
