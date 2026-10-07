"""Retired generation routing is rejected while original telemetry stays readable."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from qq_ai_bot.model_runtime.db_models import ModelInvocationModel
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.model_runtime.profiles import (
    ModelRuntimeConfigurationError,
    parse_model_profile_catalog,
)
from qq_ai_bot.model_runtime.repository import ModelInvocationRepository

ENVIRONMENT = {
    "LLM_BASE_URL": "https://model.invalid",
    "LLM_MODEL": "main",
    "LLM_REASONING_EFFORT": "low",
    "LLM_FLASH_BASE_URL": "https://background.invalid",
    "LLM_FLASH_MODEL": "background",
}


def test_complete_old_toml_requires_explicit_removal_of_retired_route():
    current = Path("config/model_profiles.example.toml").read_text(encoding="utf-8")
    old = current.replace("[routes]", '[routes]\nautomation_text_generation = "background_tasks"')
    with pytest.raises(
        ModelRuntimeConfigurationError,
        match=r"retired model routes remain \(automation_text_generation\).*remove.*\[routes\]",
    ):
        parse_model_profile_catalog(old, environment=ENVIRONMENT)
    catalog = parse_model_profile_catalog(current, environment=ENVIRONMENT)
    assert set(catalog.routes) == set(ModelTask)
    assert "automation_text_generation" not in {task.value for task in ModelTask}


@pytest.mark.asyncio
async def test_original_generation_telemetry_remains_readable_without_live_enum(database):
    async with database.sessions() as session:
        row = ModelInvocationModel(
            task="automation_text_generation",
            profile_id="original",
            provider="fake",
            model="original-model",
            success=False,
            prompt_tokens=7,
            completion_tokens=3,
            total_tokens=10,
            latency_seconds=1,
            error_category="original_failure",
            created_at=datetime.now(UTC),
        )
        session.add(row)
        await session.commit()
        identity = row.id
    repository = ModelInvocationRepository(database)
    assert (await repository.recent_errors(limit=1))[0].task == "automation_text_generation"
    assert "automation_text_generation" in await repository.stats_by_task()
    async with database.sessions() as session:
        stored = await session.scalar(
            select(ModelInvocationModel).where(ModelInvocationModel.id == identity)
        )
        assert stored.task == "automation_text_generation"
        assert stored.profile_id == "original" and stored.total_tokens == 10
