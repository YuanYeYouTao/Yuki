"""Real personal/operator APIs over original files and one running Settings."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from tests.unit.test_admin_api import TOKEN
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_app_messages import drain
from tests.unit.test_client_api import bearer, pair, person_and_code

from yuki.admin_api.http import attach_admin_api
from yuki.character.prompts import CharacterPrompts
from yuki.character.repository import CharacterRepository
from yuki.client_api.http import ClientApi, attach_client_api
from yuki.client_api.views import ClientViews
from yuki.client_api.works import ClientWorks
from yuki.config import prompt_document_revision, read_prompt_documents
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.persistence.models import ChatEventModel

admin = admin_fixture
app_env = app_fixture


@pytest.fixture
async def prompt_env(app_env, admin, tmp_path):
    _, api, *_ = admin
    settings = app_env.harness.settings
    system = tmp_path / "system_prompt.md"
    persona = tmp_path / "persona.md"
    system.write_text("OLD_SYSTEM\n{{YUKI_PERSONA_CORE}}", encoding="utf-8")
    persona.write_text("OLD_PERSONA", encoding="utf-8")
    settings.system_prompt_file = system
    settings.bot_persona_file = persona
    settings.apply_prompt_documents(
        read_prompt_documents(system, persona, system_prompt_template="unused")
    )
    service = CharacterPrompts(app_env.identity, settings)
    works = ClientWorks(app_env.identity, app_env.gateway)
    views = ClientViews(app_env.identity, works, documents=service)
    app = FastAPI()
    attach_admin_api(app, lambda: replace(api, documents=service))
    attach_client_api(
        app, lambda: ClientApi(app_env.identity, works=works, views=views, documents=service)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        person, code = await person_and_code(client)
        first = await pair(client, code)
        await CharacterRepository(app_env.identity).set_owner(
            str(uuid4()), person, str(uuid4()), granted=True
        )
        issue = await app_env.identity.issue_for_device(
            await app_env.identity.authenticate(first["token"]), str(uuid4())
        )
        second = await pair(client, issue["code"])
        _, code = await person_and_code(client)
        other = await pair(client, code)
        yield SimpleNamespace(
            client=client,
            service=service,
            views=views,
            settings=settings,
            system=system,
            persona=persona,
            first=first,
            second=second,
            other=other,
            app_env=app_env,
        )


async def docs(env, device=None):
    response = await env.client.get(
        "/client/v1/character/prompts", headers=bearer((device or env.first)["token"])
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def save(env, key, content, *, revision=None, request_id=None, device=None):
    if revision is None:
        revision = next(
            item["revision"] for item in (await docs(env))["documents"] if item["key"] == key
        )
    request = {
        "request_id": request_id or str(uuid4()),
        "expected_revision": revision,
        "content": content,
    }
    response = await env.client.put(
        f"/client/v1/character/prompts/{key}",
        json=request,
        headers=bearer((device or env.first)["token"]),
    )
    return response, request


async def test_save_read_metadata_original_replay_and_startup_equivalence(prompt_env, database):
    env = prompt_env
    before = await docs(env)
    response, request = await save(env, "persona", "NEW_PERSONA\n")
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert env.persona.read_bytes() == b"NEW_PERSONA\n"
    assert (
        env.settings.bot_persona == "NEW_PERSONA"
        and env.settings.system_prompt == "OLD_SYSTEM\nNEW_PERSONA"
    )
    assert result["revision"] == prompt_document_revision(b"NEW_PERSONA\n")
    from tests.conftest import make_settings

    cold = make_settings(database.url, system_prompt_file=env.system, bot_persona_file=env.persona)
    assert (cold.system_prompt, cold.bot_persona) == (
        env.settings.system_prompt,
        env.settings.bot_persona,
    )
    after = await docs(env)
    document = next(item for item in after["documents"] if item["key"] == "persona")
    assert document["updated_by"].startswith("device:") and document["writable"]
    assert after["active"]["persona"] == result["revision"] != before["active"]["persona"]
    again, _ = await save(
        env,
        "persona",
        request["content"],
        revision=request["expected_revision"],
        request_id=request["request_id"],
        device=env.second,
    )
    assert again.status_code == 200 and again.json()["data"] == {**result, "replay": True}
    conflict, _ = await save(
        env,
        "persona",
        "different",
        revision=request["expected_revision"],
        request_id=request["request_id"],
    )
    assert conflict.status_code == 409 and conflict.json()["problem"]["code"] == "request_conflict"
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ChatEventModel)) == 0


async def test_two_devices_cas_and_new_uuid_cannot_bypass_old_revision(prompt_env):
    env = prompt_env
    revision = (await docs(env))["active"]["persona"]
    results = await asyncio.gather(
        save(env, "persona", "first", revision=revision),
        save(env, "persona", "second", revision=revision, device=env.second),
    )
    assert sorted(response.status_code for response, _ in results) == [200, 409]
    content = env.persona.read_text()
    stale, _ = await save(env, "persona", content, revision=revision)
    assert stale.status_code == 409 and stale.json()["problem"]["code"] == "version_conflict"


async def test_validation_failure_keeps_both_active_values_and_written_failure_is_visible(
    prompt_env,
):
    env = prompt_env
    original = (
        env.settings.system_prompt,
        env.settings.bot_persona,
        env.settings.active_prompt_revisions,
    )
    env.persona.write_text(" \n", encoding="utf-8")
    response, request = await save(env, "system_prompt", "NEW_SYSTEM {{YUKI_PERSONA_CORE}}")
    assert response.status_code == 409, response.text
    assert response.json()["problem"]["written"] is True
    assert env.system.read_text() == request["content"]
    assert (
        env.settings.system_prompt,
        env.settings.bot_persona,
        env.settings.active_prompt_revisions,
    ) == original
    lookup = await env.client.get(
        f"/client/v1/requests/{request['request_id']}", headers=bearer(env.second["token"])
    )
    assert lookup.json()["data"]["status"] == "failed"
    env.persona.write_text("REPAIRED_PERSONA", encoding="utf-8")
    reload = await env.client.post(
        "/client/v1/character/prompts/reload",
        json={"request_id": str(uuid4())},
        headers=bearer(env.second["token"]),
    )
    assert reload.status_code == 200 and env.settings.system_prompt == "NEW_SYSTEM REPAIRED_PERSONA"


async def test_permissions_warning_environment_template_and_readonly_probe(prompt_env, monkeypatch):
    import yuki.character.prompts as module

    env = prompt_env
    denied = await env.client.get(
        "/client/v1/character/prompts", headers=bearer(env.other["token"])
    )
    assert denied.status_code == 403
    denied, _ = await save(env, "persona", "forged", device=env.other)
    assert denied.status_code == 403
    warning, _ = await save(env, "system_prompt", "MY_FIXED_SYSTEM")
    assert warning.status_code == 200 and warning.json()["data"]["warnings"] == [
        "persona_placeholder_missing"
    ]
    monkeypatch.setattr(module, "_writable", lambda _path: False)
    assert all(not item["writable"] for item in (await docs(env))["documents"])
    denied, _ = await save(env, "persona", "cannot write")
    assert denied.status_code == 409 and denied.json()["problem"]["written"] is False


async def test_actual_inflight_activation_freezes_prefix_and_next_chat_uses_new_pair(prompt_env):
    env = prompt_env
    first_returned = asyncio.Event()
    release = asyncio.Event()
    provider = env.app_env.provider
    complete = provider._complete
    first_request = None

    async def blocked(request):
        nonlocal first_request
        response = await complete(request)
        if first_request is None:
            first_request = deepcopy(request)
            first_returned.set()
            await release.wait()
        return response

    provider._complete = blocked
    response = await env.app_env.client.post(
        "/client/v1/messages",
        json={"request_id": str(uuid4()), "text": "first activation"},
        headers=bearer(env.first["token"]),
    )
    assert response.status_code == 200
    await asyncio.wait_for(first_returned.wait(), 5)
    before = await docs(env)
    saved, _ = await save(env, "system_prompt", "NEW_SYSTEM\n{{YUKI_PERSONA_CORE}}")
    assert saved.status_code == 200
    saved, _ = await save(env, "persona", "NEW_PERSONA")
    assert saved.status_code == 200
    release.set()
    await drain(env.app_env)
    requests = provider.requests[:]
    assert len(requests) == 2
    for request in requests:
        prefix = "\n".join(
            message.content or "" for message in request.messages if message.role == "system"
        )
        assert "OLD_SYSTEM" in prefix and "OLD_PERSONA" in prefix
        assert "NEW_SYSTEM" not in prefix and "NEW_PERSONA" not in prefix
    assert requests[0] == first_request
    response = await env.app_env.client.post(
        "/client/v1/messages",
        json={"request_id": str(uuid4()), "text": "second activation"},
        headers=bearer(env.first["token"]),
    )
    assert response.status_code == 200
    await drain(env.app_env)
    prefix = "\n".join(
        message.content or ""
        for message in provider.requests[2].messages
        if message.role == "system"
    )
    assert "NEW_SYSTEM" in prefix and "NEW_PERSONA" in prefix and "OLD_SYSTEM" not in prefix
    assert (await docs(env))["active"] != before["active"]


@pytest.mark.parametrize("stage", ["A", "B", "C", "D"])
async def test_real_process_crashes_recover_original_receipt_and_do_not_rewrite_after_B(
    prompt_env, database, stage, monkeypatch
):
    import json
    import sys

    from tests.conftest import make_settings

    import yuki.character.prompts as module

    env = prompt_env
    before = await docs(env)
    request = {
        "request_id": str(uuid4()),
        "expected_revision": before["active"]["persona"],
        "content": "RECOVERED_PERSONA",
    }
    payload = {
        "database": database.url,
        "system": str(env.system),
        "persona": str(env.persona),
        "token": env.first["token"],
        "stage": stage,
        "request": request,
    }
    child = r"""
