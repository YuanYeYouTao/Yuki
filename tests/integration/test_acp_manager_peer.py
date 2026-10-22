"""Original Manager SQLite -> environment file wire -> real ACP SDK stdio fixture."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.integration.test_current_material_main_contract import _deployment

from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.llm.fake import FakeLLMProvider
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import effects, work
from yuki.sandbox.acp_peer import read_json
from yuki.sandbox.db_models import SandboxTaskRunModel
from yuki.sandbox.persistent import PersistentManager
from yuki.social.db_models import SocialOperationModel
from yuki.workspace.store import WorkspaceStore


async def observe(probe):
    async with asyncio.timeout(10):
        while not (value := probe()):  # noqa: ASYNC110 - the independent peer owns the file outbox
            await asyncio.sleep(0.02)
        return value


async def test_manager_reconnects_original_sdk_permission_and_prompt_receipts(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(os, "geteuid", lambda: 10001, raising=False)
    agent_files = tmp_path / "agent-files"
    agent_files.mkdir()
    fixture = Path(__file__).parents[1] / "support" / "acp_host_agent.py"
    source = Path(__file__).parents[2] / "src" / "yuki" / "sandbox" / "acp_peer.py"
    config = tmp_path / "agents.toml"
    config.write_text(
        "[agents.fixture]\n"
        f"command={json.dumps(sys.executable)}\n"
        f"args={json.dumps([str(fixture), str(agent_files)])}\n"
        f"cwd={json.dumps(str(agent_files))}\n"
        'permission_mode="ask"\n',
        encoding="utf-8",
    )

    def reopen():
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
        return manager

    manager = reopen()
    process = None
    try:
        resource = await manager.handle(
            {
                "method": "acp_peer_open",
                "args": {"agent_id": "fixture"},
                "request_id": "original:peer",
            }
        )
        peer_id = resource["peer_id"]
        root = manager.acp.root(peer_id)
        args = json.loads(manager.acp.row(peer_id)["payload"])
        manager.prepare_spec(peer_id, args, "acp_peer")
        with manager.db:
            manager.db.execute("UPDATE environment_jobs SET dispatched=1 WHERE id=?", (peer_id,))
            manager.db.execute("UPDATE jobs SET state='running' WHERE id=?", (peer_id,))
        # This substitutes only execd/Supervisor launch. The SDK and Agent run
        # as real independent processes and produce their own protocol results.
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(source),
            peer_id,
            "--root",
            str(manager.runtime_root),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await observe(lambda: (root / "peer_metadata.json").exists())
        assert read_json(root / "peer_metadata.json")["session_id"] == "fixture-session"
        intent = {
            "method": "acp_prompt_submit",
            "args": {"peer_id": peer_id, "prompt": "permission"},
            "request_id": "original-prompt",
        }
        original = await manager.handle(intent)
        run_id = original["run_id"]

        def query():
            return manager.acp.query(run_id)

        first = await observe(lambda: query().get("permission_requests"))
        original_question = first[0]["permission_id"]
        await manager.close()

        manager = reopen()
        same_resource = await manager.handle(
            {
                "method": "acp_peer_open",
                "args": {"agent_id": "fixture"},
                "request_id": "original:peer",
            }
        )
        assert (
            same_resource["peer_id"] == peer_id and same_resource["session_id"] == "fixture-session"
        )
        assert manager.queue.empty()
        replay = await manager.handle(intent)
        assert replay["run_id"] == run_id
        assert replay["permission_requests"][0]["permission_id"] == original_question
        seen = set()
        for index in range(2):
            questions = await observe(
                lambda: [
                    question
                    for question in query()["permission_requests"]
                    if question["permission_id"] not in seen
                ]
            )
            question = questions[0]
            seen.add(question["permission_id"])
            response = await manager.handle(
                {
                    "method": "acp_permission_reply",
                    "args": {
                        "run_id": run_id,
                        "permission_id": question["permission_id"],
                        "option_id": "yes",
                    },
                    "request_id": f"reply:{index}",
                }
            )
            assert response["status"] in {"running", "succeeded"}

        def settled():
            manager.acp.refresh()
            value = manager.acp.query(run_id)
            return value if not value["pending"] else None

        result = await observe(settled)
        assert result["status"] == "succeeded"
        assert len(seen) == 2
        assert len(result["events"]) == 4
        canonical = manager.completions.pending()["events"]
        assert len(canonical) == 1 and canonical[0]["result"] == result["completion"]
        manager.completions.acknowledge(run_id)
        assert (await manager.handle(intent))["completion"] == result["completion"]
        assert not manager.completions.pending()["events"]
        large = await manager.handle(
            {
                "method": "acp_prompt_submit",
                "args": {"peer_id": peer_id, "prompt": "large"},
                "request_id": "large-prompt",
            }
        )
        run_id = large["run_id"]
        full = await observe(settled)
        assert full["events"][0]["payload"]["content"]["text"] == "雪" * 100000
        methods = [
            json.loads(line)["method"]
            for line in (agent_files / "calls.jsonl").read_text().splitlines()
        ]
        assert methods.count("initialize") == methods.count("session/new") == 1
        assert methods.count("session/prompt") == 2
        assert manager.acp.view(peer_id)["status"] == "running"
        closed = await manager.handle(
            {"method": "acp_peer_close", "args": {"peer_id": peer_id}, "request_id": "close"}
        )
        assert closed["admitted"] and closed["closing"]
        _stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0
        assert b"separate stderr diagnostic" in stderr
        manager.finish(peer_id, "succeeded", {"exit_code": process.returncode})
        manager.acp.refresh()
        close_result = await manager.handle(
            {"method": "acp_prompt_query", "args": {"request_id": "close"}}
        )
        assert close_result["status"] == "succeeded" and close_result["result"]["closed"]
    finally:
        if process is not None and process.returncode is None:
            manager.acp.control("close", {"peer_id": peer_id}, "cleanup-close")
            await asyncio.wait_for(process.communicate(), 10)
        await manager.close()


@pytest.mark.skipif(os.name != "posix", reason="Real local Manager Unix socket")
async def test_main_work_delegates_to_real_manager_and_delivers_original_result(database, tmp_path):
    agent_files = tmp_path / "agent"
    agent_files.mkdir()
    fixture = Path(__file__).parents[1] / "support" / "acp_host_agent.py"
    source = Path(__file__).parents[2] / "src" / "yuki" / "sandbox" / "acp_peer.py"
    config = tmp_path / "agents.toml"
    config.write_text(
        "[agents.fixture]\n"
        f"command={json.dumps(sys.executable)}\n"
        f"args={json.dumps([str(fixture), str(agent_files)])}\n"
        f"cwd={json.dumps(str(agent_files))}\n",
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
    socket = tmp_path / "s"
    server = await asyncio.start_unix_server(manager.serve, str(socket), limit=262144)
    phase = 0
    original_run = None

    def response(request):
        nonlocal phase, original_run
        phase += 1
        if phase == 1:
            name, args = "task_control", {"action": "accept", "goal": "Read actual external result"}
        elif phase == 2:
            name, args = (
                "external_agent",
                {"action": "prompt", "agent_id": "fixture", "prompt": "text"},
            )
        elif phase == 3:
            original_run = manager.db.execute(
                "SELECT id FROM environment_jobs WHERE kind='acp_prompt'"
            ).fetchone()[0]
            name, args = "task_control", {"action": "wait", "run_id": original_run}
        elif phase == 4:
            name, args = "external_agent", {"action": "get", "run_id": original_run}
        elif phase == 5:
            assert "EXTERNAL_AGENT_ORIGINAL_RESULT" in str(request.messages)
            name, args = (
                "send",
                {"text": "External result was read through its original receipt."},
            )
        elif phase == 6:
            name, args = (
                "task_control",
                {"action": "complete", "result": "Original result delivered"},
            )
        else:
            raise AssertionError(f"Unexpected model request {phase}")
        return ChatResponse(
            content="",
            latency_seconds=0,
            tool_calls=(ToolCall(str(uuid4()), ToolFunction(name, json.dumps(args))),),
        )

    provider = FakeLLMProvider(response, delay_seconds=0.2)
    app, env, message, sender = await _deployment(
        database, tmp_path, provider, sandbox_socket=socket
    )
    process = None
    peer_id = None
    try:
        assert (await app.processor.handle(message, sender)).reason == "chat"
        repository = WorkRepository(database)
        async with database.sessions() as reader:
            waiting = dict((await reader.execute(select(work))).mappings().one())
            task = (await reader.scalars(select(SandboxTaskRunModel))).one()
        assert phase == 3 and waiting["state"] == "waiting_external"
        assert task.run_id == original_run and task.status == "waiting"
        assert json.loads(task.source_json)["work_id"] == waiting["id"]
        peer_id = json.loads(task.progress_json)["peer_id"]
        assert manager.queue.get_nowait() == peer_id
        args = json.loads(manager.acp.row(peer_id)["payload"])
        manager.prepare_spec(peer_id, args, "acp_peer")
        with manager.db:
            manager.db.execute("UPDATE environment_jobs SET dispatched=1 WHERE id=?", (peer_id,))
            manager.db.execute("UPDATE jobs SET state='running' WHERE id=?", (peer_id,))
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(source),
            peer_id,
            "--root",
            str(manager.runtime_root),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await observe(lambda: (manager.acp.root(peer_id) / "peer_metadata.json").exists())
        manager.acp.refresh()

        def completed():
            manager.acp.refresh()
            result = manager.acp.query(original_run)
            return result if not result["pending"] else None

        native = await observe(completed)
        assert native["status"] == "succeeded"
        assert native["events"][0]["payload"]["content"]["text"] == "EXTERNAL_AGENT_ORIGINAL_RESULT"
        assert await app.sandbox_completions.drain_once() == 1
        await app.work_scheduler.poll_waits_once()
        queued = await repository.get(waiting["id"])
        assert queued["state"] == "queued"
        await app.work_scheduler.drain_once()
        final = await repository.get(waiting["id"])
        assert final["state"] == "completed" and phase == 6
        assert final["id"] == waiting["id"] and final["source_key"] == waiting["source_key"]
        assert not manager.completions.pending()["events"]
        assert manager.acp.view(peer_id)["status"] == "running"
        async with database.sessions() as reader:
            saved = (await reader.scalars(select(SandboxTaskRunModel))).one()
            assert saved.run_id == original_run and saved.status == "completed"
            assert json.loads(saved.completion_json) == native["completion"]
            operation = (await reader.scalars(select(SocialOperationModel))).one()
            assert operation.action == "send_message" and operation.status == "succeeded"
            assert operation.event_id is not None
            settled_effects = (await reader.execute(select(effects))).mappings().all()
            assert all(effect["state"] == "accepted" for effect in settled_effects)
        assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1
        methods = [
            json.loads(line)["method"]
            for line in (agent_files / "calls.jsonl").read_text().splitlines()
        ]
        assert (
            methods.count("initialize")
            == methods.count("session/new")
            == methods.count("session/prompt")
            == 1
        )
    finally:
        if process is not None and process.returncode is None:
            manager.acp.control("close", {"peer_id": peer_id}, "test-close")
            await asyncio.wait_for(process.communicate(), 10)
        assert not await app.lifecycle._close_entries(app.lifecycle._entries)
        server.close()
        await server.wait_closed()
        await manager.close()
