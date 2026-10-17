"""The official ACP client talks to the actual CLI process over stdio."""

import asyncio
import os
import socket
import sys
from contextlib import asynccontextmanager
from hashlib import sha256
from typing import cast
from uuid import uuid4

import httpx
import uvicorn
from acp import Client, spawn_agent_process
from acp.schema import ClientCapabilities, FileSystemCapabilities, ResourceContentBlock
from fastapi import FastAPI, Request, Response
from tests.integration.test_acp_runtime_integration import acp_runtime as acp_runtime
from tests.integration.test_acp_runtime_integration import settled
from tests.integration.test_client_continuation import live
from tests.unit.test_acp_adapter import Backend, Peer, text

from yuki.client_sdk import Outbox


@asynccontextmanager
async def backend_http(backend):
    app = FastAPI()

    @app.get("/client/v1/artifacts/{identity}")
    async def artifact(identity: str, version: str, request: Request):
        if request.headers.get("authorization") != "Bearer test-device-token":
            return Response(status_code=401)
        content = backend.artifacts.get(identity)
        if content is None or sha256(content).hexdigest() != version:
            return Response(status_code=404)
        return Response(content, media_type="application/octet-stream")

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def dispatch(request: Request):
        original = httpx.Request(
            request.method,
            str(request.url),
            headers=request.headers,
            content=await request.body(),
        )
        return {"data": await backend.handle(original)}

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
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


def process(peer, url, state, *, token="test-device-token"):
    return spawn_agent_process(
        cast(Client, peer),
        sys.executable,
        "-m",
        "yuki.cli",
        "acp",
        "--url",
        url,
        "--state-dir",
        str(state),
        env={
            **os.environ,
            "YUKI_CLIENT_TOKEN": token,
            "NO_PROXY": "localhost,127.0.0.1",
            "PYTHONPATH": os.pathsep.join(sys.path),
        },
    )


async def test_official_client_stdio_new_prompt_file_callback_and_full_load(tmp_path):
    backend, peer = Backend(), Peer()
    async with backend_http(backend) as url:
        async with process(peer, url, tmp_path) as (connection, child):
            async with asyncio.timeout(15):
                initialized = await connection.initialize(
                    protocol_version=1,
                    client_capabilities=ClientCapabilities(
                        fs=FileSystemCapabilities(read_text_file=True)
                    ),
                )
                assert initialized.protocol_version == 1
                session = await connection.new_session(cwd=str(tmp_path), mcp_servers=[])
                identity = str(uuid4())
                response = await connection.prompt(
                    session_id=session.session_id,
                    prompt=[
                        text("stdio question"),
                        ResourceContentBlock(
                            type="resource_link", uri="file:///client/notes.txt", name="notes"
                        ),
                    ],
                    **{"yuki.requestId": identity},
                )
                assert response.stop_reason == "end_turn"
                assert peer.reads == [(session.session_id, "/client/notes.txt")]
                assert (session.session_id, identity) in backend.requests
                assert peer.updates[-1][1].content.text == "confirmed outbound"
                assert "client-provided file" in backend.events[session.session_id][0]["text"]
                peer.updates.clear()
                for i in range(111):
                    backend.event(session.session_id, f"old {i}", "inbound")
                await connection.load_session(
                    cwd=str(tmp_path), session_id=session.session_id, mcp_servers=[]
                )
                assert len(peer.updates) == 113
                assert peer.updates[0][1].session_update == "user_message_chunk"
                assert peer.updates[1][1].session_update == "agent_message_chunk"
                assert peer.updates[-1][1].content.text == "old 110"
                assert child.returncode is None


async def test_official_stdio_cancel_is_exact_and_disconnect_is_not_cancel(tmp_path):
    backend, peer = Backend(), Peer()
    backend.auto_end = False
    session, identity = backend.session(), str(uuid4())
    async with backend_http(backend) as url:
        async with process(peer, url, tmp_path) as (connection, _):
            await connection.initialize(protocol_version=1)
            pending = asyncio.create_task(
                connection.prompt(
                    session_id=session,
                    prompt=[text("cancel this exact request")],
                    **{"yuki.requestId": identity},
                )
            )
            await asyncio.wait_for(backend.received.wait(), 10)
            output = backend.event(session, "sent just before cancel", "outbound")
            backend.requests[session, identity]["result_event_ids"].append(output)
            await connection.cancel(session_id=session)
            assert (await asyncio.wait_for(pending, 5)).stop_reason == "cancelled"
            assert backend.cancels == [(session, identity)]
            assert peer.updates[-1][1].content.text == "sent just before cancel"
            backend.received.clear()
            next_id = str(uuid4())
            disconnected = asyncio.create_task(
                connection.prompt(
                    session_id=session,
                    prompt=[text("disconnect retains server execution")],
                    **{"yuki.requestId": next_id},
                )
            )
            await asyncio.wait_for(backend.received.wait(), 5)
        await asyncio.gather(disconnected, return_exceptions=True)
        assert backend.requests[session, next_id]["turn"]["state"] == "running"
        assert backend.cancels == [(session, identity)]


