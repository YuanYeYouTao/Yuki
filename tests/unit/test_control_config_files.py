"""Startup file edits share validation, permissions, revisions and durable effects."""

import asyncio
import json
import os
import threading
import tomllib
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import tomlkit
from fastapi import FastAPI
from sqlalchemy import select
from tests.conftest import make_settings
from tests.unit.test_control_operator_access import operator_file
from tests.unit.test_control_plane_foundation import context

from qq_ai_bot.admin.config_files import ConfigFileError, ConfigFileService
from qq_ai_bot.application.control_access import ControlOperatorAccess
from qq_ai_bot.application.modules.control_plane import ControlPlaneBundle
from qq_ai_bot.control_plane import (
    ControlCommand,
    ControlCommandError,
    ControlCommandService,
    ControlQueryError,
    ControlQueryService,
    ProblemCode,
    YukiControlTarget,
)
from qq_ai_bot.conversation.canonical_db_models import ControlCommandReceiptModel
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.model_runtime.profiles import parse_model_profile_catalog
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.persistence.models import AdminOperationEventModel
from qq_ai_bot.webui.http import attach_webui


@pytest.fixture
def files(database, tmp_path):
    document = {
        "schema_version": 3,
        "profiles": {
            "main": {
                "provider": "fake",
                "model": "offline",
                "timeout_seconds": 10,
                "max_retries": 0,
                "default_temperature": 1,
                "default_max_output_tokens": 1000,
                "capabilities": ["reasoning", "tools", "structured_output"],
                "headers": {"X-Site": "private-header-value"},
            },
        },
        "routes": {task.value: "main" for task in ModelTask},
    }
    profile = tmp_path / "profiles.toml"
    profile.write_text(tomlkit.dumps(document), encoding="utf-8")
    persona = tmp_path / "persona.md"
    persona.write_text("共享人格", encoding="utf-8")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("模板：{{YUKI_PERSONA_CORE}}", encoding="utf-8")
    settings = make_settings(
        database.url,
        model_profiles_file=profile,
        system_prompt_file=prompt,
        bot_persona_file=persona,
    )
    catalog = parse_model_profile_catalog(profile.read_text(encoding="utf-8"))
    return settings, ConfigFileService(settings, catalog)


async def test_saved_and_loaded_states_headers_and_revision(files):
    settings, service = files
    first = await service.read("model_profiles")
    assert first["matches_loaded"] is True
    assert 0 < first["revision"] < 2**53
    assert "private-header-value" not in json.dumps(first)
    draft = first["document"]
    draft["profiles"]["main"]["model"] = "new-model"
    revision = await service.save("model_profiles", first["revision"], {"document": draft})
    assert revision != first["revision"]
    saved = await service.read("model_profiles")
    assert saved["matches_loaded"] is False
    actual = tomllib.loads(settings.model_profiles_file.read_text(encoding="utf-8"))
    assert actual["profiles"]["main"]["headers"]["X-Site"] == "private-header-value"
    assert saved["document"]["profiles"]["main"]["model"] == "new-model"


async def test_native_wire_options_roundtrip_does_not_insert_chat_defaults(files):
    settings, service = files
    raw = tomllib.loads(settings.model_profiles_file.read_text(encoding="utf-8"))
    raw["profiles"]["main"].update(
        protocol="anthropic_messages",
        wire_options={"reasoning": "budget", "thinking_budget_tokens": 2048},
    )
    settings.model_profiles_file.write_text(tomlkit.dumps(raw), encoding="utf-8")
    view = await service.read("model_profiles")
    assert view["valid"] is True
    assert view["document"]["profiles"]["main"]["wire_options"] == {
        "reasoning": "budget",
        "thinking_budget_tokens": 2048,
    }
    await service.save("model_profiles", view["revision"], {"document": view["document"]})
    assert parse_model_profile_catalog(settings.model_profiles_file.read_text(encoding="utf-8"))


