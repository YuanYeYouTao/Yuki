from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from yuki.sandbox.acp_peer import (
    Peer,
    agent_environment,
    atomic_json,
    operation_key,
    post_request,
    read_events,
    read_json,
    read_protocol_line,
)


async def eventually(probe):
    async with asyncio.timeout(10):
        while not (value := probe()):  # noqa: ASYNC110 - observing an external file outbox
            await asyncio.sleep(0.02)
        return value


@pytest_asyncio.fixture
async def running_peer(tmp_path):
    identity = str(uuid4())
    root = tmp_path / identity
    root.mkdir()
    spec = {
        "agent_id": "fixture",
        "command": sys.executable,
        "args": [str(Path(__file__).parents[1] / "support" / "acp_host_agent.py"), str(root)],
        "cwd": str(root),
        "env": {},
        "permission_mode": "ask",
    }
    atomic_json(root / "spec.json", {"acp_peer": spec})
    peer = Peer(identity, root, spec)
    task = asyncio.create_task(peer.run())
    await eventually(lambda: (root / "peer_metadata.json").exists() or task.done())
    if task.done():
        await task
    try:
        yield peer, root
    finally:
        peer.stopping.set()
        await asyncio.wait_for(task, 10)


def submit(peer, root, method, *, run_id=None, operation_id=None, **payload):
    request = {
        "version": 1,
        "operation_id": operation_id or str(uuid4()),
        "method": method,
        "peer_id": peer.identity,
        "run_id": run_id,
        "payload": payload,
    }
    post_request(root, request)
    return request


async def result(root, request):
    path = root / "rpc" / "results" / f"{operation_key(request['operation_id'])}.json"
    await eventually(path.exists)
    return read_json(path)


@pytest.mark.asyncio
async def test_original_prompt_reused_unicode_frames_and_observer_cancellation(running_peer):
    peer, root = running_peer
    request = submit(peer, root, "prompt", run_id=str(uuid4()), prompt="large")
    await result(root, request)
    post_request(root, request)
    page = read_events(root)
    assert page["events"][0]["payload"]["content"]["text"] == "雪" * 100000
    assert not read_events(root, page["next_cursor"])["events"]
    with pytest.raises(ValueError, match="changed"):
        post_request(root, {**request, "payload": {"prompt": "changed"}})
    waiting = submit(peer, root, "prompt", run_id=str(uuid4()), prompt="wait")
    await eventually(lambda: (root / "prompt-active").exists())
    observer = asyncio.create_task(result(root, waiting))
    observer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observer
    cancel = submit(peer, root, "cancel", run_id=waiting["run_id"])
    assert (await result(root, cancel))["result"]["cancel_sent"]
    assert (await result(root, waiting))["status"] == "cancelled"
    later = submit(peer, root, "prompt", run_id=str(uuid4()), prompt="done")
    assert (await result(root, later))["status"] == "succeeded"
    stale = submit(peer, root, "cancel", run_id=waiting["run_id"])
    assert (await result(root, stale))["result"]["cancel_sent"] is False
    methods = [
        json.loads(line)["method"] for line in (root / "calls.jsonl").read_text().splitlines()
    ]
    assert methods.count("initialize") == methods.count("session/new") == 1
    assert methods.count("session/prompt") == 3


@pytest.mark.asyncio
async def test_distinct_permission_callbacks_reliable_originals_and_reply(running_peer):
    peer, root = running_peer
    prompt = submit(peer, root, "prompt", run_id=str(uuid4()), prompt="permission")
    seen = set()
    for _ in range(2):

        def question():
            return next(
                (
                    item
                    for item in read_json(root / "peer_metadata.json").get(
                        "permission_requests", []
                    )
                    if item["permission_id"] not in seen
                ),
                None,
            )

        pending = await eventually(question)
        seen.add(pending["permission_id"])
        reply = submit(
            peer,
            root,
            "permission_reply",
            run_id=prompt["run_id"],
            permission_id=pending["permission_id"],
            option_id="yes",
        )
        assert (await result(root, reply))["status"] == "succeeded"
        post_request(root, reply)
    assert (await result(root, prompt))["status"] == "succeeded"
    assert len(seen) == 2
    page = read_events(root, limit=1)
    assert page["has_more"]
    assert len(read_events(root, page["next_cursor"])["events"]) == 3
    assert read_json(root / "peer_metadata.json")["permission_requests"] == []


@pytest.mark.asyncio
async def test_files_and_background_terminal_live_past_prompt_and_close_releases(running_peer):
    peer, root = running_peer
    peer.spec["env"]["YUKI_PEER_ENV_PROBE"] = "configured-environment"
    prompt = submit(peer, root, "prompt", run_id=str(uuid4()), prompt="callbacks")
    assert (await result(root, prompt))["status"] == "succeeded"
    assert (root / "callback.txt").read_text(encoding="utf-8") == "真实回调\n"
    terminal = peer.terminals[(root / "terminal-id").read_text()]
    assert terminal.process.returncode is None
    await eventually(lambda: b"configured-environment" in terminal.output)
    close = submit(peer, root, "close")
    assert (await result(root, close))["result"]["closed"]
    assert terminal.process.returncode is not None


