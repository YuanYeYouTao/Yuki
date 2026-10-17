"""Explicit peer close while the real SDK is waiting for Agent setup."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from tests.integration.test_acp_manager_peer import observe

from yuki.sandbox.acp_peer import atomic_json, read_json
from yuki.sandbox.persistent import PersistentManager
from yuki.workspace.store import WorkspaceStore


@pytest.mark.skipif(os.name != "posix", reason="Real native Supervisor process tree")
@pytest.mark.parametrize("blocked_method", ["initialize", "session/new"])
async def test_explicit_close_reaps_original_peer_blocked_before_session(tmp_path, blocked_method):
    fixture = tmp_path / "waiting_agent.py"
    fixture.write_text(
        "import json,os,signal,sys\nfrom pathlib import Path\n"
        "for line in sys.stdin.buffer:\n"
        " request=json.loads(line)\n"
        " if request['method']==sys.argv[1]:\n"
        "  Path(sys.argv[2]).write_text(str(os.getpid()))\n"
        "  while True: signal.pause()\n"
        " result={'protocolVersion':1,'agentCapabilities':{}}\n"
        " print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)\n",
        encoding="utf-8",
    )
    blocked = tmp_path / "blocked"
    config = tmp_path / "agents.toml"
    config.write_text(
        "[agents.fixture]\n"
        f"command={json.dumps(sys.executable)}\n"
        f"args={json.dumps([str(fixture), blocked_method, str(blocked)])}\n"
        f"cwd={json.dumps(str(tmp_path))}\n",
        encoding="utf-8",
    )
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
    source = Path(__file__).parents[2] / "src/yuki/sandbox"
    peer_id = manager.acp.peer_open({"agent_id": "fixture"}, "original:peer")["peer_id"]
    root = manager.acp.root(peer_id)
    spec = manager.prepare_spec(
        peer_id, json.loads(manager.acp.row(peer_id)["payload"]), "acp_peer"
    )
    spec["argv"] = [
        sys.executable,
        "-I",
        str(source / "acp_peer.py"),
        peer_id,
        "--root",
        str(manager.runtime_root),
    ]
    atomic_json(root / "spec.json", spec)
    with manager.db:
        manager.db.execute("UPDATE jobs SET state='running' WHERE id=?", (peer_id,))
        manager.db.execute("UPDATE environment_jobs SET dispatched=1 WHERE id=?", (peer_id,))
    # Only the runtime mount path is relocated; Supervisor process ownership,
    # native cancel marker and descendant termination are the product code.
    wrapper = (
        "import sys,importlib.util; from pathlib import Path; "
        "spec=importlib.util.spec_from_file_location('s',sys.argv[3]); "
        "s=importlib.util.module_from_spec(spec); spec.loader.exec_module(s); "
        "root,job=Path(sys.argv[1]),sys.argv[2]; "
        "s.Path=lambda value: root if value=='/var/lib/yuki-runtime' else Path(value); "
        "sys.argv=['supervisor',job]; raise SystemExit(s.main())"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        wrapper,
        str(manager.runtime_root),
        peer_id,
        str(source / "environment_supervisor.py"),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await observe(blocked.exists)
        assert process.returncode is None and not (root / "peer_metadata.json").exists()
        agent_pid = int(blocked.read_text())
        close = manager.acp.control("close", {"peer_id": peer_id, "purge": True}, "forget")
        assert close["admitted"] and close["pending"]
        assert (root / "state/cancel").exists(), "Unready SDK cannot consume the close inbox"
        await asyncio.wait_for(process.communicate(), 10)
        native = read_json(root / "state/status.json")
        assert native["status"] == "cancelled"
        manager.finish(peer_id, native["status"], {"exit_code": native["exit_code"]})
        manager.acp.refresh()
        closed = manager.acp.view(close["run_id"])
        assert closed["status"] == "succeeded" and closed["purged"]
        assert not root.exists()
        assert not await asyncio.to_thread(Path(f"/proc/{agent_pid}").exists)
        assert manager.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    finally:
        if process.returncode is None:
            (root / "state/cancel").touch()
            await asyncio.wait_for(process.communicate(), 10)
        await manager.close()
