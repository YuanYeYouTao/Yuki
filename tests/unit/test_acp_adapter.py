"""ACP behavior against the public personal HTTP contract, without model calls."""

import argparse
import asyncio
import hashlib
import json
import os
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest
from acp import RequestError
from acp.schema import (
    ClientCapabilities,
    EmbeddedResourceContentBlock,
    FileSystemCapabilities,
    ImageContentBlock,
    ReadTextFileResponse,
    ResourceContentBlock,
    TextContentBlock,
    TextResourceContents,
)

from qq_ai_bot.acp_adapter.agent import YukiAgent


def digest(operation, target, body):
    return hashlib.sha256(
        json.dumps([operation, target, body], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class Backend:
    """A controllable server projection; no adapter implementation is mocked."""

    def __init__(self):
        self.base_binding = str(uuid4())
        self.base_conversation = str(uuid4())
        self.acp_binding = str(uuid4())
        self.person = str(uuid4())
        self.sessions = {}
        self.requests = {}
        self.events = {}
        self.posts = []
        self.cancels = []
        self.received = asyncio.Event()
        self.auto_end = True
        self.lose_ack = False
        self.next_event = 1

    def session(self):
        identity = str(uuid4())
        self.sessions[identity] = {
            "session_id": identity,
            "conversation_id": identity,
            "binding_id": self.acp_binding,
            "platform": "acp",
            "generation": 1,
            "replay": False,
        }
        self.events[identity] = []
        return identity

    def event(self, session, text, direction):
        event = {
            "event_id": self.next_event,
            "conversation_id": session,
            "direction": direction,
            "text": text,
        }
        self.next_event += 1
        self.events[session].append(event)
        return event["event_id"]

    def finish(self, session, request_id, *, outcome="handled", work=None):
        row = self.requests[session, request_id]
        row["turn"] = {"state": "ended", "outcome": outcome}
        row["work"] = work

    async def handle(self, request):
        assert request.headers["authorization"] == "Bearer test-device-token"
        path = request.url.path.removeprefix("/client/v1/")
        session = request.headers.get("x-yuki-conversation")
        data = json.loads(request.content) if request.content else {}
        if path == "me":
            return {
                "request_person_id": self.person,
                "person_id": self.person,
                "conversation_id": session or self.base_conversation,
                "binding_id": self.acp_binding if session else self.base_binding,
                "bindings": [
                    {
                        "binding_id": self.base_binding,
                        "platform": "app",
                        "source_person_id": self.person,
                    }
                ],
            }
        if path == "acp/sessions" and request.method == "POST":
            self.posts.append((None, data["request_id"], path))
            created = self.session()
            result = self.sessions[created]
            self.requests[None, data["request_id"]] = {
                "found": True,
                "operation_kind": "client.acp_session_new",
                "target": self.base_binding,
                "payload_hash": digest("client.acp_session_new", self.base_binding, {}),
                "result": result,
            }
            return result
        if path.startswith("acp/sessions/"):
            parts = path.split("/")
            if len(parts) == 3:
                return self.sessions[parts[2]]
            assert parts[3] == "requests" and parts[5] == "cancel"
            request_id = parts[4]
            assert session == parts[2]
            self.cancels.append((session, request_id))
            self.finish(session, request_id, outcome="cancelled")
            return {**self.requests[session, request_id], "cancelled": True}
        if path == "messages" and request.method == "POST":
            assert session in self.sessions
            assert request.headers["x-yuki-entry"] == self.acp_binding
            identity = data["request_id"]
            self.posts.append((session, identity, path))
            event_id = self.event(session, data["text"], "inbound")
            result = {
                "found": True,
                "operation_kind": "message",
                "target": session,
                "payload_hash": digest(
                    "message", session, {k: v for k, v in data.items() if k != "request_id"}
                ),
                "event_id": event_id,
                "conversation_id": session,
                "result_event_ids": [],
                "turn": {"state": "running", "outcome": None},
                "work": None,
            }
            self.requests[session, identity] = result
            if self.auto_end:
                output = self.event(session, "confirmed outbound", "outbound")
                result["result_event_ids"].append(output)
                self.finish(session, identity)
            self.received.set()
            if self.lose_ack:
                self.lose_ack = False
                raise httpx.ReadError("lost acknowledgement", request=request)
            return result
        if path.startswith("requests/"):
            return deepcopy(self.requests.get((session, path.split("/")[1]), {"found": False}))
        if path.startswith("conversations/"):
            assert session == path.split("/")[1]
            after = int(request.url.params.get("after", "0"))
            events = [e for e in self.events[session] if e["event_id"] > after]
            page = events[:50]
            return {
                "items": page,
                "has_more": len(events) > len(page),
                "next_after": page[-1]["event_id"] if page else after,
            }
        raise AssertionError((request.method, path))

    async def transport(self, request):
        return httpx.Response(200, json={"data": await self.handle(request)})


class Peer:
    def __init__(self):
        self.updates = []
        self.reads = []
        self.updated = asyncio.Event()

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append((session_id, update))
        self.updated.set()

    async def read_text_file(self, session_id, path, **kwargs):
        self.reads.append((session_id, path))
        return ReadTextFileResponse(content="client-provided file")


@pytest.fixture
async def adapter(tmp_path):
    backend, peer = Backend(), Peer()
    async with httpx.AsyncClient(transport=httpx.MockTransport(backend.transport)) as http:
        agent = YukiAgent("http://yuki", "test-device-token", tmp_path, http=http)
        agent.on_connect(peer)
        yield agent, backend, peer
        await agent.close()


def text(value="same input"):
    return TextContentBlock(type="text", text=value)


async def test_capabilities_and_mcp_rejection_are_honest(adapter):
    agent, backend, _ = adapter
    initialized = await agent.initialize(99)
    assert initialized.protocol_version == 1
    capabilities = initialized.agent_capabilities.model_dump(exclude_none=True)
    assert capabilities["load_session"] is True
    assert capabilities["prompt_capabilities"] == {
        "audio": False,
        "embedded_context": True,
        "image": False,
    }
    assert not capabilities.get("session_capabilities", {}).get("resume")
    with pytest.raises(RequestError, match="Invalid params"):
        await agent.new_session("/client/only", mcp_servers=[{"name": "no"}])
    assert not backend.posts


async def test_new_session_is_server_uuid_and_load_replays_all_pages(adapter):
    agent, backend, peer = adapter
    new = await agent.new_session("/not/a/server/workdir")
    assert new.session_id in backend.sessions
    for i in range(123):
        backend.event(new.session_id, f"history {i}", "inbound" if i % 2 == 0 else "outbound")
    await agent.load_session("/different/client/workdir", new.session_id)
    assert [u.content.text for _, u in peer.updates] == [f"history {i}" for i in range(123)]
    assert [u.session_update for _, u in peer.updates[:2]] == [
        "user_message_chunk",
        "agent_message_chunk",
    ]


async def test_same_text_is_new_intent_and_explicit_uuid_remains_scoped(adapter):
    agent, backend, _ = adapter
    first, second = backend.session(), backend.session()
    await agent.prompt(first, [text()])
    await agent.prompt(first, [text()])
    assert backend.posts[0][1] != backend.posts[1][1]
    identity = str(uuid4())
    for session in (first, second):
        await agent.prompt(session, [text()], **{"yuki.requestId": identity})
    assert (first, identity) in backend.requests and (second, identity) in backend.requests


async def test_lost_ack_and_reopen_query_original_uuid_without_second_post(adapter, tmp_path):
    agent, backend, peer = adapter
    session, identity = backend.session(), str(uuid4())
    backend.lose_ack = True
    response = await agent.prompt(session, [text()], **{"yuki.requestId": identity})
    assert response.stop_reason == "end_turn"
    reopened = YukiAgent("http://yuki", "test-device-token", tmp_path, http=agent._http)
    reopened.on_connect(peer)
    await reopened.prompt(session, [text()], **{"yuki.requestId": identity})
    assert backend.posts == [(session, identity, "messages")]


async def test_ack_and_successful_output_do_not_finish_running_turn(adapter):
    agent, backend, peer = adapter
    backend.auto_end = False
    session, identity = backend.session(), str(uuid4())
    running = asyncio.create_task(agent.prompt(session, [text()], **{"yuki.requestId": identity}))
    await backend.received.wait()
    output = backend.event(session, "real send while running", "outbound")
    backend.requests[session, identity]["result_event_ids"].append(output)
    await peer.updated.wait()
    assert not running.done()
    backend.finish(session, identity)
    assert (await asyncio.wait_for(running, 3)).stop_reason == "end_turn"
    assert [u.content.text for _, u in peer.updates] == ["real send while running"]


@pytest.mark.parametrize("work_state", ["queued", "running", "waiting_external"])
async def test_foreground_completion_waits_original_work(adapter, work_state):
    agent, backend, _ = adapter
    backend.auto_end = False
    session, identity = backend.session(), str(uuid4())
    running = asyncio.create_task(agent.prompt(session, [text()], **{"yuki.requestId": identity}))
    await backend.received.wait()
    work = {"work_id": "original-work", "state": work_state, "revision": 1, "reason": None}
    backend.finish(session, identity, work=work)
    await asyncio.sleep(0.02)
    assert not running.done()
    work["state"] = "waiting_user"
    assert (await asyncio.wait_for(running, 3)).stop_reason == "end_turn"


@pytest.mark.parametrize(
    "outcome,work_state", [("llm_failure", None), ("handled", "failed"), ("handled", "suspended")]
)
async def test_real_failure_flushes_only_confirmed_outbound_then_errors(
    adapter, outcome, work_state
):
    agent, backend, peer = adapter
    backend.auto_end = False
    session, identity = backend.session(), str(uuid4())
    running = asyncio.create_task(agent.prompt(session, [text()], **{"yuki.requestId": identity}))
    await backend.received.wait()
    good = backend.event(session, "sent before failure", "outbound")
    backend.event(session, "unconfirmed/model final must not leak", "outbound")
    backend.requests[session, identity]["result_event_ids"] = [good]
    work = {"work_id": "original-work", "state": work_state} if work_state else None
    backend.finish(session, identity, outcome=outcome, work=work)
    with pytest.raises(RequestError, match="did not complete") as error:
        await running
    assert error.value.data["request_id"] == identity
    assert [u.content.text for _, u in peer.updates] == ["sent before failure"]


async def test_precise_cancel_flushes_output_and_returns_cancelled(adapter):
    agent, backend, peer = adapter
    backend.auto_end = False
    session, identity = backend.session(), str(uuid4())
    running = asyncio.create_task(agent.prompt(session, [text()], **{"yuki.requestId": identity}))
    await backend.received.wait()
    output = backend.event(session, "sent before cancellation", "outbound")
    backend.requests[session, identity]["result_event_ids"].append(output)
    await agent.cancel(session)
    assert (await asyncio.wait_for(running, 3)).stop_reason == "cancelled"
    assert backend.cancels == [(session, identity)]
    assert [u.content.text for _, u in peer.updates] == ["sent before cancellation"]


async def test_external_cancel_and_unknown_use_durable_facts(adapter):
    agent, backend, _ = adapter
    backend.auto_end = False
    session, identity = backend.session(), str(uuid4())
    running = asyncio.create_task(agent.prompt(session, [text()], **{"yuki.requestId": identity}))
    await backend.received.wait()
    backend.finish(session, identity, outcome="cancelled")
    assert (await running).stop_reason == "cancelled"
    backend.requests[session, identity]["turn"] = {"state": "unknown", "outcome": None}
    with pytest.raises(RequestError, match="outcome unknown"):
        await agent.prompt(session, [text()], **{"yuki.requestId": identity})
    assert len(backend.posts) == 1


async def test_content_uses_only_explicit_client_file_read_and_source_links(adapter):
    agent, backend, peer = adapter
    session = backend.session()
    await agent.initialize(1, ClientCapabilities(fs=FileSystemCapabilities(read_text_file=True)))
    await agent.prompt(
        session,
        [
            text("question"),
            EmbeddedResourceContentBlock(
                type="resource", resource=TextResourceContents(uri="notes:1", text="embedded")
            ),
            ResourceContentBlock(
                type="resource_link", uri="file:///C:/client/example.txt", name="file"
            ),
            ResourceContentBlock(type="resource_link", uri="https://example.com/page", name="web"),
        ],
    )
    submitted = backend.events[session][0]["text"]
    assert "embedded" in submitted and "client-provided file" in submitted
    assert "https://example.com/page" in submitted
    assert peer.reads == [(session, "C:/client/example.txt")]
    await agent.initialize(1)
    for block in [
        ResourceContentBlock(type="resource_link", uri="file:///client/private.txt", name="file"),
        ImageContentBlock(type="image", data="YWJj", mime_type="image/png"),
    ]:
        with pytest.raises(RequestError):
            await agent.prompt(session, [block])
    assert len(backend.posts) == 1


async def test_local_disconnect_does_not_cancel_server_work(adapter):
    agent, backend, _ = adapter
    backend.auto_end = False
    session = backend.session()
    running = asyncio.create_task(agent.prompt(session, [text()]))
    await backend.received.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    await agent.close()
    assert backend.cancels == []
    assert next(iter(backend.requests.values()))["turn"]["state"] == "running"


async def test_cancel_during_client_file_read_never_submits_to_yuki(adapter):
    agent, backend, peer = adapter
    await agent.initialize(1, ClientCapabilities(fs=FileSystemCapabilities(read_text_file=True)))
    reading = asyncio.Event()

    async def read(**kwargs):
        reading.set()
        await asyncio.Event().wait()

    peer.read_text_file = read
    session = backend.session()
    pending = asyncio.create_task(
        agent.prompt(
            session,
            [
                ResourceContentBlock(
                    type="resource_link", uri="file:///client/slow.txt", name="slow"
                )
            ],
        )
    )
    await reading.wait()
    await agent.cancel(session)
    assert (await pending).stop_reason == "cancelled"
    assert backend.posts == [] and backend.cancels == []


async def test_lost_ack_without_original_receipt_returns_uuid_and_never_replaces_it(adapter):
    agent, backend, _ = adapter
    session, identity = backend.session(), str(uuid4())
    original = backend.handle

    async def lost(request):
        if request.method == "POST" and request.url.path.endswith("/messages"):
            backend.posts.append((session, identity, "messages"))
            raise httpx.ReadError("private transport diagnostics", request=request)
        return await original(request)

    backend.handle = lost
    with pytest.raises(RequestError, match="outcome unknown") as error:
        await agent.prompt(session, [text()], **{"yuki.requestId": identity})
    assert error.value.data["request_id"] == identity
    assert backend.posts == [(session, identity, "messages")]


async def test_authoritative_missing_receipt_does_not_use_old_accepted_snapshot(adapter):
    agent, backend, _ = adapter
    backend.auto_end = False
    session, identity = backend.session(), str(uuid4())
    pending = asyncio.create_task(agent.prompt(session, [text()], **{"yuki.requestId": identity}))
    await backend.received.wait()
    del backend.requests[session, identity]
    with pytest.raises(RequestError, match="outcome unknown"):
        await asyncio.wait_for(pending, 3)
    assert len(backend.posts) == 1


async def test_sensitive_http_failure_is_not_reflected_in_rpc_error(adapter):
    agent, backend, _ = adapter
    original = backend.handle

    async def denied(request):
        if "/acp/sessions/" in request.url.path:
            raise httpx.ConnectError(
                "Bearer private-secret https://internal-host/", request=request
            )
        return await original(request)

    backend.handle = denied
    with pytest.raises(RequestError) as error:
        await agent.prompt(backend.session(), [text()])
    assert "private-secret" not in str(error.value)
    assert "internal-host" not in str(error.value)


async def test_default_client_uses_standard_http_proxy_without_direct_fallback(
    tmp_path, monkeypatch
):
    paths = []
    session = str(uuid4())

    async def proxy(reader, writer):
        try:
            request_line = await reader.readline()
            paths.append(request_line.decode().split(" ")[1])
            headers = {}
            while (line := await reader.readline()) not in {b"\r\n", b""}:
                name, value = line.decode().split(":", 1)
                headers[name.lower()] = value.strip()
            await reader.readexactly(int(headers.get("content-length", "0")))
            if paths[-1].endswith("/me"):
                data = {
                    "request_person_id": "person",
                    "binding_id": "base",
                    "conversation_id": "base-conversation",
                }
            else:
                data = {"session_id": session}
            body = json.dumps({"data": data}).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    for key in tuple(os.environ):
        if key.upper() in {
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        }:
            monkeypatch.delenv(key)
    server = await asyncio.start_server(proxy, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("NO_PROXY", "")
    agent = YukiAgent("http://acp.invalid", "test-device-token", tmp_path)
    try:
        assert (await agent.new_session("/client")).session_id == session
    finally:
        await agent.close()
        server.close()
        await server.wait_closed()
    assert paths == ["http://acp.invalid/client/v1/me", "http://acp.invalid/client/v1/acp/sessions"]


def test_cli_loads_only_standard_http_environment(tmp_path, monkeypatch):
    from qq_ai_bot.acp_adapter import cli

    monkeypatch.chdir(tmp_path)
    for key in tuple(os.environ):
        if key.upper() in {
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "BOT_TEST_SECRET",
        }:
            monkeypatch.delenv(key)
    (tmp_path / ".env").write_text(
        "HTTP_PROXY=http://proxy.example:8080\nNO_PROXY=localhost\nBOT_TEST_SECRET=private\n",
        encoding="utf-8",
    )

    async def run(args):
        assert os.environ["HTTP_PROXY"] == "http://proxy.example:8080"
        assert os.environ["NO_PROXY"] == "localhost"
        assert "BOT_TEST_SECRET" not in os.environ

    monkeypatch.setattr(cli, "_run", run)
    assert cli.run_acp_command(argparse.Namespace()) == 0
