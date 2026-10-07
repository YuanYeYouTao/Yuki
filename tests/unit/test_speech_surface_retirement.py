"""Retired outgoing speech is rejected while ASR, approvals and history remain usable."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError
from pydantic import ValidationError
from sqlalchemy import select
from tests.conftest import make_settings
from tests.unit.test_plugin_manifest import _plugin_dir

from qq_ai_bot.admin.capabilities import CapabilityRegistry
from qq_ai_bot.admin.config_registry import ConfigRegistry
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.capabilities.models import CapabilityTrustSource
from qq_ai_bot.capabilities.provider import ChatToolCapabilityProvider
from qq_ai_bot.config import Settings
from qq_ai_bot.control_plane import CommandOperation, ControlCommandService, ControlQueryService
from qq_ai_bot.control_plane.capabilities import is_protocol_capability
from qq_ai_bot.control_plane.command_types import (
    failure_audit_target_type,
    project_success_effective,
    success_audit_target_type,
    validate_success_audit_before,
)
from qq_ai_bot.control_plane.surface import method_capability
from qq_ai_bot.deployment_setup import command as setup_command
from qq_ai_bot.deployment_setup.service import compose_profiles_with_features
from qq_ai_bot.persistence.models import RuntimeConfigOverrideModel
from qq_ai_bot.plugin_host.extension_registry import ExtensionRegistry
from qq_ai_bot.plugin_host.facades import HostPluginContext
from qq_ai_bot.plugin_host.manifest import load_manifest
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
from yuki_plugin_sdk import context, models, registrar
from yuki_plugin_sdk.api import DEFAULT_FEATURES, PLUGIN_API_VERSION
from yuki_plugin_sdk.events import EventName
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.testing import fake_services
from yuki_plugin_sdk.testing.contract import run_plugin_contract_tests
from yuki_plugin_sdk.testing.fake_context import FakePluginContext

RETIRED_PERMISSIONS = (
    "speech.profile.read",
    "speech.generate",
    "speech.send",
    "speech.manage",
    "speech.provider.register",
)


@pytest.mark.parametrize("retired_field", ("profile_id", "style_hint", "text"))
def test_admin_action_schema_rejects_retired_speech_arguments(retired_field):
    tools = CapabilityRegistry().definitions()
    tool = next(item for item in tools if item.name == "admin_execute_action")
    assert tool.schema_version == "2"
    assert "profile_id" not in tool.description and "style_hint" not in tool.description
    validator = Draft202012Validator(tool.parameters)
    valid = {"action": "relationship.get", "arguments": {"target": "self"}}
    validator.validate(valid)
    with pytest.raises(SchemaValidationError, match="Additional properties"):
        validator.validate({**valid, "arguments": {"target": "self", retired_field: "retired"}})
    descriptors = ChatToolCapabilityProvider(
        tools, source=CapabilityTrustSource.ADMIN
    ).descriptors()
    descriptor = next(item for item in descriptors if item.model_name == tool.name)
    assert descriptor.schema_version == "2"
    assert descriptor.as_chat_tool().parameters == tool.parameters
    assert not any(
        action.startswith("speech.") for action in tool.parameters["properties"]["action"]["enum"]
    )


def test_retired_sdk_and_control_contracts_cannot_dispatch():
    assert PLUGIN_API_VERSION == "3.2"
    assert not any(feature.startswith("speech.") for feature in DEFAULT_FEATURES)
    for module, symbol in (
        (context, "SpeechFacade"),
        (models, "GeneratedSpeechHandle"),
        (registrar, "TTSProviderRegistration"),
        (fake_services, "FakeSpeechFacade"),
    ):
        assert not hasattr(module, symbol)
    assert not hasattr(context.PluginContext, "speech")
    assert not hasattr(HostPluginContext, "speech")
    assert not hasattr(FakePluginContext("fixture"), "speech")
    assert not hasattr(ExtensionRegistry().registrar("fixture", ()), "register_tts_provider")
    assert not hasattr(registrar.PluginRegistrar, "register_tts_provider")
    for method, service in (
        ("mutate_speech", ControlCommandService),
        ("list_speech_profiles", ControlQueryService),
    ):
        assert not hasattr(service, method)
        with pytest.raises(StopIteration):
            method_capability(method)
    for capability in ("control.speech.read", "control.speech.mutate"):
        assert not is_protocol_capability(capability)
    with pytest.raises(ValueError):
        CommandOperation("control.speech.mutate")


@pytest.mark.parametrize("permission", RETIRED_PERMISSIONS)
def test_api_32_manifest_rejects_retired_permissions(tmp_path, permission):
    root = _plugin_dir(tmp_path)
    path = root / "plugin.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace('"tool.register"', f'"{permission}"'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        PluginPermission(permission)
    from yuki_plugin_sdk.errors import ManifestValidationError

    with pytest.raises(ManifestValidationError):
        load_manifest(root, yuki_version="1.6.0")


@pytest.mark.parametrize(
    "event",
    (
        "worker_started",
        "worker_stopped",
        "profile_loaded",
        "profile_failed",
        "generation_started",
        "generation_completed",
        "generation_failed",
        "generation_cancelled",
        "queued",
        "sent",
        "send_failed",
    ),
)
def test_retired_sdk_event_is_not_a_current_event(event):
    with pytest.raises(ValueError):
        EventName(f"speech.{event}")


async def test_api_31_is_rejected_before_plugin_import(tmp_path):
    root = _plugin_dir(tmp_path)
    path = root / "plugin.toml"
    path.write_text(path.read_text(encoding="utf-8").replace('"3.2"', '"3.1"'), encoding="utf-8")
    marker = root / "imported"
    (root / "echo_plugin.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8"
    )
    report = await run_plugin_contract_tests(root, yuki_version="1.6.0")
    assert not report.passed and report.error_category == "ManifestValidationError"
    assert "entrypoint" not in report.checks and not marker.exists()


async def test_api_32_hash_change_requires_reapproval_and_no_new_permissions(database, tmp_path):
    manifest = load_manifest(_plugin_dir(tmp_path), yuki_version="1.6.0")
    repository = PluginInstallationRepository(database)
    fields = dict(
        plugin_id=manifest.id,
        name=manifest.name,
        version=manifest.version,
        yuki_requires=manifest.yuki_requires,
        entrypoint=manifest.entrypoint,
    )
    await repository.upsert_discovered(
        **fields,
        plugin_api="3.1",
        manifest_hash="old-speech-contract",
        requested_permissions=("tool.register", "speech.send"),
    )
    await repository.approve(manifest.id)
    await repository.set_enabled(manifest.id, enabled=True)
    record = await repository.upsert_discovered(
        **fields,
        plugin_api=manifest.plugin_api,
        manifest_hash=manifest.manifest_hash,
        requested_permissions=(permission.value for permission in manifest.permissions),
    )
    assert record.status == "pending_approval" and not record.enabled
    assert record.approved_at is None and record.approved_permissions == ()
    assert set(record.requested_permissions) == {p.value for p in manifest.permissions}
    assert PluginPermission.MEDIA_ARTIFACT_CREATE.value not in record.requested_permissions


def test_old_speech_env_is_inert_and_asr_and_display_name_stay_valid(monkeypatch):
    for name in (
        "SPEECH_ENABLED",
        "SPEECH_PROVIDER",
        "SPEECH_MAX_SYNTHESIS_CHARACTERS",
        "GENIE_DATA_DIR",
        "BOT_VOICE_NAME",
    ):
        monkeypatch.setenv(name, "invalid retired value")
    settings = Settings(_env_file=None, asr_enabled=True, bot_display_name="  Mika  ")
    assert settings.asr.asr_enabled and settings.bot_identity.display_name == "Mika"
    assert not hasattr(settings, "speech") and not hasattr(settings.bot_identity, "voice_name")
    assert not any(name.startswith("speech_") for name in Settings.model_fields)
    assert (
        "genie_data_dir" not in Settings.model_fields
        and "bot_voice_name" not in Settings.model_fields
    )
    assert ConfigRegistry().maybe_get("speech.enabled") is None
    assert ConfigRegistry().maybe_get("genie.data_dir") is None
    for name in ("", "\r", "Mika\nYuki"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, bot_display_name=name)


async def test_historical_speech_override_is_ignored_and_retained(database):
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        session.add(
            RuntimeConfigOverrideModel(
                config_key="speech.enabled",
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
    assert not hasattr(await runtime.snapshot(), "speech")
    async with database.sessions() as session:
        row = await session.scalar(
            select(RuntimeConfigOverrideModel).where(
                RuntimeConfigOverrideModel.config_key == "speech.enabled"
            )
        )
        assert row is not None and row.value_json == "true"


def test_historical_speech_result_and_audit_remain_readable():
    operation = "control.speech.mutate"
    assert validate_success_audit_before({"revision": 1}, operation=operation) == {"revision": 1}
    state = {"resource": "voice-profile", "revision": 2, "status": "disabled"}
    assert (
        project_success_effective(
            state,
            operation=operation,
            resource_id="voice-profile",
            revision=2,
            semantic_target_id="yuki",
            material={"action": "disable", "resource_id": "voice-profile"},
        )
        == state
    )
    assert success_audit_target_type(operation) == failure_audit_target_type(operation) == "speech"


def test_setup_drops_retired_profile_and_keeps_custom_gateway_profiles():
    assert "speech" not in setup_command._SECTIONS
    assert not hasattr(setup_command, "_page_speech")
    assert (
        compose_profiles_with_features(
            {"COMPOSE_PROFILES": "napcat,speech,custom"}, gateways=("snowluma",)
        )
        == "snowluma,custom"
    )


@pytest.mark.parametrize("arguments", (["speech", "status"], ["prompt", "inspect", "speech"]))
def test_cli_rejects_retired_commands_before_settings_or_database(monkeypatch, arguments):
    from qq_ai_bot import cli
    from qq_ai_bot.services.command_service import CommandService

    def settings_must_not_load():
        raise AssertionError("retired command must be rejected before loading settings")

    monkeypatch.setattr("sys.argv", ["qq-ai-bot-cli", *arguments])
    monkeypatch.setattr(cli, "Settings", settings_must_not_load)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "/ai voice" not in CommandService._help_text()


@pytest.mark.parametrize(
    "relative",
    (
        "plugins/github-monitor",
        "plugins/subscription-monitor",
        "plugins/io.github.yuanyeyoutao.kun-game",
        "examples/plugins/com.example.echo",
    ),
)
def test_bundled_manifests_require_the_first_api_32_host(relative):
    from yuki_plugin_sdk.errors import ManifestValidationError

    root = Path(__file__).resolve().parents[2] / relative
    manifest = load_manifest(root, yuki_version="3.9.0")
    assert manifest.plugin_api == "3.2" and manifest.yuki_requires == ">=3.9.0,<4.0"
    with pytest.raises(ManifestValidationError, match="Yuki"):
        load_manifest(root, yuki_version="3.8.3")