async def test_read_reports_readonly_directory_without_changing_file(files, monkeypatch):
    settings, service = files
    before = settings.system_prompt_file.read_bytes()
    monkeypatch.setattr(os, "access", lambda *_: False)
    view = await service.read("system_prompt")
    assert view["writable_directory"] is False
    assert view["content"] == "模板：{{YUKI_PERSONA_CORE}}"
    assert settings.system_prompt_file.read_bytes() == before


@pytest.mark.parametrize("change", ["route", "capability", "secret", "headers", "protocol"])
async def test_invalid_documents_never_replace_file(files, change):
    settings, service = files
    before = settings.model_profiles_file.read_bytes()
    view = await service.read("model_profiles")
    document = view["document"]
    main = document["profiles"]["main"]
    if change == "route":
        document["routes"]["chat_agent"] = "missing"
    elif change == "capability":
        main["capabilities"] = ["reasoning"]
    elif change == "secret":
        main["api_key"] = "never-store-me"
    elif change == "headers":
        main["headers"] = {"Authorization": "secret"}
    else:
        main["protocol"] = "unknown"
    with pytest.raises(ConfigFileError, match="validation_error"):
        await service.save("model_profiles", view["revision"], {"document": document})
    assert settings.model_profiles_file.read_bytes() == before


async def test_invalid_disk_document_does_not_expose_unknown_or_header_fields(files):
    settings, service = files
    with settings.model_profiles_file.open("a", encoding="utf-8") as stream:
        stream.write('\n[profiles.bad]\napi_key = "leaked-secret"\n')
        stream.write('[profiles.bad.provider]\napi_key = "nested-secret"\n')
    result = await service.read("model_profiles")
    assert result["valid"] is False
    assert "leaked-secret" not in json.dumps(result)
    assert "private-header-value" not in json.dumps(result)
    assert "nested-secret" not in json.dumps(result)


async def test_persona_template_save_is_separate_from_current_loaded_prompt(files):
    settings, service = files
    view = await service.read("system_prompt")
    assert view["content"] == "模板：{{YUKI_PERSONA_CORE}}"
    assert view["matches_loaded"] is True
    await service.save("system_prompt", view["revision"], {"content": "新模板"})
    assert settings.system_prompt == "模板：共享人格"
    assert (await service.read("system_prompt"))["matches_loaded"] is False
    with pytest.raises(ConfigFileError, match="version_conflict"):
        await service.save("system_prompt", view["revision"], {"content": "覆盖"})
    fresh = await service.read("system_prompt")
    with pytest.raises(ConfigFileError, match="validation_error"):
        await service.save("system_prompt", fresh["revision"], {"content": "  "})


async def test_crlf_file_matches_startup_universal_newline_decoding(files):
    settings, _ = files
    settings.system_prompt_file.write_bytes("第一行\r\n{{YUKI_PERSONA_CORE}}\r\n".encode())
    loaded = make_settings(
        settings.database_url,
        bot_persona_file=settings.resolved_bot_persona_file,
        system_prompt_file=settings.system_prompt_file,
    )
    service = ConfigFileService(loaded)
    result = await service.read("system_prompt")
    assert result["matches_loaded"] is True
    assert result["content"] == "第一行\r\n{{YUKI_PERSONA_CORE}}\r\n"


