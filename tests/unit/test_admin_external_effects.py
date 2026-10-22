"""Independent management requests use domain revisions, not historical unknowns."""

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.unit.test_admin_api import admin as admin
from tests.unit.test_admin_api import command, plugin_action

from yuki.admin.authority.command_types import ControlCommandError
from yuki.admin.authority.problems import Problem, ProblemCode
from yuki.admin.config_files import ConfigFileError
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.model_runtime.secrets import model_secrets_path


@pytest.mark.parametrize("external", [False, True])
async def test_typed_commit_ack_loss_keeps_original_success_receipt(
    admin, database, monkeypatch, external
):
    client, _, manager, *_ = admin
    if external:
        await plugin_action(client, "approve", permissions=["tool.register"])
        current = (await client.get("/admin/v1/plugins/test.contract")).json()["data"]
        request = command(current["revision"])
        url = "/admin/v1/plugins/test.contract/actions/enable"
    else:
        request = command(display_name="original created Person")
        url = "/admin/v1/persons"
    commit = AsyncSession.commit
    lost = False

    async def lose_terminal_ack(session):
        nonlocal lost
        receipt = await session.scalar(
            select(ControlCommandReceiptModel).where(
                ControlCommandReceiptModel.request_id == request["request_id"]
            )
        )
        terminal = receipt is not None and receipt.status == "succeeded"
        await commit(session)
        if terminal and not lost:
            lost = True
            raise ControlCommandError(Problem(ProblemCode.PRECONDITION_FAILED))

    with monkeypatch.context() as injected:
        injected.setattr(AsyncSession, "commit", lose_terminal_ack)
        first = await client.post(url, json=request)
    assert lost
    assert first.status_code == (200 if external else 500), first.text
    if not external:
        assert first.json()["problem"]["code"] == "unknown"
    replay = await client.post(url, json=request)
    assert replay.status_code == 200 and replay.json()["data"]["success"]
    if external:
        assert manager.running_plugin_ids == ("test.contract",)
        assert first.json() == replay.json()
    async with database.read_session() as reader:
        receipt = await reader.scalar(
            select(ControlCommandReceiptModel).where(
                ControlCommandReceiptModel.request_id == request["request_id"]
            )
        )
        assert receipt.status == "succeeded" and receipt.problem_code is None


async def test_prewrite_model_failure_is_failed_and_new_request_can_apply(admin, monkeypatch):
    client, api, _manager, _models, profiles = admin
    before = profiles.read_bytes()
    current = (await client.get("/admin/v1/models")).json()["data"]
    document = current["document"]
    document["profiles"]["main"]["model"] = "recovered-model"
    body = command(current["revision"], document=document)

    def readonly(*args, **kwargs):
        raise PermissionError("read-only deployment directory")

    with monkeypatch.context() as injected:
        injected.setattr("yuki.admin.config_files.tempfile.mkstemp", readonly)
        failed = await client.post("/admin/v1/models/apply", json=body)
    assert profiles.read_bytes() == before
    assert failed.status_code == 503, failed.text
    assert failed.json()["problem"]["code"] == "operation_unavailable"
    assert (await client.post("/admin/v1/models/apply", json=body)).json() == failed.json()
    applied = await client.post(
        "/admin/v1/models/apply", json=command(current["revision"], document=document)
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["data"]["success"]
    assert api.commands.model.files.loaded_catalog.profiles["main"].model == "recovered-model"


async def test_unknown_enable_does_not_block_new_explicit_stop(admin, monkeypatch):
    client, _api, manager, *_ = admin
    await plugin_action(client, "approve", permissions=["tool.register"])
    shown = (await client.get("/admin/v1/plugins/test.contract")).json()["data"]
    original = manager.enable
    calls = []

    async def lost(*args, **kwargs):
        result = await original(*args, **kwargs)
        calls.append(result.plugin_id)
        raise RuntimeError("acknowledgement lost after effect")

    monkeypatch.setattr(manager, "enable", lost)
    body = command(shown["revision"])
    url = "/admin/v1/plugins/test.contract/actions/enable"
    first = await client.post(url, json=body)
    assert first.json()["data"]["operation"]["status"] == "unknown"
    assert manager.running_plugin_ids == ("test.contract",)
    stopped = await plugin_action(client, "disable")
    assert stopped["success"] and manager.running_plugin_ids == ()
    assert (await client.post(url, json=body)).json() == first.json()
    operation = first.json()["data"]["operation"]["operation_id"]
    assert (await client.get("/admin/v1/operations/" + operation)).json()["data"][
        "status"
    ] == "unknown"
    assert calls == ["test.contract"]


async def test_concurrent_management_requests_still_use_domain_revision(admin):
    client, _api, manager, *_ = admin
    await plugin_action(client, "approve", permissions=["tool.register"])
    shown = (await client.get("/admin/v1/plugins/test.contract")).json()["data"]
    url = "/admin/v1/plugins/test.contract/actions/enable"
    responses = await asyncio.gather(
        client.post(url, json=command(shown["revision"])),
        client.post(url, json=command(shown["revision"])),
    )
    assert sorted(item.status_code for item in responses) == [200, 409]
    rejected = next(item for item in responses if item.status_code == 409)
    assert rejected.json()["problem"]["code"] == "version_conflict"
    assert manager.running_plugin_ids == ("test.contract",)


async def test_unconfirmed_model_bundle_rollback_stays_unknown(admin, monkeypatch):
    client, api, _manager, _models, profiles = admin
    files = api.commands.model.files
    current = (await client.get("/admin/v1/models")).json()["data"]
    document = current["document"]
    document["profiles"]["main"]["api_key_env"] = "TEST_ROLLBACK_KEY"
    replace = files._replace
    restore = files._restore_file
    secrets = model_secrets_path(profiles.absolute())
    before = profiles.read_bytes()

    def fail_model_write(path, original, content):
        if path == profiles.absolute():
            raise ConfigFileError("operation_unavailable")
        return replace(path, original, content)

    def fail_secret_restore(path, original, content):
        if path == secrets:
            raise ConfigFileError("operation_unavailable")
        return restore(path, original, content)

    monkeypatch.setattr(files, "_replace", fail_model_write)
    monkeypatch.setattr(files, "_restore_file", fail_secret_restore)
    body = command(
        current["revision"], document=document, api_keys={"TEST_ROLLBACK_KEY": "synthetic-value"}
    )
    response = await client.post("/admin/v1/models/apply", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["operation"]["status"] == "unknown"
    assert profiles.read_bytes() == before
    assert secrets.exists()
    assert (await client.post("/admin/v1/models/apply", json=body)).json() == response.json()
