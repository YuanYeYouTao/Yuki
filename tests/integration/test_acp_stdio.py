"""The official ACP client talks to the actual CLI process over stdio."""

import asyncio
import os
import socket
import sys
from contextlib import asynccontextmanager
from typing import cast
from uuid import uuid4

import httpx
import uvicorn
from acp import Client, spawn_agent_process
from acp.schema import ClientCapabilities, FileSystemCapabilities, ResourceContentBlock
from fastapi import FastAPI, Request
from tests.unit.test_acp_adapter import Backend, Peer, text


@asynccontextmanager
async def backend_http(backend):
    app = FastAPI()

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


def process(peer, url, state):
    return spawn_agent_process(
        cast(Client, peer),
        sys.executable,
        "-m",
        "qq_ai_bot.cli",
        "acp",
        "--url",
        url,
        "--state-dir",
        str(state),
        env={
            **os.environ,
            "YUKI_CLIENT_TOKEN": "test-device-token",
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
