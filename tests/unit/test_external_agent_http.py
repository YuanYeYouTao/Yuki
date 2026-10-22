"""Purpose-bound connector credentials and credential-safe public validation."""

import asyncio
from uuid import uuid4

import httpx
from fastapi import FastAPI
from tests.support.client_identity import client_identity_service

from yuki.external_agents.http import attach_external_agent_api
from yuki.external_agents.service import ExternalAgentService
from yuki.identity.canonical_repository import find_identity_binding
from yuki.sandbox.task_repository import SandboxTaskRepository


async def test_pair_validation_does_not_echo_code_or_unexpected_secret(database):
    service = ExternalAgentService(database, SandboxTaskRepository(database))
    app = FastAPI()
    attach_external_agent_api(app, lambda: service)
    raw_code = "private-one-time-computer-pair-code"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://test"
    ) as client:
        missing = await client.post("/agents/v1/pair", json={"code": raw_code})
        assert missing.status_code == 422
        assert raw_code not in missing.text
        assert missing.json() == {"problem": {"code": "validation_error"}}
        extra = await client.post(
            "/agents/v1/pair",
            json={
                "code": raw_code,
                "device_label": "computer",
                "token": "unexpected-private-token",
            },
        )
        assert extra.status_code == 422
        assert raw_code not in extra.text and "unexpected-private-token" not in extra.text


async def test_chat_token_cannot_authenticate_connector_websocket(database):
    chat = client_identity_service(database)
    async with database.read_session() as session:
        person = (await find_identity_binding(session, "1001")).person_id
    code = await chat.issue_for_operator(person, str(uuid4()), str(uuid4()))
    device = await chat.pair(code["code"], "chat only")
    service = ExternalAgentService(database, SandboxTaskRepository(database))
    app = FastAPI()
    attach_external_agent_api(app, lambda: service)
    incoming = asyncio.Queue()
    outgoing = []
    incoming.put_nowait({"type": "websocket.connect"})

    async def receive():
        return await incoming.get()

    async def send(message):
        outgoing.append(message)

    await app(
        {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "wss",
            "path": "/agents/v1/connect",
            "raw_path": b"/agents/v1/connect",
            "query_string": b"",
            "root_path": "",
            "subprotocols": [],
            "headers": [(b"authorization", f"Bearer {device['token']}".encode())],
            "client": ("127.0.0.1", 10001),
            "server": ("test", 443),
        },
        receive,
        send,
    )
    assert outgoing == [{"type": "websocket.close", "code": 4401, "reason": ""}]
    assert not service.connections.online(device["device_id"])
