"""Path sends freeze once, and all delivery facts follow the actual transport plan."""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from tests.integration.test_native_social_delivery import prepare_paid_send
from tests.support.social_identity_cases import social_env
from tests.support.workspace_snapshots import snapshot_bytes
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_client_api import bearer
from tests.unit.test_client_files import app_context
from tests.unit.test_client_works import owner

from yuki.capabilities.results import normalize_legacy_result
from yuki.gateway.providers.feishu_social import FeishuSocialOperations
from yuki.gateway.social import TransportMessage, transport_payload
from yuki.runtime.effect_outcomes import execution_evidence
from yuki.sandbox.persistent import PersistentManager
from yuki.social.models import SocialError
from yuki.workspace.service import WorkspaceService

admin = admin_fixture
app_env = app_fixture


@pytest.mark.parametrize("caption_readable", [False, True])
def test_original_file_and_caption_evidence_survives_tool_rename(caption_readable):
    result = {
        "status": "succeeded",
        "file": {"status": "succeeded"},
        "caption": {"status": "succeeded"},
    }
    if caption_readable:
        result["caption"].update(delivered_text_available=True, delivered_text="original caption")
    else:
        result["file"]["artifact_id"] = "original-file"
    fact = execution_evidence(
        normalize_legacy_result(result, provider_id="core", tool_name="send_message"),
        tool="send_message",
        side_effecting=True,
        arguments=json.dumps(
            {"artifact_id": "original-file", "attachment_kind": "file", "text": "original caption"}
        ),
    )
    assert fact["delivered_artifacts"] == ["original-file"]
    assert fact["caption_delivered"] is True
    assert fact["delivered_message"] is True


async def test_original_prepared_direct_plan_is_not_rebuilt(database, tmp_path, monkeypatch):
    env = await social_env(database, tmp_path)
    args = {"text": "original one-part message"}
    target = await env.service.target("space", {}, env.context)
    route = await env.service.send_route(target, env.context)
    await env.service.receipts.prepare(
        source_turn_id=env.context.turn_id,
        tool_call_id=env.context.call_id,
        source_conversation_id=env.context.conversation_id,
        action="send_message",
        target=target,
        payload=args,
        prepared={
            "message": transport_payload(TransportMessage(args["text"])),
            "route": env.service._route_payload(route),
        },
    )

    def no_replan(self, message):
        raise AssertionError("an original physical plan cannot change with today's split settings")

    monkeypatch.setattr(type(env.router.operations(route)), "plan_send", no_replan)
    result = await env.service.execute("send", args, env.context)
    assert result["status"] == "succeeded"
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1


@pytest.mark.skipif(os.name != "posix", reason="Manager safe file FDs require POSIX")
@pytest.mark.parametrize("kind", ["file", "image"])
async def test_single_path_send_work_recovery_keeps_confirmed_delivery(database, tmp_path, kind):
    from yuki.runtime.effect_outcomes import current_result_capture
    from yuki.runtime.work_activation import current_work_control
    from yuki.runtime.work_journal import WorkJournal

    env = await social_env(database, tmp_path)
    manager = attach_manager(env, tmp_path)
    (manager.files.root / "result.txt").write_bytes(b"original result")
    args = {"path": "/workspace/result.txt", "attachment_kind": kind}
    control, capture, context = await prepare_paid_send(env, args)
    work_token = current_work_control.set(control)
    capture_token = current_result_capture.set(capture)
    try:
        result = await env.service.execute("send", args, context)
    finally:
        current_result_capture.reset(capture_token)
        current_work_control.reset(work_token)
    try:
        (manager.files.root / "result.txt").unlink()
        env.registry.disconnect(env.bot)
        recovered = await WorkJournal(control.repository).effect_result(context.call_id)
        fact = execution_evidence(
            normalize_legacy_result(recovered, provider_id="core", tool_name="send"),
            tool="send",
            side_effecting=True,
            arguments=json.dumps(args),
        )
        assert fact["status"] == "succeeded"
        assert fact["uncertain"] is False
        assert fact["delivered_artifacts"] == [result["artifact_id"]]
    finally:
        await control.repository.release(control.lease)
        await manager.close()


def attach_manager(env, tmp_path):
    manager = PersistentManager(
        tmp_path / "manager",
        env.store,
        "test-image",
        "test-network",
        "",
        tmp_path / "home",
        testing=True,
    )
    manager.files.root.mkdir(parents=True, exist_ok=True)

    async def execute(method, args, *, request_id):
        return await manager.file_request(method, args, request_id)

    workspace = WorkspaceService(env.store)
    workspace.sandbox = SimpleNamespace(execute=execute)
    env.service.workspace_service = workspace
    return manager


@pytest.mark.skipif(os.name != "posix", reason="Manager safe file FDs require POSIX")
@pytest.mark.parametrize("kind", ["file", "image"])
async def test_path_send_freezes_once_and_replays_after_source_deleted(
    database, tmp_path, monkeypatch, kind
):
    env = await social_env(database, tmp_path)
    manager = attach_manager(env, tmp_path)
    path = manager.files.root / "selected.txt"
    path.write_bytes(b"selected original bytes")
    args = {"path": "/workspace/selected.txt", "attachment_kind": kind}
    try:
        result = await env.service.execute("send", args, env.context)
        assert result["status"] == "succeeded"
        artifact = result["artifact_id"]
        assert env.store.read_bytes(artifact)[1] == b"selected original bytes"
        fact = execution_evidence(
            normalize_legacy_result(result, provider_id="core", tool_name="send"),
            tool="send",
            side_effecting=True,
        )
        assert fact["delivered_artifacts"] == [artifact]
        calls = len(env.bot.calls)
        path.unlink()
        env.registry.disconnect(env.bot)

        async def no_new_publication(**kwargs):
            raise AssertionError("original delivery must not reopen the file")

        monkeypatch.setattr(env.service.workspace_service, "publish_for_send", no_new_publication)
        assert await env.service.execute("send", args, env.context) == result
        assert len(env.bot.calls) == calls
    finally:
        await manager.close()


