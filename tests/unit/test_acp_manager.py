"""Original Manager jobs and the durable environment peer wire, without paid agents."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuki.sandbox.acp_config import load_agents
from yuki.sandbox.acp_peer import Peer, atomic_json, operation_key, read_json
from yuki.sandbox.persistent import PersistentManager
from yuki.workspace.store import WorkspaceStore


def manager_at(path: Path) -> PersistentManager:
    path.mkdir(exist_ok=True)
    config = path / "agents.toml"
    config.write_text(
        '[agents.fixture]\ncommand="fixture-agent"\nargs=["--stdio"]\n'
        'description="Local fixture"\n[agents.fixture.env]\nPRIVATE_KEY="not-public"\n',
        encoding="utf-8",
    )
    manager = PersistentManager(
        path / "manager",
        WorkspaceStore(path / "artifacts"),
        "image",
        "network",
        "",
        path / "home",
        acp_config=config,
        testing=True,
    )
    manager.ready = True
    manager.container_id = "original-container"
    return manager


def ready_peer(manager: PersistentManager, request_id: str = "resource") -> tuple[str, Peer]:
    result = manager.acp.peer_open({"agent_id": "fixture"}, request_id)
    peer_id = result["peer_id"]
    root = manager.acp.root(peer_id)
    root.mkdir()
    atomic_json(
        root / "peer_metadata.json", {"session_id": "original-session", "permission_requests": []}
    )
    with manager.db:
        manager.db.execute("UPDATE jobs SET state='running' WHERE id=?", (peer_id,))
        manager.db.execute("UPDATE environment_jobs SET dispatched=1 WHERE id=?", (peer_id,))
    peer = Peer(peer_id, root, {})
    peer.session_id = "original-session"
    return peer_id, peer


def prompt(manager: PersistentManager, peer_id: str, request_id: str = "prompt") -> dict:
    return manager.acp.prompt_submit({"peer_id": peer_id, "prompt": "原始输入"}, request_id)


def original_request(manager: PersistentManager, peer_id: str, request_id: str) -> dict:
    return read_json(manager.acp.root(peer_id) / "rpc/inbox" / f"{operation_key(request_id)}.json")


async def test_catalog_is_configuration_only_and_does_not_expose_environment(tmp_path):
    manager = manager_at(tmp_path)
    try:
        result = await manager.handle({"method": "acp_agents", "args": {}})
        assert result == {"agents": [{"id": "fixture", "description": "Local fixture"}]}
        assert manager.queue.empty()
        assert manager.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert not list(manager.runtime_root.iterdir())
        unavailable = await manager.handle(
            {"method": "acp_peer_open", "args": {"agent_id": "missing"}, "request_id": "unknown"}
        )
        assert unavailable["error"] == "acp_agent_not_configured"
    finally:
        await manager.close()


async def test_original_peer_and_prompt_survive_manager_restart_without_reposting(tmp_path):
    manager = manager_at(tmp_path)
    peer_id, peer = ready_peer(manager)
    result = prompt(manager, peer_id)
    request = original_request(manager, peer_id, "prompt")
    accepted = peer.root / "rpc/accepted" / f"{operation_key('prompt')}.json"
    atomic_json(accepted, request)
    (peer.root / "rpc/inbox" / accepted.name).unlink()
    await manager.close()
    manager = manager_at(tmp_path)
    try:
        again = prompt(manager, peer_id)
        assert again["run_id"] == result["run_id"]
        assert again["session_id"] == "original-session"
        assert not (peer.root / "rpc/inbox" / accepted.name).exists()
        assert read_json(accepted) == request
        assert manager.queue.empty()
        assert manager.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        assert manager.acp.peer_open({"agent_id": "fixture"}, "resource")["peer_id"] == peer_id
        conflict = await manager.handle(
            {
                "method": "acp_prompt_submit",
                "args": {"peer_id": peer_id, "prompt": "changed"},
                "request_id": "prompt",
            }
        )
        assert conflict["error"] == "idempotency_conflict"
    finally:
        await manager.close()


async def test_prompt_waits_for_session_metadata_then_posts_once(tmp_path):
    manager = manager_at(tmp_path)
    try:
        peer_id = manager.acp.peer_open({"agent_id": "fixture"}, "resource")["peer_id"]
        run = prompt(manager, peer_id)
        assert run["status"] == "queued"
        assert not manager.acp.root(peer_id).exists()
        atomic_json(manager.acp.root(peer_id) / "peer_metadata.json", {"session_id": "ready"})
        manager.acp.refresh()
        path = manager.acp.root(peer_id) / "rpc/inbox" / f"{operation_key('prompt')}.json"
        before = path.read_bytes()
        manager.acp.refresh()
        assert path.read_bytes() == before
        assert manager.acp.view(run["run_id"])["status"] == "running"
    finally:
        await manager.close()


async def test_original_updates_paginate_and_completion_matches_outbox_after_ack(tmp_path):
    manager = manager_at(tmp_path)
    try:
        peer_id, peer = ready_peer(manager)
        peer.event("session_update", {"initial": "not a prompt"})
        start = (peer.root / "rpc/events.jsonl").stat().st_size
        run = prompt(manager, peer_id)
        request = original_request(manager, peer_id, "prompt")
        peer.active = request
        for index in range(103):
            peer.event("session_update", {"text": f"结果{index}"})
        end = (peer.root / "rpc/events.jsonl").stat().st_size
        peer.finish(
            request,
            status="succeeded",
            result={"stopReason": "end_turn"},
            event_cursor_start=start,
            event_cursor_end=end,
        )
        peer.active = None
        peer.event("session_update", {"later": "not this prompt"})
        manager.acp.refresh()
        view = manager.acp.query(run["run_id"])
        assert len(view["events"]) == 100 and view["has_more"]
        page = manager.acp.query(run["run_id"], view["next_cursor"])
        assert len(page["events"]) == 3 and not page["has_more"]
        assert page["next_cursor"] == end
        assert all(event["run_id"] == run["run_id"] for event in view["events"] + page["events"])
        original = manager.completions.pending()["events"]
        assert len(original) == 1
        assert original[0]["result"] == view["completion"] == page["completion"]
        assert "events" not in view["completion"]
        manager.completions.acknowledge(run["run_id"])
        manager.acp.events({"peer_id": peer_id, "ack_cursor": end})
        assert manager.acp.query(run["run_id"])["events"] == view["events"]
        manager.acp.refresh()
        assert not manager.completions.pending()["events"]
        assert manager.acp.view(peer_id)["status"] == "running"
    finally:
        await manager.close()


async def test_dead_peer_keeps_original_unknown_and_never_rebuys_prompt(tmp_path):
    manager = manager_at(tmp_path)
    try:
        peer_id, peer = ready_peer(manager)
        run = prompt(manager, peer_id)
        request = original_request(manager, peer_id, "prompt")
        atomic_json(peer.root / "rpc/accepted" / f"{operation_key('prompt')}.json", request)
        # Crash between file admission and the Manager's dispatch bit commit.
        with manager.db:
            manager.db.execute(
                "UPDATE environment_jobs SET dispatched=0 WHERE id=?", (run["run_id"],)
            )
        manager.finish(peer_id, "failed", {"error": "process_exited"})
        manager.acp.refresh()
        view = prompt(manager, peer_id)
        assert view["run_id"] == run["run_id"]
        assert view["status"] == "failed" and view["uncertain"]
        assert manager.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        assert manager.queue.qsize() == 1  # Only the original peer admission.
        assert manager.completions.pending()["events"][0]["result"] == view["completion"]
    finally:
        await manager.close()


async def test_terminal_alias_cancels_original_prompt_and_leaves_newer_prompt_and_peer(tmp_path):
    manager = manager_at(tmp_path)
    try:
        peer_id, peer = ready_peer(manager)
        run = prompt(manager, peer_id)
        cancelled = await manager.control(run["run_id"], "cancel")
        assert cancelled["run_id"] == run["run_id"] and cancelled["status"] == "running"
        cancel = original_request(manager, peer_id, f"prompt-cancel:{run['run_id']}")
        assert cancel["method"] == "cancel" and cancel["run_id"] == run["run_id"]
        peer.finish(
            original_request(manager, peer_id, "prompt"),
            status="cancelled",
            result={"stopReason": "cancelled"},
        )
        manager.acp.refresh()
        newer = prompt(manager, peer_id, "newer")
        assert newer["status"] == "running"
        assert (await manager.control(run["run_id"], "cancel"))["status"] == "cancelled"
        assert manager.acp.view(newer["run_id"])["status"] == "running"
        assert manager.acp.view(peer_id)["status"] == "running"
        read = await manager.handle({"method": "terminal_read", "args": {"run_id": run["run_id"]}})
        assert read["completion"]["status"] == "cancelled"
    finally:
        await manager.close()


async def test_permission_projection_and_events_ack_keep_original_question_ids(tmp_path):
    manager = manager_at(tmp_path)
    try:
        peer_id, peer = ready_peer(manager)
        run = prompt(manager, peer_id)
        peer.active = original_request(manager, peer_id, "prompt")
        questions = [
            {"run_id": run["run_id"], "permission_id": key, "options": [{"optionId": "once"}]}
            for key in ("question-one", "question-two")
        ]
        atomic_json(
            peer.root / "peer_metadata.json",
            {"session_id": peer.session_id, "permission_requests": questions},
        )
        for question in questions:
            peer.event("permission_request", question, permission_id=question["permission_id"])
        view = manager.acp.view(run["run_id"])
        assert view["permission_requests"] == questions
        events = manager.acp.events({"peer_id": peer_id})
        assert len(events["events"]) == 2
        manager.acp.events({"peer_id": peer_id, "ack_cursor": events["next_cursor"]})
        assert read_json(peer.root / "rpc/events.ack")["cursor"] == events["next_cursor"]
        reply = manager.acp.control(
            "permission_reply",
            {"run_id": run["run_id"], "permission_id": "question-one", "option_id": "once"},
            "reply",
        )
        assert reply["status"] == "running"
        assert (
            original_request(manager, peer_id, "reply")["payload"]["permission_id"]
            == "question-one"
        )
        assert manager.acp.view(run["run_id"])["pending"]
    finally:
        await manager.close()


async def test_close_before_open_is_durable_and_late_open_never_launches(tmp_path):
    manager = manager_at(tmp_path)
    closed = manager.acp.control(
        "close", {"resource_request_id": "original:peer", "purge": True}, "forget"
    )
    assert closed["admitted"] and closed["purged"]
    peer_id = closed["peer_id"]
    await manager.close()
    manager = manager_at(tmp_path)
    try:
        manager.ready = False
        late = manager.acp.peer_open({"agent_id": "fixture"}, "original:peer")
        assert late["peer_id"] == peer_id and late["status"] == "cancelled" and late["purged"]
        assert manager.queue.empty()
        assert not manager.acp.root(peer_id).exists()
        assert manager.acp.control(
            "close", {"resource_request_id": "original:peer", "purge": True}, "forget"
        )["purged"]
    finally:
        await manager.close()


async def test_purge_waits_for_actual_process_exit_and_preserves_unrelated_home_and_peer(tmp_path):
    manager = manager_at(tmp_path)
    try:
        peer_id, peer = ready_peer(manager)
        other_id, other = ready_peer(manager, "other-resource")
        manager.home.mkdir()
        secret = manager.home / "operator-credential"
        secret.write_text("keep", encoding="utf-8")
        run = prompt(manager, peer_id)
        closed = manager.acp.control("close", {"peer_id": peer_id, "purge": True}, "forget")
        assert closed["admitted"] and closed["pending"] and peer.root.exists()
        denied = await manager.handle(
            {
                "method": "acp_prompt_submit",
                "args": {"peer_id": peer_id, "prompt": "new"},
                "request_id": "late",
            }
        )
        assert denied["error"] == "acp_peer_closed"
        peer.finish(
            original_request(manager, peer_id, "forget"),
            status="succeeded",
            result={"closed": True},
        )
        manager.acp.refresh()
        assert peer.root.exists()  # Protocol close alone is not supervisor exit.
        manager.finish(peer_id, "succeeded", {"exit_code": 0})
        manager.acp.refresh()
        assert not peer.root.exists()
        assert other.root.exists() and manager.acp.view(other_id)["status"] == "running"
        assert secret.read_text(encoding="utf-8") == "keep"
        assert manager.acp.view(run["run_id"])["purged"]
        assert not manager.completions.pending()["events"]
        assert "原始输入" not in json.dumps(
            [dict(row) for row in manager.db.execute("SELECT * FROM jobs")], ensure_ascii=False
        )
        assert manager.acp.events({"peer_id": peer_id, "ack_cursor": 100})["purged"]
        assert not peer.root.exists()
    finally:
        await manager.close()


def test_operator_configuration_resolves_explicit_environment_without_changing_inherited_proxy(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FIXTURE_KEY", "private-key")
    monkeypatch.setenv("HTTPS_PROXY", "http://existing-proxy:1080")
    config = tmp_path / "agents.toml"
    config.write_text(
        '[agents.fixture]\ncommand="adapter"\npermission_mode="ask"\n'
        '[agents.fixture.env_from]\nAPI_KEY="FIXTURE_KEY"\n'
        '[agents.fixture.env]\nHTTPS_PROXY="http://explicit-proxy:8080"\n',
        encoding="utf-8",
    )
    program = load_agents(config)["fixture"]
    assert program.env == {"API_KEY": "private-key", "HTTPS_PROXY": "http://explicit-proxy:8080"}
    assert program.permission_mode == "ask"
    assert "private-key" not in repr(program)
    assert os.environ["HTTPS_PROXY"] == "http://existing-proxy:1080"


@pytest.mark.parametrize(
    "setting", ["command=[]", 'command="x"\nargs=[1]', 'command="x"\npermission_mode="automatic"']
)
def test_invalid_operator_config_is_reported_without_inventing_an_adapter(tmp_path, setting):
    config = tmp_path / "agents.toml"
    config.write_text(f"[agents.fixture]\n{setting}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid_acp_"):
        load_agents(config)


async def test_close_during_peer_launch_does_not_resurrect_or_attach_original_process(
    tmp_path, monkeypatch
):
    manager = manager_at(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    requests = []

    async def request(method, path, data=None):
        requests.append((method, path))
        if method == "POST":
            entered.set()
            await release.wait()
            return {"session_id": "unused-pty"}
        return {}

    monkeypatch.setattr(os, "geteuid", lambda: 10001, raising=False)
    manager.execd = SimpleNamespace(request=request)
    manager.attach = AsyncMock()
    launch = None
    try:
        peer_id = manager.acp.peer_open({"agent_id": "fixture"}, "resource")["peer_id"]
        launch = asyncio.create_task(manager.launch(peer_id))
        await entered.wait()
        spec = read_json(manager.acp.root(peer_id) / "spec.json")
        assert spec["argv"] == [
            "/usr/local/bin/python",
            "-I",
            "/opt/yuki-runtime/acp_peer.py",
            peer_id,
        ]
        assert spec["acp_peer"]["agent_id"] == "fixture" and spec["timeout_seconds"] == 0
        # POSIX permits unlinking a read-only spec; Windows test hosts do not.
        (manager.acp.root(peer_id) / "spec.json").chmod(0o600)
        receipt = manager.acp.control("close", {"peer_id": peer_id, "purge": True}, "forget")
        assert receipt["purged"]
        release.set()
        await launch
        assert manager.acp.view(peer_id)["status"] == "cancelled"
        assert not manager.acp.root(peer_id).exists()
        assert requests == [("POST", "/pty"), ("DELETE", "/pty/unused-pty")]
        manager.attach.assert_not_called()
    finally:
        release.set()
        if launch is not None:
            await asyncio.gather(launch, return_exceptions=True)
        await manager.close()


async def test_event_ack_is_monotonic_and_cannot_skip_unwritten_or_partial_events(tmp_path):
    manager = manager_at(tmp_path)
    try:
        peer_id, peer = ready_peer(manager)
        peer.event("session_update", {"text": "first"})
        first = manager.acp.events({"peer_id": peer_id})["next_cursor"]
        peer.event("session_update", {"text": "second"})
        manager.acp.events({"peer_id": peer_id, "ack_cursor": first})
        remaining = manager.acp.events({"peer_id": peer_id, "ack_cursor": 0})
        assert len(remaining["events"]) == 1
        assert remaining["events"][0]["payload"] == {"text": "second"}
        end = remaining["next_cursor"]
        for invalid in (first + 1, end + 1):
            with pytest.raises(ValueError, match="invalid_acp_event_ack"):
                manager.acp.events({"peer_id": peer_id, "ack_cursor": invalid})
        assert read_json(peer.root / "rpc/events.ack")["cursor"] == first
    finally:
        await manager.close()


async def test_cancelled_observation_keeps_the_original_prompt_alive(tmp_path):
    manager = manager_at(tmp_path)
    try:
        peer_id, _peer = ready_peer(manager)
        run = prompt(manager, peer_id)
        observation = asyncio.create_task(manager.wait_result(run["run_id"], wait_seconds=100))
        await asyncio.sleep(0)
        observation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await observation
        assert manager.acp.view(run["run_id"])["status"] == "running"
        assert manager.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        assert not manager._waiters
    finally:
        await manager.close()
