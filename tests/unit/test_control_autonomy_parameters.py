"""WebUI edits the original hot profile without ticking or replacing controller state."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.conftest import make_settings
from tests.unit.test_control_plane_foundation import context
from yuki_participation.autonomy_parameters import DEFAULT_AUTONOMY_PARAMETERS

from qq_ai_bot.admin.config_files import ConfigFileError, ConfigFileService
from qq_ai_bot.control_plane import (
    ControlCommand,
    ControlCommandError,
    ControlCommandService,
    ControlQueryError,
    ControlQueryService,
    ProblemCode,
    YukiControlTarget,
)
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.services.semantic_participation import SemanticParticipationService


@pytest.fixture
def hot_profile(database, tmp_path):
    path = tmp_path / "autonomous-model.json"
    settings = make_settings(database.url, semantic_participation_model_config_file=path)
    host = SemanticParticipationService(SimpleNamespace(settings=settings, database=database))
    service = ConfigFileService(settings, autonomy_parameters=host.control_model_parameters)
    return path, settings, host, service


async def test_original_save_pending_reload_actual_loaded_state_and_replay(database, hot_profile):
    path, settings, host, service = hot_profile
    queries = ControlQueryService(
        ControlQueryAdapter(database, settings=settings, config_files=service)
    )
    commands = ControlCommandService(
        ControlCommandAdapter(database, settings=settings, config_files=service)
    )
    read_ctx = context("control.config.file.content.read")
    first = (await queries.read_config_file(read_ctx, "autonomous_model")).fields
    assert first["revision"] == 0 and first["exists"] is False
    assert first["apply_mode"] == "hot_reload" and first["matches_loaded"] is True
    assert (
        first["document"]
        == first["loaded_document"]
        == DEFAULT_AUTONOMY_PARAMETERS.model_dump(mode="json")
    )
    assert set(first["document"]) == set(first["parameter_schema"]["properties"])
    draft = {**first["document"], "intrinsic_interval_seconds": 120}
    ctx = replace(
        context("control.config.file.mutate"), canonical_target=YukiControlTarget.PERMANENT_YUKI
    )
    command = ControlCommand(
        request_id=ctx.request_id,
        expected_revision=0,
        payload={
            "action": "save",
            "resource_id": "autonomous_model",
            "spec": {"document": draft},
        },
    )
    result = await commands.save_config_file(ctx, command)
    assert result.success and result.effective_state["status"] == "saved_pending_reload"
    assert result.effective_state["revision"] > 0
    assert host.control_model_parameters().intrinsic_interval_seconds == 600
    saved = await service.read("autonomous_model")
    assert (
        saved["matches_loaded"] is False and saved["document"]["intrinsic_interval_seconds"] == 120
    )
    assert str(path) not in json.dumps(saved)
    host._refresh_model_parameters()
    assert host.control_model_parameters().intrinsic_interval_seconds == 120
    assert (await service.read("autonomous_model"))["matches_loaded"] is True
    # An identical original request returns its receipt without touching the file.
    before = path.stat().st_mtime_ns
    replay = await commands.save_config_file(ctx, command)
    assert replay == result
    assert path.stat().st_mtime_ns == before
    with pytest.raises(ConfigFileError, match="version_conflict"):
        await service.save("autonomous_model", 0, {"document": draft})


@pytest.mark.parametrize(
    "document",
    [
        {"quiet_group_floor": 0.1},
        {"silence_rise_seconds": 0},
        {"pressure_bias": 21},
        {"intrinsic_interval_seconds": float("nan")},
        {"intrinsic_interval_seconds": float("inf")},
    ],
)
async def test_invalid_parameters_never_replace_original_file(hot_profile, document):
    path, _, host, service = hot_profile
    await service.save("autonomous_model", 0, {"document": {}})
    before = path.read_bytes()
    view = await service.read("autonomous_model")
    with pytest.raises(ConfigFileError, match="validation_error"):
        await service.save("autonomous_model", view["revision"], {"document": document})
    assert path.read_bytes() == before
    assert host.control_model_parameters() == DEFAULT_AUTONOMY_PARAMETERS


async def test_invalid_disk_preserves_active_profile_and_exposes_only_schema(hot_profile):
    path, _, host, service = hot_profile
    path.write_text('{"intrinsic_interval_seconds":120}', encoding="utf-8")
    host._refresh_model_parameters()
    path.write_text('{"injected":"private-invalid-material"}', encoding="utf-8")
    host._refresh_model_parameters()
    result = await service.read("autonomous_model")
    assert result["valid"] is False and "document" not in result
    assert result["loaded_document"]["intrinsic_interval_seconds"] == 120
    assert result["parameter_schema"]["additionalProperties"] is False
    assert "private-invalid-material" not in json.dumps(result)
    assert host.control_model_parameters().intrinsic_interval_seconds == 120


async def test_hot_profile_uses_existing_file_grants_before_io(database, hot_profile, monkeypatch):
    _, settings, _, service = hot_profile

    def forbidden(*args):
        raise AssertionError("unauthorized filesystem access")

    monkeypatch.setattr(service, "_path", forbidden)
    queries = ControlQueryService(
        ControlQueryAdapter(database, settings=settings, config_files=service)
    )
    commands = ControlCommandService(
        ControlCommandAdapter(database, settings=settings, config_files=service)
    )
    with pytest.raises(ControlQueryError) as exc:
        await queries.read_config_file(
            context("control.execution.metadata.read"), "autonomous_model"
        )
    assert exc.value.problem.code is ProblemCode.CAPABILITY_DENIED
    ctx = replace(
        context("control.config.file.content.read"),
        canonical_target=YukiControlTarget.PERMANENT_YUKI,
    )
    with pytest.raises(ControlCommandError) as exc:
        await commands.save_config_file(
            ctx,
            ControlCommand(
                request_id=ctx.request_id,
                expected_revision=0,
                payload={
                    "action": "save",
                    "resource_id": "autonomous_model",
                    "spec": {"document": {}},
                },
            ),
        )
    assert exc.value.problem.code is ProblemCode.CAPABILITY_DENIED