@pytest.mark.skipif(os.name != "posix", reason="Manager safe file FDs require POSIX")
async def test_restart_between_freeze_and_social_uses_original_snapshot(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    manager = attach_manager(env, tmp_path)
    source = manager.files.root / "report.txt"
    source.write_bytes(b"original report")
    args = {"path": "/workspace/report.txt", "attachment_kind": "file", "text": "report"}
    send = env.service._send_message_sequence

    async def crash_after_snapshot(*args, **kwargs):
        raise RuntimeError("host interrupted before social preparation")

    monkeypatch.setattr(env.service, "_send_message_sequence", crash_after_snapshot)
    try:
        with pytest.raises(RuntimeError, match="host interrupted"):
            await env.service.execute("send", args, env.context)
        original = env.store.list()["items"]
        assert len(original) == 1
        assert not any(action.startswith("upload_") for action, _ in env.bot.calls)
        source.unlink()
    finally:
        await manager.close()

    manager = attach_manager(env, tmp_path)
    monkeypatch.setattr(env.service, "_send_message_sequence", send)

    def no_reopen(*args, **kwargs):
        raise AssertionError("durable publish receipt must precede source access")

    monkeypatch.setattr(manager, "file_operation", no_reopen)
    try:
        result = await env.service.execute("send", args, env.context)
        assert result["status"] == "succeeded"
        assert result["artifact_id"] == original[0]["artifact_id"]
        assert env.store.read_bytes(result["artifact_id"])[1] == b"original report"
        with pytest.raises(SocialError, match="idempotency_conflict"):
            await env.service.execute("send", args | {"path": "/workspace/other.txt"}, env.context)
        assert sum(action == "upload_group_file" for action, _ in env.bot.calls) == 1
    finally:
        await manager.close()


@pytest.mark.skipif(os.name != "posix", reason="Manager safe file FDs require POSIX")
async def test_unconfirmed_publication_remains_unknown_without_platform_send(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    manager = attach_manager(env, tmp_path)
    source = manager.files.root / "report.txt"
    source.write_bytes(b"selected report")
    original = manager.file_operation

    def interrupted(method, values):
        original(method, values)
        raise asyncio.CancelledError("manager interrupted after snapshot publication")

    monkeypatch.setattr(manager, "file_operation", interrupted)
    args = {"path": "/workspace/report.txt", "attachment_kind": "file"}
    try:
        with pytest.raises(asyncio.CancelledError):
            await env.service.execute("send", args, env.context)
        source.unlink()

        def no_reopen(*args, **kwargs):
            raise AssertionError("an unknown original publication is not dispatched again")

        monkeypatch.setattr(manager, "file_operation", no_reopen)
        result = await env.service.execute("send", args, env.context)
        assert result["status"] == "uncertain" and result["uncertain"] is True
        assert result["error"] == "file_mutation_unknown_read_current_version"
        assert not any(action.startswith("upload_") for action, _ in env.bot.calls)
    finally:
        await manager.close()


async def test_feishu_text_first_plan_cannot_report_failed_file_as_delivered(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    artifact = snapshot_bytes(env.store, "report.txt", b"report")
    route = await env.service.send_route(
        await env.service.target("space", {}, env.context), env.context
    )
    operations = env.router.operations(route)
    monkeypatch.setattr(type(operations), "plan_send", FeishuSocialOperations.plan_send)
    original_call = env.bot.call_api

    class UploadRejected(RuntimeError):
        uncertain = False

    async def reject_upload(action, **params):
        if action == "upload_group_file":
            env.bot.calls.append((action, params))
            raise UploadRejected
        return await original_call(action, **params)

    monkeypatch.setattr(env.bot, "call_api", reject_upload)
    args = {"artifact_id": artifact["artifact_id"], "attachment_kind": "file", "text": "报告"}
    result = await env.service.execute("send", args, env.context)
    assert [part["status"] for part in result["parts"]] == ["succeeded", "failed"]
    assert "artifact_id" not in result["parts"][0]
    assert result["parts"][1]["artifact_id"] == artifact["artifact_id"]
    assert "file" not in result and "caption" not in result
    fact = execution_evidence(
        normalize_legacy_result(result, provider_id="core", tool_name="send"),
        tool="send",
        side_effecting=True,
    )
    assert fact["delivered_artifacts"] == []
    assert fact["caption_delivered"] is True
    before = len(env.bot.calls)
    assert await env.service.execute("send", args, env.context) == result
    assert len(env.bot.calls) == before


@pytest.mark.skipif(os.name != "posix", reason="Manager safe file FDs require POSIX")
async def test_app_path_send_exposes_exact_snapshot_to_original_person(app_env, database, tmp_path):
    env = app_env
    device, _ = await owner(env, database)
    context = await app_context(env, device)
    manager = attach_manager(SimpleNamespace(store=env.store, service=env.social), tmp_path)
    source = manager.files.root / "report.txt"
    source.write_bytes(b"selected report")
    try:
        result = await env.social.execute(
            "send",
            {"path": "/workspace/report.txt", "attachment_kind": "file", "text": "result"},
            context,
        )
        assert result["status"] == "succeeded"
        source.write_bytes(b"later user changes")
        downloaded = await env.client.get(
            f"/client/v1/artifacts/{result['artifact_id']}?version={result['version']}",
            headers=bearer(device["token"]),
        )
        assert downloaded.status_code == 200
        assert downloaded.content == b"selected report"
    finally:
        await manager.close()