import asyncio, json, os, sys
from pathlib import Path
from yuki.config import Settings
from yuki.client_api.identity import ClientIdentityService
from yuki.character.prompts import CharacterPrompts
from yuki.persistence.database import Database
import yuki.character.prompts as module

async def main():
    payload = json.loads(sys.stdin.read())
    settings = Settings(database_url=payload["database"],
                        system_prompt_file=Path(payload["system"]),
                        bot_persona_file=Path(payload["persona"]))
    identity = ClientIdentityService(Database(payload["database"]))
    actor = await identity.authenticate(payload["token"])
    service = CharacterPrompts(identity, settings)
    if payload["stage"] == "A":
        reserve = service._reserve
        async def interrupted(*args, **kwargs):
            await reserve(*args, **kwargs)
            os._exit(73)
        service._reserve = interrupted
    elif payload["stage"] == "B":
        replace = module._replace
        def interrupted(*args):
            replace(*args)
            os._exit(73)
        module._replace = interrupted
    elif payload["stage"] == "C":
        apply = Settings.apply_prompt_documents
        def interrupted(self, rendered):
            apply(self, rendered)
            os._exit(73)
        Settings.apply_prompt_documents = interrupted
    else:
        finish = service._finish
        async def interrupted(*args, **kwargs):
            await finish(*args, **kwargs)
            os._exit(73)
        service._finish = interrupted
    request = payload["request"]
    await service.save("persona", request["request_id"], request["expected_revision"],
                       request["content"], principal=actor)
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _output, errors = await process.communicate(json.dumps(payload).encode())
    assert process.returncode == 73, errors.decode()
    assert env.persona.read_text() == ("OLD_PERSONA" if stage == "A" else request["content"])
    await env.app_env.identity.receipts.recover_interrupted_controls()
    settings = make_settings(
        database.url, system_prompt_file=env.system, bot_persona_file=env.persona
    )
    recovered = CharacterPrompts(env.app_env.identity, settings)
    app = FastAPI()
    attach_client_api(app, lambda: ClientApi(env.app_env.identity, documents=recovered))
    replacements = []
    replace_file = module._replace

    def replace_once(*args):
        replacements.append(True)
        return replace_file(*args)

    monkeypatch.setattr(module, "_replace", replace_once)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.put(
            "/client/v1/character/prompts/persona",
            json=request,
            headers=bearer(env.second["token"]),
        )
        assert result.status_code == 200, result.text
        retry = await client.put(
            "/client/v1/character/prompts/persona", json=request, headers=bearer(env.first["token"])
        )
        assert retry.status_code == 200 and retry.json()["data"]["replay"]
    assert len(replacements) == (1 if stage == "A" else 0)
    assert settings.bot_persona == request["content"]
    assert settings.system_prompt == "OLD_SYSTEM\nRECOVERED_PERSONA"
    async with database.sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ControlCommandReceiptModel)
                .where(
                    ControlCommandReceiptModel.request_id == request["request_id"],
                )
            )
            == 1
        )