@pytest.mark.asyncio
async def test_disconnected_original_is_uncertain_and_same_peer_is_never_reinitialized(
    running_peer,
):
    peer, root = running_peer
    prompt = submit(peer, root, "prompt", run_id=str(uuid4()), prompt="crash")
    receipt = await result(root, prompt)
    assert receipt["status"] == "failed"
    assert receipt["uncertain"] is True
    # Retrying the exact intent only observes its accepted/result original.
    post_request(root, prompt)
    with pytest.raises(FileExistsError):
        await Peer(peer.identity, root, peer.spec).run()
    methods = [
        json.loads(line)["method"] for line in (root / "calls.jsonl").read_text().splitlines()
    ]
    assert methods.count("initialize") == methods.count("session/new") == 1
    assert methods.count("session/prompt") == 1


@pytest.mark.asyncio
async def test_cancel_pending_permission_rejects_late_reply(running_peer):
    peer, root = running_peer
    prompt = submit(peer, root, "prompt", run_id=str(uuid4()), prompt="permission")
    questions = await eventually(
        lambda: read_json(root / "peer_metadata.json").get("permission_requests")
    )
    wrong = submit(
        peer,
        root,
        "permission_reply",
        run_id=prompt["run_id"],
        permission_id=questions[0]["permission_id"],
        option_id="not-an-option",
    )
    assert (await result(root, wrong))["status"] == "failed"
    assert read_json(root / "peer_metadata.json")["permission_requests"]
    cancelled = submit(peer, root, "cancel", run_id=prompt["run_id"])
    await result(root, cancelled)
    assert (await result(root, prompt))["status"] == "cancelled"
    late = submit(
        peer,
        root,
        "permission_reply",
        run_id=prompt["run_id"],
        permission_id=questions[0]["permission_id"],
        option_id="yes",
    )
    assert (await result(root, late))["status"] == "failed"
    assert read_json(root / "peer_metadata.json")["permission_requests"] == []


def test_event_reader_does_not_ack_partial_original(tmp_path):
    peer = Peer(str(uuid4()), tmp_path, {})
    peer.event("session_update", {"text": "完整原件"})
    first = read_events(tmp_path)
    path = tmp_path / "rpc" / "events.jsonl"
    with path.open("ab") as stream:
        stream.write(b'{"partial":')
    page = read_events(tmp_path, first["next_cursor"])
    assert page == {"events": [], "next_cursor": first["next_cursor"], "has_more": False}


@pytest.mark.asyncio
async def test_manager_client_preserve_frames_larger_than_each_stream_buffer(tmp_path, monkeypatch):
    from yuki.sandbox.client import SandboxClient
    from yuki.sandbox.persistent import PersistentManager
    from yuki.workspace.store import WorkspaceStore

    identity = "雪" * 100000
    config = tmp_path / "agents.toml"
    config.write_text(f'[agents."{identity}"]\ncommand="fixture"\n', encoding="utf-8")
    manager = PersistentManager(
        tmp_path / "manager",
        WorkspaceStore(tmp_path / "artifacts"),
        "image",
        "network",
        "",
        tmp_path / "home",
        acp_config=config,
        testing=True,
    )
    manager.ready = True
    # TCP only supplies portable test transport; both production framing owners run.
    server = await asyncio.start_server(manager.serve, "127.0.0.1", 0, limit=1024)
    port = server.sockets[0].getsockname()[1]

    async def connect(_path, **kwargs):
        return await asyncio.open_connection("127.0.0.1", port, limit=1024)

    monkeypatch.setattr(asyncio, "open_unix_connection", connect, raising=False)
    client = SandboxClient(tmp_path / "manager.sock")
    try:
        opened = await client.execute("acp_peer_open", {"agent_id": identity}, request_id="open")
        assert opened["peer_id"]
        listed = await client.execute("acp_agents", {}, request_id="list")
        assert listed["agents"][0]["id"] == identity
    finally:
        server.close()
        await server.wait_closed()
        await manager.close()


@pytest.mark.asyncio
async def test_protocol_eof_does_not_accept_partial_json_frame():
    reader = asyncio.StreamReader(limit=4)
    reader.feed_data(b'{"valid":"json"}')
    reader.feed_eof()
    with pytest.raises(ValueError, match="incomplete"):
        await read_protocol_line(reader)


def test_environment_proxy_ca_and_explicit_override(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://environment-proxy")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/environment-ca.pem")
    monkeypatch.setenv("EXECD_SECRET", "not-for-agent")
    env = agent_environment({"HTTPS_PROXY": "http://explicit", "TOKEN": "operator-value"})
    assert env["HTTPS_PROXY"] == "http://explicit"
    assert env["NODE_EXTRA_CA_CERTS"] == "/environment-ca.pem"
    assert "EXECD_SECRET" not in env
    assert env["TOKEN"] == "operator-value"
    assert os.environ["HTTPS_PROXY"] == "http://environment-proxy"
