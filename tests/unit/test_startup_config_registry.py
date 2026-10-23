"""Restart declarations and Settings share one actual startup contract."""

import pytest
from tests.conftest import make_settings

from yuki.admin.config_registry import ConfigRegistry
from yuki.admin.config_service import RuntimeConfigService
from yuki.admin.models import ConfigApplyMode
from yuki.config import Settings


async def test_every_restart_declaration_reaches_actual_startup_settings(database):
    settings = make_settings(database.url)
    runtime = RuntimeConfigService(settings=settings, database=database)
    await runtime.initialize()
    updates = await runtime.startup_settings_updates()
    restarted = Settings(_env_file=None, **{**settings.model_dump(), **updates})
    for spec in runtime.registry.list():
        if spec.apply_mode is ConfigApplyMode.RESTART_REQUIRED:
            for name in spec.settings_fields:
                assert name in updates
                assert str(getattr(restarted, name)) == str(spec.default_getter(settings))


@pytest.mark.parametrize(
    ("key", "field", "value"),
    [
        ("plugins.enabled", "plugin_system_enabled", False),
        ("plugins.directory", "plugin_directory", "changed-plugins"),
        ("memory.dream_enabled", "memory_dream_enabled", False),
        ("memory.self_reflection_enabled", "memory_self_reflection_enabled", False),
        ("memory.rebuild_enabled", "memory_rebuild_enabled", False),
        ("memory.dream_schedule_hour", "memory_dream_schedule_hour", 4),
        ("automation.poll_seconds", "automation_poll_seconds", 0.01),
        ("automation.poll_seconds", "automation_poll_seconds", 120),
        ("automation.lease_seconds", "automation_lease_seconds", 1),
        ("automation.lease_seconds", "automation_lease_seconds", 7200),
    ],
)
async def test_saved_restart_setting_is_activated_by_new_settings(database, key, field, value):
    settings = make_settings(
        database.url, **({field: not value} if isinstance(value, bool) else {})
    )
    runtime = RuntimeConfigService(settings=settings, database=database)
    await runtime.initialize()
    saved = await runtime.set_override(
        key,
        value,
        scope_type="global",
        scope_id="",
        actor_user_id="test",
        trigger_message_id="startup-contract",
    )
    assert saved.success and saved.pending_restart
    restarted_runtime = RuntimeConfigService(settings=settings, database=database)
    await restarted_runtime.initialize()
    restarted = Settings(
        _env_file=None,
        **{
            **settings.model_dump(),
            **await restarted_runtime.startup_settings_updates(),
        },
    )
    if field.startswith("automation_"):
        assert getattr(restarted, field) == value
        assert getattr(restarted.automation, field) == value
    else:
        assert str(getattr(restarted, field)) == str(value)


@pytest.mark.parametrize(
    ("name", "invalid", "valid"),
    [
        ("timeout_seconds", 0, 1),
        ("max_download_bytes", 0, 1),
        ("max_duration_seconds", 301, 300),
        ("global_concurrency", 0, 1),
        ("queue_max_pending", 0, 1),
    ],
)
async def test_asr_uses_actual_settings_field_metadata(database, name, invalid, valid):
    registry = ConfigRegistry()
    spec = registry.get(f"asr.{name}")
    with pytest.raises(ValueError):
        registry.convert(spec, invalid)
    assert registry.convert(spec, valid) == valid
    settings = make_settings(database.url)
    runtime = RuntimeConfigService(settings=settings, database=database)
    await runtime.initialize()
    saved = await runtime.set_override(
        spec.key,
        invalid,
        scope_type="global",
        scope_id="",
        actor_user_id="test",
        trigger_message_id="asr-contract",
    )
    assert not saved.success and saved.error_category == "validation_error"
    restarted_runtime = RuntimeConfigService(settings=settings, database=database)
    await restarted_runtime.initialize()
    Settings(
        _env_file=None,
        **{
            **settings.model_dump(),
            **await restarted_runtime.startup_settings_updates(),
        },
    )


async def test_vision_image_count_has_the_same_explicit_value_in_settings_and_hot_config(database):
    settings = make_settings(database.url, vision_max_images_per_turn=6)
    runtime = RuntimeConfigService(settings=settings, database=database)
    await runtime.initialize()
    saved = await runtime.set_override(
        "vision.max_images_per_turn",
        6,
        scope_type="global",
        scope_id="",
        actor_user_id="test",
        trigger_message_id="six-vision-inputs",
    )
    assert saved.success and not saved.pending_restart
    assert runtime.registry.convert(runtime.registry.get("vision.max_images_per_turn"), 6) == 6
    assert settings.vision_max_images_per_turn == 6


@pytest.mark.parametrize(
    "key,field,value",
    [
        ("automation.poll_seconds", "automation_poll_seconds", 0),
        ("automation.poll_seconds", "automation_poll_seconds", -0.01),
        ("automation.lease_seconds", "automation_lease_seconds", 0),
        ("automation.lease_seconds", "automation_lease_seconds", -1),
        ("automation.lease_seconds", "automation_lease_seconds", 1.5),
    ],
)
async def test_automation_config_write_and_settings_share_only_structural_limits(
    database, key, field, value
):
    settings = make_settings(database.url)
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{**settings.model_dump(), field: value})
    runtime = RuntimeConfigService(settings=settings, database=database)
    await runtime.initialize()
    with pytest.raises(ValueError):
        runtime.registry.convert(runtime.registry.get(key), value)
    saved = await runtime.set_override(
        key,
        value,
        scope_type="global",
        scope_id="",
        actor_user_id="test",
        trigger_message_id="automation-positive-contract",
    )
    assert not saved.success and saved.error_category == "validation_error"
    restarted_runtime = RuntimeConfigService(settings=settings, database=database)
    await restarted_runtime.initialize()
    restarted = Settings(
        _env_file=None,
        **{**settings.model_dump(), **await restarted_runtime.startup_settings_updates()},
    )
    assert getattr(restarted, field) == getattr(settings, field)
    assert getattr(restarted.automation, field) == getattr(settings, field)
