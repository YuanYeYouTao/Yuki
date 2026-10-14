"""A retired task declaration cannot prevent deployed model profiles loading."""

from pathlib import Path

from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.model_runtime.profiles import parse_model_profile_catalog


def test_legacy_compaction_route_is_ignored_with_one_warning(caplog):
    document = Path("config/model_profiles.example.toml").read_text()
    document = document.replace("[routes]", '[routes]\nconversation_compaction = "main"')
    catalog = parse_model_profile_catalog(
        document,
        environment={
            "LLM_BASE_URL": "https://model.invalid/v1",
            "LLM_MODEL": "test",
            "LLM_FLASH_BASE_URL": "https://model.invalid/v1",
            "LLM_FLASH_MODEL": "test",
        },
    )
    assert set(catalog.routes) == set(ModelTask)
    assert "conversation_compaction" not in {task.value for task in catalog.routes}
    assert sum("retired model route" in record.message for record in caplog.records) == 1
