"""Consistent synthetic backups retain original private refs and deletion fences."""

import asyncio
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from scripts.verify_work_backup import verify
from sqlalchemy import update
from sqlalchemy.engine import make_url
from tests.support.codemode_cases import environment, outer_call, requires_worker

from qq_ai_bot.codemode.driver import CodeCompositionYield, CodeModeDriver
from qq_ai_bot.mcp.repository import ToolArtifactRepository
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.protocol_schema import objects
from qq_ai_bot.runtime.protocol_store import ProtocolStore
from qq_ai_bot.runtime.work_repository import WorkRepository

pytestmark = requires_worker


def snapshot(source: Path, destination: Path):
    destination.mkdir()
    with sqlite3.connect(f"{(source / 'test.db').as_uri()}?mode=ro", uri=True) as original:
        with sqlite3.connect(destination / "test.db") as copy:
            original.backup(copy)
    for directory in ("work-protocol", "tool_artifacts", "workspace", "manager-evidence", "config"):
        if (source / directory).exists():
            shutil.copytree(source / directory, destination / directory)


@pytest.mark.parametrize("after_privacy", [False, True])
async def test_backup_restore_retains_only_original_owned_private_objects(
    database, tmp_path, after_privacy
):
    env = await environment(database, tmp_path, tool_limit=1)
    outer = outer_call(env, "await yuki_lookup({'q': 1})\nawait yuki_lookup({'q': 2})")
    driver = CodeModeDriver(env.host, outer)
    with pytest.raises(CodeCompositionYield):
        await driver.run()
    row = await driver._parent_row()
    composition = json.loads(row["receipt_json"])["composition"]
    binding = driver._binding(composition)
    store = env.owner.journal.objects
    original_dump = await store.get_code_snapshot(composition["snapshot_ref"], binding)
    root = Path(make_url(database.url).database).parent
    artifacts = ToolArtifactRepository(database, root / "tool_artifacts", retention_seconds=60)
    handle = await artifacts.write_artifact(
        provider_id="core",
        tool_name="lookup",
        content='{"original":"完整领域正文"}',
        media_type="application/json",
        work_id=env.control.current["id"],
    )
    for name, content in {
        "workspace/original.txt": "original workspace version",
        "manager-evidence/original.json": '{"run_id":"original-run","status":"pending"}',
        "config/model_profiles.toml": 'api_key_env = "LOCAL_DUMMY_KEY"\n',
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    if after_privacy:
        # Simulate interruption after the deletion CAS but before physical
        # unlink. The copied old bytes must never regain an original owner.
        async with database.immediate_session() as writer:
            await env.control.repository.purge_scope(writer, env.control.lease.conversation_id)
            await writer.execute(update(objects).values(deleting=True))
    copied = root / "complete-synthetic-snapshot"
    await asyncio.to_thread(snapshot, root, copied)
    counts = await asyncio.to_thread(verify, copied / "test.db", copied)
    assert counts["tool_artifacts"] == (0 if after_privacy else 1)
    if after_privacy:
        assert counts["protocol_objects"] == 0
    replay = Database(f"sqlite+aiosqlite:///{copied / 'test.db'}")
    try:
        restored = ProtocolStore(replay)
        if after_privacy:
            with pytest.raises(ValueError, match="code_snapshot_authority_changed"):
                await restored.get_code_snapshot(composition["snapshot_ref"], binding)
            assert await restored.cleanup(grace_seconds=0) > 0
            assert not restored._path(composition["snapshot_ref"]).exists()
        else:
            assert counts["protocol_objects"] > 0
            assert (
                await restored.get_code_snapshot(composition["snapshot_ref"], binding)
                == original_dump
            )
            assert await restored.cleanup(grace_seconds=0) == 0
            original = await env.control.repository.get(env.control.current["id"])
            assert await WorkRepository(replay).get(original["id"]) == original
        reader = ToolArtifactRepository(replay, copied / "tool_artifacts", retention_seconds=60)
        assert await reader.read(handle) == await artifacts.read(handle)
        for name in (
            "workspace/original.txt",
            "manager-evidence/original.json",
            "config/model_profiles.toml",
        ):
            assert (copied / name).read_bytes() == (root / name).read_bytes()
    finally:
        await replay.close()


async def test_backup_verification_refuses_a_live_deleting_protocol_object(database, tmp_path):
    env = await environment(database, tmp_path)
    await env.owner.save("paired")
    async with database.immediate_session() as writer:
        await writer.execute(update(objects).values(deleting=True))
    root = Path(make_url(database.url).database).parent
    with pytest.raises(RuntimeError, match="missing/deleting owned protocol metadata"):
        await asyncio.to_thread(verify, root / "test.db", root)


@pytest.mark.parametrize("fault", ["missing", "tampered"])
async def test_backup_verification_rejects_missing_or_corrupt_owned_bytes(
    database, tmp_path, fault
):
    env = await environment(database, tmp_path)
    await env.owner.save("paired")
    root = Path(make_url(database.url).database).parent
    copied = root / "faulted-synthetic-snapshot"
    await asyncio.to_thread(snapshot, root, copied)
    assert (await asyncio.to_thread(verify, copied / "test.db", copied))["protocol_objects"] > 0
    with sqlite3.connect(copied / "test.db") as reader:
        key = reader.execute("SELECT sha256 FROM runtime_protocol_refs LIMIT 1").fetchone()[0]
    path = copied / "work-protocol" / key[:2] / key[2:]
    if fault == "missing":
        path.unlink()
    else:
        original = path.read_bytes()
        path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    with pytest.raises(RuntimeError, match="backup protocol object incomplete"):
        await asyncio.to_thread(verify, copied / "test.db", copied)
