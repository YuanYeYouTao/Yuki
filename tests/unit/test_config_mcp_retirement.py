"""Old MCP settings are inert without removing generic tool configuration."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from tests.conftest import make_settings

from qq_ai_bot.admin.config_registry import ConfigRegistry
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.config import Settings
from qq_ai_bot.persistence.models import RuntimeConfigOverrideModel


def test_old_mcp_environment_is_ignored_while_tooling_and_web_remain(monkeypatch):
    monkeypatch.setenv("MCP_ENABLED", "true")
    monkeypatch.setenv("MCP_CONFIG_PATH", "/nonexistent/retired-mcp.json")
    monkeypatch.setenv("MCP_MAX_PARALLEL_CALLS", "invalid-old-value")
    settings = Settings(
        _env_file=None,
        tooling_max_parallel_calls=3,
        web_enabled=True,
        tavily_api_key="synthetic-local-key",
    )
    assert not hasattr(settings, "mcp")
    assert not any(name.startswith("mcp_") for name in Settings.model_fields)
    assert settings.tooling.tooling_max_parallel_calls == 3
    assert settings.web.web_enabled
    registry = ConfigRegistry()
    assert registry.maybe_get("mcp.enabled") is None
    assert registry.maybe_get("tooling.max_parallel_calls") is not None


@pytest.mark.asyncio
async def test_legacy_mcp_override_is_ignored_and_not_deleted(database):
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        session.add(
            RuntimeConfigOverrideModel(
                config_key="mcp.enabled",
                scope_type="global",
                value_json="true",
                value_type="boolean",
                apply_mode="hot",
                version=1,
                created_at=now,
                updated_at=now,
                updated_by="synthetic-test",
            )
        )
    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    expected = await runtime.snapshot()
    assert not hasattr(expected, "mcp")
    assert expected.tooling is not None
    async with database.sessions() as session:
        rows = list(
            await session.scalars(
                select(RuntimeConfigOverrideModel)
                .where(RuntimeConfigOverrideModel.config_key == "mcp.enabled")
                .limit(2)
            )
        )
        assert len(rows) == 1 and rows[0].value_json == "true"
