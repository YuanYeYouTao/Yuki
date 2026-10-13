"""Checkout retries read the original receipt after a physical file write."""

import asyncio
import hashlib
import json

import pytest
from tests.support.workspace_snapshots import snapshot_bytes

from qq_ai_bot.sandbox.persistent import PersistentManager
from qq_ai_bot.workspace.store import WorkspaceStore


@pytest.mark.parametrize("legacy", [False, True])
async def test_checkout_interrupted_after_write_replays_original_unknown_receipt(
    tmp_path, monkeypatch, legacy
):
    store = WorkspaceStore(tmp_path / "artifacts")
    artifact = snapshot_bytes(store, "source.txt", b"immutable original bytes")
    manager = PersistentManager(
        tmp_path / "manager",
        store,
        "test-image",
        "test-network",
        "",
        tmp_path / "home",
        testing=True,
    )
    original = manager.file_operation
    manager.files.root.mkdir(parents=True, exist_ok=True)
    calls = []

    def interrupted(method, args):
        calls.append(dict(args))
        original(method, args)
        raise asyncio.CancelledError("acknowledgement interrupted after write")

    try:
        args = {"artifact_id": artifact["artifact_id"], "name": "target.txt"}
        monkeypatch.setattr(manager, "file_operation", interrupted)
        with pytest.raises(asyncio.CancelledError):
            await manager.file_request("workspace_checkout", args, "original-checkout")
        if legacy:
            with manager.db:
                manager.db.execute(
                    "UPDATE environment_mutations SET digest=? WHERE request_id=?",
                    (
                        hashlib.sha256(
                            json.dumps(
                                ["workspace_checkout", args | {"path": "target.txt"}],
                                sort_keys=True,
                            ).encode()
                        ).hexdigest(),
                        "original-checkout",
                    ),
                )
        monkeypatch.setattr(manager, "file_operation", original)
        result = await manager.file_request("workspace_checkout", args, "original-checkout")
        assert result["error"] == "file_mutation_unknown_read_current_version"
        assert result["retryable"] is False and len(calls) == 1
        assert manager.files.read("target.txt")["text"] == "immutable original bytes"
        changed = await manager.file_request(
            "workspace_checkout", {**args, "name": "different.txt"}, "original-checkout"
        )
        assert changed == {"error": "idempotency_conflict"}
        assert len(manager.db.execute("SELECT * FROM environment_mutations").fetchall()) == 1
    finally:
        manager.db.close()


@pytest.mark.parametrize("collision", [False, True])
async def test_old_completed_checkout_returns_original_receipt(tmp_path, monkeypatch, collision):
    store = WorkspaceStore(tmp_path / "artifacts")
    artifact = snapshot_bytes(store, "source.txt", b"immutable original bytes")
    manager = PersistentManager(
        tmp_path / "manager",
        store,
        "test-image",
        "test-network",
        "",
        tmp_path / "home",
        testing=True,
    )
    manager.files.root.mkdir(parents=True, exist_ok=True)
    try:
        if collision:
            manager.files.write("target.txt", b"another file")
        args = {"artifact_id": artifact["artifact_id"], "name": "target.txt"}
        result = await manager.file_request("workspace_checkout", args, "legacy-checkout")
        assert "error" not in result
        path = manager.db.execute("SELECT path FROM workspace_migrations").fetchone()[0]
        with manager.db:
            manager.db.execute(
                "UPDATE environment_mutations SET digest=?",
                (
                    hashlib.sha256(
                        json.dumps(
                            ["workspace_checkout", args | {"path": path}], sort_keys=True
                        ).encode()
                    ).hexdigest(),
                ),
            )

        def no_replay(*_args):
            raise AssertionError("confirmed checkout must not execute again")

        monkeypatch.setattr(manager, "file_operation", no_replay)
        assert await manager.file_request("workspace_checkout", args, "legacy-checkout") == result
        if collision:
            assert manager.files.read("target.txt")["text"] == "another file"
        changed = await manager.file_request(
            "workspace_checkout", {**args, "name": "changed.txt"}, "legacy-checkout"
        )
        assert changed == {"error": "idempotency_conflict"}
    finally:
        manager.db.close()
