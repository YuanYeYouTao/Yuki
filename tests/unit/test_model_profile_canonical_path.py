"""The deployed model file has one path and missing files fail closed."""

from pathlib import Path

import pytest
from tests.conftest import make_settings

from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.application.modules.model_runtime import ModelRuntimeModule
from qq_ai_bot.deployment_setup.command import _load_current_configuration
from qq_ai_bot.deployment_setup.service import (
    SetupPaths,
    SetupValidationError,
    require_migrated_model_profiles,
)
from qq_ai_bot.model_runtime.profiles import (
    ModelRuntimeConfigurationError,
    load_model_profile_catalog,
)


def test_missing_selected_model_file_does_not_restore_legacy_deepseek(tmp_path: Path) -> None:
    selected = tmp_path / "webui-config" / "model_profiles.toml"
    with pytest.raises(ModelRuntimeConfigurationError, match="configuration is missing"):
        load_model_profile_catalog(selected)


@pytest.mark.asyncio
async def test_runtime_requires_model_file_without_explicit_compatibility(
    database, tmp_path
) -> None:
    settings = make_settings(
        database.url,
        llm_provider="deepseek",
        model_profiles_file=tmp_path / "absent.toml",
    )
    with pytest.raises(
        ModelRuntimeConfigurationError, match="model profile configuration is missing"
    ):
        ModelRuntimeModule(settings.model_runtime, database, lifecycle=LifecycleRegistry()).build()


def test_guided_setup_requires_reviewed_migration_of_existing_legacy_file(tmp_path: Path) -> None:
    paths = SetupPaths(tmp_path)
    assert paths.model_profiles == tmp_path / "webui-config" / "model_profiles.toml"
    assert paths.legacy_model_profiles == tmp_path / "config" / "model_profiles.toml"
    paths.legacy_model_profiles.parent.mkdir()
    paths.legacy_model_profiles.write_text("schema_version = 3\n", encoding="utf-8")
    with pytest.raises(SetupValidationError, match=r"Model-Profile-Path-Migration\.md"):
        require_migrated_model_profiles(paths)
    paths.model_profiles.parent.mkdir()
    paths.model_profiles.write_text("schema_version = 3\n", encoding="utf-8")
    require_migrated_model_profiles(paths)
    assert paths.legacy_model_profiles.read_text(encoding="utf-8") == "schema_version = 3\n"
    paths.env.write_text("MODEL_PROFILES_FILE=config/model_profiles.toml\n", encoding="utf-8")
    with pytest.raises(SetupValidationError, match="仍指向旧路径"):
        _load_current_configuration(paths)