async def test_manual_change_between_write_and_activation_is_not_misreported(
    prompt_env, monkeypatch
):
    import yuki.character.prompts as module

    env = prompt_env
    original = module._replace

    def competing_edit(*args):
        original(*args)
        env.persona.write_text("EXTERNAL_PERSONA", encoding="utf-8")

    monkeypatch.setattr(module, "_replace", competing_edit)
    result, _ = await save(env, "persona", "MY_PERSONA")
    assert result.status_code == 409 and result.json()["problem"]["code"] == "version_conflict"
    assert result.json()["problem"]["written"] is True
    assert env.settings.bot_persona == "OLD_PERSONA"


async def test_environment_template_is_readonly_and_reused_on_each_persona_reload(
    prompt_env, database
):
    from tests.conftest import make_settings

    env = prompt_env
    settings = make_settings(
        database.url, system_prompt="ENV {{YUKI_PERSONA_CORE}}", bot_persona_file=env.persona
    )
    env.service.settings = settings
    document = next(
        item for item in (await docs(env))["documents"] if item["key"] == "system_prompt"
    )
    assert document["content"] == "ENV {{YUKI_PERSONA_CORE}}" and not document["writable"]
    denied, _ = await save(env, "system_prompt", "cannot override environment")
    assert denied.status_code == 409
    for persona in ("SECOND_PERSONA", "THIRD_PERSONA"):
        result, _ = await save(env, "persona", persona)
        assert result.status_code == 200, result.text
        assert settings.system_prompt == "ENV " + persona and settings.bot_persona == persona
    cold = make_settings(
        database.url, system_prompt="ENV {{YUKI_PERSONA_CORE}}", bot_persona_file=env.persona
    )
    assert cold.system_prompt == settings.system_prompt


