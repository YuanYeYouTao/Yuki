"""Independent original-protocol lifecycle regressions found during final review."""

import asyncio
import shutil
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from acp.schema import AgentCapabilities, InitializeResponse
from tests.unit.test_acp_manager import manager_at, original_request, ready_peer

from yuki.sandbox.acp_peer import Peer, operation_key, read_json


async def test_cancel_admitted_before_peer_scan_cannot_launch_the_same_queued_prompt(tmp_path):
    manager = manager_at(tmp_path)
    scanner = None
    peer: Peer | None = None
    sent = []
    try:
        peer_id, peer = ready_peer(manager)
        # Both originals reached the inbox before the peer's next scan. The
        # current hash order puts cancellation ahead of its original prompt.
        request_id = "queued-prompt"
        cancel_id = next(
            value
            for value in (f"cancel-{index}" for index in range(100))
            if operation_key(value) < operation_key(request_id)
        )
        run = manager.acp.prompt_submit({"peer_id": peer_id, "prompt": "original"}, request_id)
        manager.acp.control("cancel", {"run_id": run["run_id"]}, cancel_id)
        assert original_request(manager, peer_id, request_id)["run_id"] == run["run_id"]

        async def prompt(**kwargs):
            sent.append(kwargs)
            peer.sent.set()
            return SimpleNamespace(model_dump=lambda **_kwargs: {"stopReason": "end_turn"})

        async def cancel(**kwargs):
            raise AssertionError("An undispatched prompt needs no Agent cancellation")

        peer.connection = SimpleNamespace(prompt=prompt, cancel=cancel)
        scanner = asyncio.create_task(peer.scan())
        result_path = peer.root / "rpc/results" / f"{operation_key(request_id)}.json"
        async with asyncio.timeout(5):
            while not result_path.exists():  # noqa: ASYNC110 - original file admission boundary
                await asyncio.sleep(0.01)
        assert sent == [], "A durable cancellation must stop its still-queued original prompt"
        assert read_json(result_path)["status"] == "cancelled"
    finally:
        if peer is not None:
            peer.stopping.set()
        if scanner is not None:
            await asyncio.wait_for(scanner, 5)
        await manager.close()


async def test_failed_protocol_directory_purge_preserves_original_close_intent_for_retry(
    tmp_path, monkeypatch
):
    manager = manager_at(tmp_path)
    peer_id, peer = ready_peer(manager)
    attempts = 0
    remove = shutil.rmtree

    def fail_once(path, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary original-directory removal failure")
        return remove(path, *args, **kwargs)

    try:
        manager.acp.prompt_submit({"peer_id": peer_id, "prompt": "private body"}, "prompt")
        close = manager.acp.control("close", {"peer_id": peer_id, "purge": True}, "forget")
        assert close["admitted"] and close["pending"]
        manager.finish(peer_id, "succeeded", {"exit_code": 0})
        monkeypatch.setattr("yuki.sandbox.acp_manager.shutil.rmtree", fail_once)
        try:
            manager.acp.refresh()
        except OSError:
            pass  # Same original operation is reconciled by the next worker pass.
        assert peer.root.exists()
        manager.acp.refresh()
        assert not peer.root.exists(), "The durable purge request must survive its failed cleanup"
        view = manager.acp.view(close["run_id"])
        assert view["status"] == "succeeded" and view["purged"]
        assert attempts == 2
    finally:
        await manager.close()


async def test_unsupported_negotiated_protocol_never_starts_a_session(tmp_path, monkeypatch):
    connection = SimpleNamespace(
        initialize=AsyncMock(
            return_value=InitializeResponse(
                protocol_version=2, agent_capabilities=AgentCapabilities()
            )
        ),
        new_session=AsyncMock(side_effect=RuntimeError("session creation must not be attempted")),
    )

    @asynccontextmanager
    async def spawn(*args, **kwargs):
        yield connection, SimpleNamespace()

    monkeypatch.setattr("acp.stdio.spawn_agent_process", spawn)
    peer = Peer(
        "unsupported-protocol",
        tmp_path,
        {"command": "fixture", "cwd": str(tmp_path), "agent_id": "fixture"},
    )
    with pytest.raises((ValueError, RuntimeError)):
        await peer.run()
    connection.new_session.assert_not_called()
    assert not (tmp_path / "peer_metadata.json").exists()


async def test_restart_after_purge_before_finish_preserves_successful_original_close(tmp_path):
    manager = manager_at(tmp_path)
    peer_id, peer = ready_peer(manager)
    close_args = {"peer_id": peer_id, "purge": True}
    close = manager.acp.control("close", close_args, "forget")
    manager.finish(peer_id, "succeeded", {"exit_code": 0})
    # Real durable boundary in refresh: purge returned, finish has not run.
    manager.acp._purge(peer_id, preserve_id=close["run_id"])
    assert not peer.root.exists()
    await manager.close()
    manager = manager_at(tmp_path)
    try:
        recovered = manager.acp.control("close", close_args, "forget")
        assert recovered["run_id"] == close["run_id"]
        assert recovered["status"] == "succeeded" and recovered["purged"]
        assert not recovered.get("uncertain")
        assert not peer.root.exists()
    finally:
        await manager.close()