async def test_persona_file_selection_does_not_change_when_old_alias_later_appears(
    database, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    await asyncio.to_thread(Path("config").mkdir)
    await asyncio.to_thread(Path("config/persona.md").write_text, "启动时的原文", encoding="utf-8")
    settings = make_settings(database.url, yuki_persona_file=Path("config/yuki_persona_core.md"))
    await asyncio.to_thread(
        Path("config/yuki_persona_core.md").write_text, "后来出现的旧文件", encoding="utf-8"
    )
    service = ConfigFileService(settings)
    result = await service.read("bot_persona")
    assert result["content"] == "启动时的原文" and result["matches_loaded"] is True


async def test_missing_profile_creation_and_server_environment_resolution(files):
    settings, service = files
    first = await service.read("model_profiles")
    settings.model_profiles_file.unlink()
    assert (await service.read("model_profiles"))["revision"] == 0
    draft = first["document"]
    draft["profiles"]["main"]["model_env"] = "LLM_MODEL"
    await service.save("model_profiles", 0, {"document": draft})
    assert (
        tomllib.loads(settings.model_profiles_file.read_text(encoding="utf-8"))["profiles"]["main"][
            "model_env"
        ]
        == "LLM_MODEL"
    )
    assert (
        service._catalog_from(settings.model_profiles_file.read_text(encoding="utf-8"))
        .profiles["main"]
        .model
        == settings.llm_model
    )


async def test_paths_are_finite_and_default_inline_prompt_is_not_file_editable(files):
    settings, service = files
    for value in ("../../.env", "workspace", [], None):
        with pytest.raises(ConfigFileError, match="validation_error"):
            await service.read(value)
    service = ConfigFileService(settings.model_copy(update={"system_prompt_file": None}))
    with pytest.raises(ConfigFileError, match="operation_unavailable"):
        await service.read("system_prompt")


@pytest.mark.skipif(os.name != "posix", reason="Linux file security contract")
async def test_symlink_and_hardlink_config_files_are_rejected(files, tmp_path):
    settings, service = files
    path = settings.system_prompt_file
    original = path.read_bytes()
    target = tmp_path / "target.md"
    target.write_bytes(original)
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(ConfigFileError, match="precondition_failed"):
        await service.read("system_prompt")
    path.unlink()
    os.link(target, path)
    with pytest.raises(ConfigFileError, match="precondition_failed"):
        await service.save("system_prompt", 0, {"content": "changed"})
    assert target.read_bytes() == original


async def test_permission_checks_precede_filesystem_io(database, files, monkeypatch):
    settings, service = files

    def forbidden(*args):
        raise AssertionError("unauthorized filesystem access")

    monkeypatch.setattr(service, "_path", forbidden)
    reader = ControlQueryService(
        ControlQueryAdapter(database, settings=settings, config_files=service)
    )
    writer = ControlCommandService(
        ControlCommandAdapter(database, settings=settings, config_files=service)
    )
    ctx = replace(context("control.config.read"), canonical_target=YukiControlTarget.PERMANENT_YUKI)
    with pytest.raises(ControlQueryError) as exc:
        await reader.read_config_file(ctx, "model_profiles")
    assert exc.value.problem.code is ProblemCode.CAPABILITY_DENIED
    with pytest.raises(ControlCommandError) as exc:
        await writer.save_config_file(
            ctx,
            ControlCommand(
                request_id=ctx.request_id,
                expected_revision=0,
                payload={
                    "action": "save",
                    "resource_id": "system_prompt",
                    "spec": {"content": "x"},
                },
            ),
        )
    assert exc.value.problem.code is ProblemCode.CAPABILITY_DENIED


async def test_receipt_replay_failure_and_restart_unknown_never_resave(
    database, files, monkeypatch
):
    settings, service = files
    writer = ControlCommandAdapter(database, settings=settings, config_files=service)
    commands = ControlCommandService(writer)
    ctx = replace(
        context("control.config.file.mutate"), canonical_target=YukiControlTarget.PERMANENT_YUKI
    )
    view = await service.read("system_prompt")
    command = ControlCommand(
        request_id=ctx.request_id,
        expected_revision=view["revision"],
        payload={"action": "save", "resource_id": "system_prompt", "spec": {"content": "新模板"}},
    )
    first = await commands.save_config_file(ctx, command)
    assert first.success and first.effective_state["status"] == "saved_pending_restart"
    assert (await commands.save_config_file(ctx, command)) == first
    ctx2 = replace(ctx, request_id=type(ctx.request_id).new())
    stale = replace(command, request_id=ctx2.request_id)
    for _ in range(2):
        with pytest.raises(ControlCommandError) as exc:
            await commands.save_config_file(ctx2, stale)
        assert exc.value.problem.code is ProblemCode.VERSION_CONFLICT
    async with database.sessions() as session:
        audits = (await session.scalars(select(AdminOperationEventModel))).all()
        assert "新模板" not in " ".join(row.after_json for row in audits)

    async def fail_after_save(*args):
        await original_save(*args)
        raise RuntimeError("crash after filesystem effect")

    original_save = service.save
    monkeypatch.setattr(service, "save", fail_after_save)
    ctx3 = replace(ctx, request_id=type(ctx.request_id).new())
    revision = (await service.read("system_prompt"))["revision"]
    uncertain = ControlCommand(
        request_id=ctx3.request_id,
        expected_revision=revision,
        payload={
            "action": "save",
            "resource_id": "system_prompt",
            "spec": {"content": "已有副作用"},
        },
    )
    result = await commands.save_config_file(ctx3, uncertain)
    assert not result.success and result.operation.status.value == "unknown"
    assert (await commands.save_config_file(ctx3, uncertain)).operation.status.value == "unknown"
    # Restart recovery does not replace the file or reset the receipt.
    await writer.recover_interrupted_controls()
    assert settings.system_prompt_file.read_text(encoding="utf-8") == "已有副作用"
    ctx4 = replace(ctx, request_id=type(ctx.request_id).new())
    with pytest.raises(ControlCommandError) as exc:
        await commands.save_config_file(ctx4, replace(uncertain, request_id=ctx4.request_id))
    assert exc.value.problem.code is ProblemCode.PRECONDITION_FAILED


async def test_cancellation_keeps_file_ownership_until_os_write_finishes(files, monkeypatch):
    _, service = files
    view = await service.read("system_prompt")
    started, finish = threading.Event(), threading.Event()
    original = service._replace

    def slow(*args):
        started.set()
        assert finish.wait(5)
        return original(*args)

    monkeypatch.setattr(service, "_replace", slow)
    writing = asyncio.create_task(
        service.save("system_prompt", view["revision"], {"content": "保存中"})
    )
    assert await asyncio.to_thread(started.wait, 5)
    writing.cancel()
    await asyncio.sleep(0)
    assert service._lock.locked()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await writing
    assert not service._lock.locked()
    assert (await service.read("system_prompt"))["content"] == "保存中"


async def test_http_file_save_and_same_request_receipt(database, files, tmp_path, monkeypatch):
    settings, service = files
    origin = "http://127.0.0.1:18765"
    secret = "file-test-" + "a" * 48
    monkeypatch.setenv("YUKI_TEST_OPERATOR_TOKEN", secret)
    path, _ = operator_file(
        tmp_path,
        capabilities=(
            "control.config.file.content.read",
            "control.config.file.mutate",
            "control.operation.read",
        ),
    )
    settings = settings.model_copy(update={"webui_enabled": True, "webui_origin": origin})
    adapter = ControlCommandAdapter(database, settings=settings, config_files=service)
    bundle = ControlPlaneBundle(
        ControlOperatorAccess(database, path),
        ControlQueryService(ControlQueryAdapter(database, settings=settings, config_files=service)),
        ControlCommandService(adapter),
        adapter.recover_interrupted_controls,
    )
    app = FastAPI()
    attach_webui(app, settings, lambda: bundle)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
        headers = {"Origin": origin}
        assert (
            await client.post("/api/control/login", headers=headers, json={"credential": secret})
        ).status_code == 200
        session = (await client.get("/api/control/session")).json()
        headers["x-yuki-csrf"] = session["csrf"]
        view = await client.post(
            "/api/control/queries/read_config_file",
            json={"file_id": "system_prompt"},
            headers=headers,
        )
        assert view.status_code == 200
        request_id = context().request_id.text
        headers["x-request-id"] = request_id
        payload = {
            "request_id": request_id,
            "expected_revision": view.json()["data"]["fields"]["revision"],
            "payload": {
                "action": "save",
                "resource_id": "system_prompt",
                "spec": {"content": "浏览器保存"},
            },
        }
        for _ in range(2):
            response = await client.post(
                "/api/control/commands/save_config_file", json=payload, headers=headers
            )
            assert response.status_code == 200, response.text
            assert response.json()["data"]["effective_state"]["status"] == "saved_pending_restart"
        async with database.sessions() as db:
            receipts = (await db.scalars(select(ControlCommandReceiptModel))).all()
            assert len(receipts) == 1 and receipts[0].status == "succeeded"
