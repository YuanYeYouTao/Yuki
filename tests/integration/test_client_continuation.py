"""Real loopback HTTP, separate CLI processes, and cold original Work continuation."""

import asyncio
import json
import os
import socket
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import select
from tests.integration.test_current_material_main_contract import _catalog, _deployment, _profile
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_app_messages import drain
from tests.unit.test_client_api import bearer, pair
from tests.unit.test_client_works import owner

from yuki.client_api.http import attach_client_api
from yuki.client_sdk import Outbox, PersonalClient
from yuki.container import ApplicationContainer
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.identity.canonical_repository import create_presence, find_identity_binding
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.pool import ModelClientPool
from yuki.persistence.models import ChatEventModel
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import effects, inputs, journal, work

admin = admin_fixture
app_env = app_fixture


@asynccontextmanager
async def live(app, port=0):
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen()
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="critical", lifespan="off", timeout_graceful_shutdown=1)
    )
    running = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if running.done():
                    running.result()
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(running, 5)
        sock.close()


async def cli(url, state, token, *args):
    environment = {
        **os.environ,
        "YUKI_CLIENT_TOKEN": token,
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "yuki.cli",
        "client",
        "--url",
        url,
        "--state",
        str(state),
        *args,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, error = await asyncio.wait_for(process.communicate(), 30)
    assert process.returncode == 0, error.decode() + output.decode()
    return json.loads(output)


async def test_two_external_cli_devices_share_original_message_and_persistent_outbox(
    app_env, database, tmp_path
):
    env = app_env
    device, _ = await owner(env, database)
    code = await env.identity.issue_for_device(
        await env.identity.authenticate(device["token"]), str(uuid4())
    )
    second = await pair(env.client, code["code"])
    async with live(env.app) as url:
        identity = str(uuid4())
        draft = await cli(
            url,
            tmp_path / "a.sqlite",
            device["token"],
            "send",
            "external original",
            "--request-id",
            identity,
            "--draft",
        )
        assert draft["state"] == "draft" and not env.calls
        accepted = await cli(url, tmp_path / "a.sqlite", device["token"], "retry", identity)
        assert accepted["state"] == "accepted"
        await drain(env)
        replay = await cli(
            url,
            tmp_path / "b.sqlite",
            second["token"],
            "send",
            "external original",
            "--request-id",
            identity,
        )
        assert (
            replay["result"]["event_id"] == accepted["result"]["event_id"]
            and replay["result"]["replay"]
        )
        original = await cli(url, tmp_path / "a.sqlite", device["token"], "retry", identity)
        assert original["state"] == "result" and original["result"]["result_event_ids"]
        home = await cli(url, tmp_path / "b.sqlite", second["token"], "home")
        messages = await cli(
            url, tmp_path / "b.sqlite", second["token"], "events", home["main"]["conversation_id"]
        )
        assert [item["text"] for item in messages["items"]] == ["external original", "App reply"]
        assert len(env.calls) == 2

        from tests.support.workspace_snapshots import snapshot_bytes

        artifact = snapshot_bytes(env.store, "original.txt", b"original exported version")

        def return_file(request):
            if request.messages[-1].role == "tool":
                return "file delivered"
            return response(
                "send_message",
                {
                    "artifact_id": artifact["artifact_id"],
                    "attachment_kind": "file",
                    "text": "original file",
                },
                "file",
            )

        env.provider._responder = return_file
        input_file = tmp_path / "input.txt"
        input_file.write_bytes(b"original document sent by the reference client")
        uploaded = await cli(url, tmp_path / "b.sqlite", second["token"], "upload", str(input_file))
        await cli(
            url,
            tmp_path / "b.sqlite",
            second["token"],
            "send",
            "read and return the file",
            "--attachment",
            uploaded["result"]["media_id"],
        )
        await drain(env)
        snapshot_bytes(env.store, "original.txt", b"later replacement version")
        output = tmp_path / "received.txt"
        await cli(
            url,
            tmp_path / "a.sqlite",
            device["token"],
            "artifact",
            artifact["artifact_id"],
            "--version",
            artifact["sha256"],
            "--output",
            str(output),
        )
        assert output.read_bytes() == b"original exported version"
        await cli(url, tmp_path / "b.sqlite", second["token"], "revoke", device["device_id"])
        async with httpx.AsyncClient() as check:
            assert (
                await check.get(url + "/client/v1/me", headers=bearer(device["token"]))
            ).status_code == 401
            assert (
                await check.get(url + "/client/v1/me", headers=bearer(second["token"]))
            ).status_code == 200


async def test_real_sse_reconnects_after_last_hint_lost_and_refreshes_every_entry(
    app_env, database, tmp_path, monkeypatch
):
    env = app_env
    device, _ = await owner(env, database)
    subscribe = database.commit_hints.subscribe
    registrations = []

    def counted():
        queue = subscribe()
        registrations.append(queue)
        return queue

    monkeypatch.setattr(database.commit_hints, "subscribe", counted)
    async with live(env.app) as url:
        client = PersonalClient(url, device["token"], Outbox(tmp_path / "watch.sqlite"))
        watch = client.watch(reconnect_delay=0.01)
        try:
            first = await asyncio.wait_for(anext(watch), 5)
            assert first["main"]
            request = await client.prepare_message("final result before reconnect")
            await client.submit(request)
            await drain(env)
            # No awaits: the producer overruns this subscription before it can drain.
            for _ in range(70):
                database.commit_hints.publish()
            assert not database.commit_hints.subscribers
            async with asyncio.timeout(10):
                while len(registrations) < 2:
                    await anext(watch)
            texts = [item["text"] for item in client.events[first["main"]["conversation_id"]]]
            assert texts == ["final result before reconnect", "App reply"]
            assert client.outbox.get(request)["state"] == "result" and len(env.calls) == 2
        finally:
            await watch.aclose()
            await client.close()


def response(name, arguments, call_id):
    return ChatResponse(
        content="",
        latency_seconds=0,
        tool_calls=(ToolCall(call_id, ToolFunction(name, json.dumps(arguments))),),
    )


@pytest.mark.parametrize("entry", ("group", "qq", "app"))
async def test_original_work_continues_across_devices_tools_and_two_cold_runtimes(
    database, tmp_path, entry
):
    from tests.support.workspace_snapshots import snapshot_bytes

    initial = iter(
        (
            response(
                "task_control", {"action": "accept", "goal": "continue original work"}, "accept"
            ),
            response("send_message", {"text": "which city?"}, "question"),
            response("task_control", {"action": "need_input", "reason": "city missing"}, "waiting"),
        )
    )
    app, qq, message, sender = await _deployment(
        database,
        tmp_path,
        FakeLLMProvider(lambda _: next(initial)),
        social_gateway_transfer_directory="/transfer",
    )
    async with database.immediate_session() as session:
        await create_presence(
            session, external_account_id="yuki-app", platform="app", now=datetime.now(UTC)
        )
    await app.app_gateway.start()
    repository = WorkRepository(database)
    try:
        async with database.sessions() as session:
            binding = await find_identity_binding(session, "10001")
        issued = await app.client_api.identity.issue_for_operator(
            binding.person_id, str(uuid4()), str(uuid4())
        )
        http_app = FastAPI()
        attach_client_api(http_app, lambda: app.client_api)
        async with live(http_app) as url:
            async with httpx.AsyncClient() as http:
                desktop_device = PersonalClient.data(
                    await http.post(
                        url + "/client/v1/pair",
                        json={"code": issued["code"], "device_label": "desktop"},
                    )
                )
            desktop = PersonalClient(
                url, desktop_device["token"], Outbox(tmp_path / "desktop.sqlite")
            )
            try:
                if entry == "app":
                    initial_request = await desktop.prepare_message(
                        "continue the original task on another device"
                    )
                    await desktop.submit(initial_request)
                    await asyncio.gather(*tuple(app.app_gateway.tasks))
                else:
                    if entry == "qq":
                        message = replace(
                            message,
                            event_type="message:private:friend",
                            scope_type=ScopeType.PRIVATE,
                            group_id=None,
                            mentions_bot=False,
                        )
                    await app.processor.handle(message, sender)
                async with database.sessions() as session:
                    original = dict(
                        (await session.execute(select(work).where(work.c.state == "waiting_user")))
                        .mappings()
                        .one()
                    )
                    original_journal = (
                        await session.execute(
                            select(journal.c.payload_json).where(
                                journal.c.work_id == original["id"]
                            )
                        )
                    ).scalar_one()
                code = await app.client_api.identity.issue_for_device(
                    await app.client_api.identity.authenticate(desktop_device["token"]),
                    str(uuid4()),
                )
                async with httpx.AsyncClient() as http:
                    device = PersonalClient.data(
                        await http.post(
                            url + "/client/v1/pair",
                            json={"code": code["code"], "device_label": "phone"},
                        )
                    )
            finally:
                await desktop.close()
            phone = PersonalClient(url, device["token"], Outbox(tmp_path / "phone.sqlite"))
            try:
                snapshot = await phone.refresh()
                card = next(item for item in snapshot["works"] if item["work_id"] == original["id"])
                assert card["waiting_question"]["text"] == "which city?"
                assert card["result_delivery"]["conversation_id"] == original["conversation_id"]
                request = await phone.prepare_input(
                    original["id"], "Use Taipei, preserve the original work"
                )
                accepted = await phone.submit(request)
                queued = await repository.get(original["id"])
                assert queued["state"] == "queued" and accepted["state"] == "accepted"
                for key in (
                    "source_json",
                    "source_key",
                    "conversation_id",
                    "generation",
                    "model_requests",
                    "tool_calls",
                ):
                    assert queued[key] == original[key]
                async with database.sessions() as session:
                    assert (
                        await session.execute(
                            select(journal.c.payload_json).where(
                                journal.c.work_id == original["id"]
                            )
                        )
                    ).scalar_one() == original_journal
                artifact = await asyncio.to_thread(
                    snapshot_bytes,
                    app.workspace_service.store,
                    "original-result.txt",
                    b"original Work file bytes",
                )
            finally:
                await phone.close()
        settings = app.settings
        assert not await app.lifecycle._close_entries(app.lifecycle._entries)
        observed = []

        def read_after_input(request):
            observed.append(request)
            return response(
                "find_contacts",
                {"kind": "person", "subject_ref": "current_speaker"},
                "after-input-read",
            )

        # One existing per-segment request slot yields naturally after this tool.
        # The original root budget and counters remain in the original tables.
        app = ApplicationContainer(
            settings.model_copy(update={"agent_max_model_requests": 1}), database=database
        )
        app.models.apply_catalog(
            _catalog(_profile()),
            ModelClientPool(injected_profiles={"main": FakeLLMProvider(read_after_input)}),
        )
        app.gateway_registry.connect(qq.bot, provider_id="snowluma", presence_id=qq.presence)
        await app.model_invocations.writer.start()
        await app.app_gateway.start()
        await app.work_resumer.resume(await repository.get(original["id"]))
        continued = await repository.get(original["id"])
        assert (
            continued["state"] == "queued"
            and continued["model_requests"] == original["model_requests"] + 1
        )
        async with database.sessions() as session:
            consumed = (
                (
                    await session.execute(
                        select(inputs).where(inputs.c.id == accepted["result"]["input_id"])
                    )
                )
                .mappings()
                .one()
            )
            assert consumed["state"] == "consumed"
        assert not await app.lifecycle._close_entries(app.lifecycle._entries)
        final_calls = iter(
            (
                response(
                    "send_message",
                    {
                        "artifact_id": artifact["artifact_id"],
                        "attachment_kind": "file",
                        "text": "Taipei result from original Work",
                    },
                    "result",
                ),
                response(
                    "task_control",
                    {"action": "complete", "result": "original task finished"},
                    "complete",
                ),
            )
        )

        def finish_after_restart(request):
            observed.append(request)
            return next(final_calls)

        app = ApplicationContainer(settings, database=database)
        app.models.apply_catalog(
            _catalog(_profile()),
            ModelClientPool(injected_profiles={"main": FakeLLMProvider(finish_after_restart)}),
        )
        app.gateway_registry.connect(qq.bot, provider_id="snowluma", presence_id=qq.presence)
        await app.model_invocations.writer.start()
        await app.app_gateway.start()
        await app.work_resumer.resume(await repository.get(original["id"]))
        final = await repository.get(original["id"])
        assert final["state"] == "completed" and final["id"] == original["id"]
        assert (
            final["source_json"] == original["source_json"]
            and final["generation"] == original["generation"]
        )
        assert (
            final["model_requests"] == original["model_requests"] + 3
            and final["tool_calls"] > original["tool_calls"]
        )
        assert any(
            "Use Taipei" in (item.content or "") for call in observed for item in call.messages
        )
        assert all(
            ("read_personal_entry" in {tool.name for tool in call.tools}) == (entry == "app")
            for call in observed
        )
        async with database.sessions() as session:
            expected_manifest = (
                app.main_agent_contract.app_revision
                if entry == "app"
                else app.main_agent_contract.revision
            )
            saved_calls = [
                json.loads(body)["invocation"]
                for body in await session.scalars(
                    select(effects.c.receipt_json).where(effects.c.work_id == original["id"])
                )
            ]
            assert {call["provider_call_id"] for call in saved_calls} == {
                "accept",
                "question",
                "waiting",
                "after-input-read",
                "result",
                "complete",
            }
            assert {call["manifest_revision"] for call in saved_calls} == {expected_manifest}
            sent = (
                await session.scalars(
                    select(ChatEventModel).where(
                        ChatEventModel.content == "Taipei result from original Work"
                    )
                )
            ).one()
            assert sent.canonical_conversation_id == original["conversation_id"]
            app_input = await session.get(ChatEventModel, accepted["result"]["event_id"])
            assert (
                app_input.origin == "work_input" and app_input.author_person_id == binding.person_id
            )
            assert (app_input.canonical_conversation_id == original["conversation_id"]) == (
                entry == "app"
            )
        await asyncio.to_thread(
            snapshot_bytes,
            app.workspace_service.store,
            "original-result.txt",
            b"later replacement bytes",
        )
        restored_http = FastAPI()
        attach_client_api(restored_http, lambda: app.client_api)
        async with live(restored_http, port=int(url.rsplit(":", 1)[1])) as restored_url:
            phone = PersonalClient(restored_url, device["token"], Outbox(tmp_path / "phone.sqlite"))
            desktop = PersonalClient(
                restored_url, desktop_device["token"], Outbox(tmp_path / "desktop.sqlite")
            )
            try:
                assert (await phone.submit(request))["state"] == "result"
                home = await desktop.refresh()
                same = next(item for item in home["works"] if item["work_id"] == original["id"])
                assert (
                    same["state"] == "completed"
                    and same["budget"]["models"] >= original["model_requests"]
                )
                assert any(
                    ref["artifact_id"] == artifact["artifact_id"]
                    and ref["version"] == artifact["sha256"]
                    for event in same["results"]
                    for ref in event["artifacts"]
                )
                result = await phone.http.get(
                    phone.endpoint(f"artifacts/{artifact['artifact_id']}"),
                    headers=phone.headers,
                    params={"version": artifact["sha256"]},
                )
                assert result.status_code == 200 and result.content == b"original Work file bytes"
            finally:
                await phone.close()
                await desktop.close()
    finally:
        assert not await app.lifecycle._close_entries(app.lifecycle._entries)


async def test_actual_server_process_restart_recovers_committed_result_without_hint(
    app_env, database, tmp_path
):
    from pathlib import Path

    env = app_env
    device, _ = await owner(env, database)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    processes = []
    logs = []

    async def start(mode):
        log = (tmp_path / f"server-{len(processes)}.log").open("wb")
        logs.append(log)
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.support.client_reference_server",
            str(tmp_path),
            database.url,
            str(port),
            mode,
            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
        )
        processes.append(child)
        async with httpx.AsyncClient() as probe, asyncio.timeout(20):
            while True:
                if child.returncode is not None:
                    failure = await asyncio.to_thread(Path(log.name).read_text)
                    raise AssertionError(failure[-4000:])
                try:
                    response = await probe.get(url + "/healthz")
                    if response.status_code == 200:
                        return child
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)

    client = PersonalClient(url, device["token"], Outbox(tmp_path / "restart.sqlite"))
    watch = client.watch(reconnect_delay=0.01)
    try:
        original = await start("suppress")
        initial = await asyncio.wait_for(anext(watch), 5)
        request = await client.prepare_message("survive the server process restart")
        await client.submit(request)
        async with asyncio.timeout(10):
            while True:
                result = await client.get(f"requests/{request}")
                home = await client.get("home")
                if (
                    result["result_event_ids"]
                    and home["works"]
                    and all(item["state"] == "completed" for item in home["works"])
                ):
                    break
                await asyncio.sleep(0.02)
        original.terminate()
        await asyncio.wait_for(original.wait(), 10)
        await start("normal")
        refreshed = await asyncio.wait_for(anext(watch), 10)
        assert refreshed["main"]["conversation_id"] == initial["main"]["conversation_id"]
        assert [item["text"] for item in client.events[refreshed["main"]["conversation_id"]]] == [
            "survive the server process restart",
            "persistent reply",
        ]
        assert (await client.get(f"requests/{request}"))["result_event_ids"] == result[
            "result_event_ids"
        ]
        assert {item["work_id"] for item in refreshed["works"]} == {
            item["work_id"] for item in home["works"]
        }
        assert client.outbox.get(request)["state"] == "result"
    finally:
        await watch.aclose()
        await client.close()
        for child in processes:
            if child.returncode is None:
                child.terminate()
                await asyncio.wait_for(child.wait(), 10)
        for log in logs:
            log.close()
