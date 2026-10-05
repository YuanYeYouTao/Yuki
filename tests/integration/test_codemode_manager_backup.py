"""Back up real Bot/Manager stores; replay the original independent run receipt.

The execd boundary is a synthetic durable status/log fixture, not a gVisor
runtime acceptance. Manager admission, SQLite, receipt reconciliation, outbox,
Bot inbox, workspace versions and private Code Mode objects are real.
"""

import asyncio
import json
import shutil
import sqlite3
import time
from pathlib import Path

import pytest
from scripts.verify_work_backup import verify
from sqlalchemy import select
from sqlalchemy.engine import make_url
from tests.support.codemode_cases import environment, outer_call, requires_worker

from qq_ai_bot.codemode.driver import CodeCompositionYield, CodeModeDriver
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.sandbox.persistent import PersistentManager
from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository
from qq_ai_bot.workspace.store import WorkspaceStore

pytestmark = requires_worker


async def test_complete_backup_reconciles_original_manager_receipt_without_execution(
    database, tmp_path
):
    env = await environment(database, tmp_path, tool_limit=1)
    driver = CodeModeDriver(
        env.host, outer_call(env, "await yuki_lookup({})\nawait yuki_lookup({})")
    )
    with pytest.raises(CodeCompositionYield):
        await driver.run()
    root = Path(make_url(database.url).database).parent
    store = WorkspaceStore(root / "workspace-artifacts")
    frozen = store.write("原件.txt", "不可重复的原产物".encode())
    (root / "home/workspace").mkdir(parents=True)
    manager = PersistentManager(
        root / "manager", store, "fixture", "fixture", "", root / "home", testing=True
    )
    manager.ready = True
    manager.container_id = "synthetic-original-environment-generation"
    request_id = f"{env.control.current['id']}:original-terminal-operation"
    arguments = {"command": "synthetic independent execution"}
    submission = asyncio.create_task(manager.submit("terminal_exec", arguments, request_id))
    run_id = await asyncio.wait_for(manager.queue.get(), 2)
    # This is the post-dispatch crash window. The independent execd receipt
    # exists, while the original Manager/Bot have not consumed its completion.
    with manager.db:
        manager.db.execute(
            "UPDATE environment_jobs SET dispatched=1,container_id=?,session_id=? WHERE id=?",
            (manager.container_id, "original-execd-session", run_id),
        )
    assert manager.finish(run_id, "running", {})
    state = manager.state_path(run_id)
    state.mkdir(parents=True)
    output = "已执行一次\n".encode()
    (state / "output-0.log").write_bytes(output)
    (state / "status.json").write_text(
        json.dumps(
            {
                "status": "succeeded",
                "exit_code": 0,
                "output_offset": len(output),
                "finished_at": time.time(),
            }
        )
    )
    manager.files.write("ground-truth.txt", output)
    original = await submission
    assert original["pending"] and original["run_id"] == run_id
    async with database.sessions() as reader:
        event_id = await reader.scalar(select(ChatEventModel.id))
    tasks = SandboxTaskRepository(database)
    source = {
        "conversation_id": env.control.lease.conversation_id,
        "origin": "user_message",
        "actor_user_id": "10001",
        "trigger_event_id": event_id,
        "generation": 1,
    }
    await tasks.prepare(request_id, arguments, source)
    await tasks.bind_run(request_id, run_id)
    manager.db.close()
    copied = root / "complete-manager-snapshot"

    def backup():
        copied.mkdir()
        with sqlite3.connect(f"{(root / 'test.db').as_uri()}?mode=ro", uri=True) as original:
            with sqlite3.connect(copied / "test.db") as copy:
                original.backup(copy)
        for name in ("work-protocol", "workspace-artifacts", "manager", "home"):
            shutil.copytree(root / name, copied / name)

    await asyncio.to_thread(backup)
    assert (await asyncio.to_thread(verify, copied / "test.db", copied))["protocol_objects"] > 0
    replay = Database(f"sqlite+aiosqlite:///{copied / 'test.db'}")
    restored = PersistentManager(
        copied / "manager",
        WorkspaceStore(copied / "workspace-artifacts"),
        "fixture",
        "fixture",
        "",
        copied / "home",
        testing=True,
    )
    try:
        rows = restored.active()
        assert len(rows) == 1 and rows[0]["id"] == run_id and rows[0]["dispatched"] == 1
        assert restored.get(run_id)["pending"]
        assert (await SandboxTaskRepository(replay).get(request_id)).status == "waiting"
        await restored.reconcile(rows[0])
        result = restored.get(run_id)
        assert result["status"] == "succeeded" and result["run_id"] == run_id
        assert result["output"] == output.decode()
        assert restored.queue.empty()  # Restore queried the receipt; no launch was queued.
        events = restored.completions.pending()["events"]
        assert len(events) == 1 and events[0]["request_id"] == request_id
        inbox = SandboxTaskRepository(replay)
        await inbox.receive(events[0])
        await inbox.receive(events[0])
        saved = await inbox.get(request_id)
        assert saved.run_id == run_id and saved.status == "completed"
        assert json.loads(saved.source_json) == source
        assert restored.store.read(frozen["artifact_id"])["text"] == "不可重复的原产物"
        assert restored.files.read("ground-truth.txt")["text"] == output.decode()
        assert len(restored.completions.pending()["events"]) == 1
    finally:
        restored.db.close()
        await replay.close()