async def test_official_stdio_attachment_only_live_and_load_retain_downloadable_artifact(tmp_path):
    backend, peer = Backend(), Peer()
    backend.auto_end = False
    session, identity, artifact = backend.session(), str(uuid4()), str(uuid4())
    content = b"real immutable file bytes\n"
    version = sha256(content).hexdigest()
    backend.artifacts = {artifact: content}
    async with backend_http(backend) as url:
        async with process(peer, url, tmp_path) as (connection, _):
            await connection.initialize(protocol_version=1)
            pending = asyncio.create_task(
                connection.prompt(
                    session_id=session,
                    prompt=[text("deliver a file")],
                    **{"yuki.requestId": identity},
                )
            )
            await asyncio.wait_for(backend.received.wait(), 10)
            output = backend.event(session, "", "outbound")
            backend.events[session][-1]["attachments"] = [
                {
                    "kind": "file",
                    "artifact_id": artifact,
                    "version": version,
                    "filename": "delivered.txt",
                    "mime_type": "text/plain",
                    "size": len(content),
                }
            ]
            backend.requests[session, identity]["result_event_ids"] = [output]
            backend.finish(session, identity)
            assert (await asyncio.wait_for(pending, 5)).stop_reason == "end_turn"
            resource = peer.updates[0][1].content
            assert isinstance(resource, ResourceContentBlock)
            assert resource.name == "delivered.txt" and resource.size == len(content)
            assert "test-device-token" not in resource.model_dump_json()
            async with httpx.AsyncClient(trust_env=False) as client:
                assert (await client.get(resource.uri)).status_code == 401
                downloaded = await client.get(
                    resource.uri, headers={"Authorization": "Bearer test-device-token"}
                )
                assert downloaded.status_code == 200 and downloaded.content == content
            peer.updates.clear()
            await connection.load_session(cwd=str(tmp_path), session_id=session, mcp_servers=[])
            assert peer.updates[-1][1].content == resource


async def test_official_stdio_process_restart_recovers_original_uuid_after_lost_ack(
    acp_runtime, tmp_path
):
    env = acp_runtime
    blocked_ack, release_ack = asyncio.Event(), asyncio.Event()
    posts = 0

    async def service(scope, receive, send):
        nonlocal posts
        if (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and scope["path"].endswith("/messages")
        ):
            posts += 1

            async def delayed_ack(message):
                if message["type"] == "http.response.start":
                    blocked_ack.set()
                    await release_ack.wait()
                await send(message)

            await env.http_app(scope, receive, delayed_ack)
        else:
            await env.http_app(scope, receive, send)

    state, identity = tmp_path / "real-stdio-state", str(uuid4())
    body = "Keep the original paid request across the ACP process crash"
    async with live(service) as url:
        try:
            async with process(Peer(), url, state, token=env.device["token"]) as (first, child):
                await first.initialize(protocol_version=1)
                session = await first.new_session(cwd=str(tmp_path), mcp_servers=[])
                pending = asyncio.create_task(
                    first.prompt(
                        session_id=session.session_id,
                        prompt=[text(body)],
                        **{"yuki.requestId": identity},
                    )
                )
                await asyncio.wait_for(blocked_ack.wait(), 10)
                original = await settled(env, {"session_id": session.session_id}, identity)
                assert original["result_event_ids"]
                outbox = Outbox(
                    state / "conversations" / f"{session.session_id.replace('-', '')}.sqlite3"
                )
                assert outbox.get(identity)["state"] == "submitted"
                model_calls = len(env.provider.requests)
                assert model_calls == 2
                child.kill()
                await asyncio.wait_for(child.wait(), 5)
                await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 5)
                release_ack.set()
            peer = Peer()
            async with process(peer, url, state, token=env.device["token"]) as (restored, _):
                await restored.initialize(protocol_version=1)
                await restored.load_session(
                    cwd=str(tmp_path), session_id=session.session_id, mcp_servers=[]
                )
                assert [update.content.text for _, update in peer.updates] == [
                    body,
                    "Confirmed ACP reply",
                ]
                response = await restored.prompt(
                    session_id=session.session_id,
                    prompt=[text(body)],
                    **{"yuki.requestId": identity},
                )
                assert response.stop_reason == "end_turn"
                assert posts == 1 and len(env.provider.requests) == model_calls
                assert outbox.get(identity)["result"]["event_id"] == original["event_id"]
                assert (
                    outbox.get(identity)["result"]["result_event_ids"]
                    == original["result_event_ids"]
                )
        finally:
            release_ack.set()