async def test_real_directory_permissions_report_readonly_without_changing_source(
    prompt_env, tmp_path
):
    env = prompt_env
    directory = tmp_path / "readonly-prompts"
    directory.mkdir()
    system, persona = directory / "system.md", directory / "persona.md"
    system.write_bytes(env.system.read_bytes())
    persona.write_bytes(env.persona.read_bytes())
    env.settings.system_prompt_file = system
    env.settings.bot_persona_file = persona
    env.settings.apply_prompt_documents(
        read_prompt_documents(system, persona, system_prompt_template="unused")
    )
    directory.chmod(0o500)
    try:
        document = next(item for item in (await docs(env))["documents"] if item["key"] == "persona")
        assert document["content"] == "OLD_PERSONA" and not document["writable"]
        denied, _ = await save(env, "persona", "not written")
        assert denied.status_code == 409 and persona.read_text() == "OLD_PERSONA"
    finally:
        directory.chmod(0o700)


async def test_character_sse_hint_reaches_second_owner_device(prompt_env):
    env = prompt_env
    actor = await env.app_env.identity.authenticate(env.second["token"])
    stream = env.views.stream(actor)
    try:
        assert "event: sync" in await anext(stream)
        pending = asyncio.create_task(anext(stream))
        result, _ = await save(env, "persona", "SSE_PERSONA")
        assert result.status_code == 200, result.text
        assert "event: character" in await asyncio.wait_for(pending, 5)
        latest = await docs(env, env.second)
        assert latest["active"]["persona"] == result.json()["data"]["revision"]
    finally:
        await stream.aclose()


async def test_actual_operator_cli_reads_saves_and_reloads_shared_documents(
    prompt_env, tmp_path, monkeypatch, capsys
):
    import argparse
    import json
    from contextlib import asynccontextmanager

    from yuki.admin_api.cli import add_admin_parser, admin_command

    env = prompt_env
    env.client.headers.update(bearer(TOKEN))

    @asynccontextmanager
    async def connected(_settings):
        yield env.client

    monkeypatch.setattr("yuki.admin_api.cli.admin_client", connected)
    parser = argparse.ArgumentParser()
    add_admin_parser(parser.add_subparsers(dest="command", required=True))
    source = tmp_path / "replacement.md"
    source.write_text("CLI_SYSTEM {{YUKI_PERSONA_CORE}}", encoding="utf-8")
    args = parser.parse_args(["admin", "character", "prompt", "set", "system_prompt", str(source)])
    assert await admin_command(None, args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["revision"] == prompt_document_revision(source.read_bytes())
    args = parser.parse_args(["admin", "character", "prompt", "show", "system_prompt"])
    assert await admin_command(None, args) == 0
    assert json.loads(capsys.readouterr().out)["content"] == source.read_text()
    args = parser.parse_args(["admin", "character", "prompt", "reload"])
    assert await admin_command(None, args) == 0
    assert json.loads(capsys.readouterr().out)["active"] == env.settings.active_prompt_revisions


def test_shared_loader_keeps_universal_newlines_and_original_byte_revision(tmp_path):
    system, persona = tmp_path / "system.md", tmp_path / "persona.md"
    system.write_bytes(b"  HEADER\r\n{{YUKI_PERSONA_CORE}}\r\n ")
    persona.write_bytes(b" PERSONA\r\nSECOND\rLINE ")
    loaded = read_prompt_documents(system, persona, system_prompt_template="unused")
    assert loaded.system_prompt == "HEADER\nPERSONA\nSECOND\nLINE"
    assert loaded.persona == "PERSONA\nSECOND\nLINE"
    assert loaded.system_revision == prompt_document_revision(system.read_bytes())
    assert loaded.persona_revision == prompt_document_revision(persona.read_bytes())
