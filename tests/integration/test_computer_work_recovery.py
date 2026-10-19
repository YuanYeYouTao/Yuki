"""Real Main -> CLI/Supervisor -> Manager -> original Work, across process crashes."""

import asyncio
import json
import os
import shlex
import signal
import sqlite3
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from tests.integration.test_acp_manager_peer import observe
from tests.integration.test_current_material_main_contract import _catalog, _deployment, _profile
from tests.support.canonical_identity import ensure_person

from yuki.character.repository import CharacterRepository
from yuki.client_api.http import attach_client_api
from yuki.container import ApplicationContainer
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.pool import ModelClientPool
from yuki.runtime.work_budget_schema import budgets
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import inputs, work
from yuki.sandbox.db_models import SandboxTaskContinuationModel, SandboxTaskRunModel

CLI = Path(__file__).parents[2] / "src/yuki/sandbox/yuki_computer.py"
NOTE = "Owner completed only the screen step, not an answer from the requester."


class ManagerProcess:
    def __init__(self, root):
        self.root = root
        self.short = tempfile.TemporaryDirectory(prefix="yc-original-", dir="/tmp")
        self.socket = Path(self.short.name) / "m.sock"
        self.process = None
        self.log = (root / "manager-process.log").open("ab")

    async def start(self):
        ready = self.root / "manager-ready"
        ready.unlink(missing_ok=True)
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.support.native_computer_manager",
            str(self.root),
            str(self.socket),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=self.log,
            stderr=self.log,
        )
        await observe(ready.exists)
        data = json.loads(ready.read_text())
        assert data["pid"] == self.process.pid
        self.runtime = Path(data["runtime_root"])

    async def close(self):
        if self.process.returncode is None:
            self.process.terminate()
            await asyncio.wait_for(self.process.wait(), 10)
        # Only this fixture's recorded independent Supervisor/CLI groups.
        for state in self.runtime.glob("*/state/status.json"):
            value = json.loads(state.read_text())
            if value["status"] == "running":
                for field in ("pid", "supervisor_pid"):
                    try:
                        if field == "pid":
                            os.killpg(value[field], signal.SIGKILL)
                        else:
                            os.kill(value[field], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        self.log.close()
        self.short.cleanup()

    def facts(self, query, parameters=()):
        with sqlite3.connect(self.root / "manager/jobs.sqlite3") as database:
            database.row_factory = sqlite3.Row
            return database.execute(query, parameters).fetchall()


@pytest.mark.parametrize("kind", ["help-request", "wait-control"])
@pytest.mark.parametrize("crash", [False, True])
async def test_computer_return_resumes_original_work_once_with_duplicate_completion(
    database, tmp_path, monkeypatch, kind, crash
):
    await computer_work_recovery_case(database, tmp_path, monkeypatch, kind, crash)


async def computer_work_recovery_case(
    database,
    tmp_path,
    monkeypatch,
    kind,
    crash,
    *,
    prompt_change_at=None,
):
    manager = ManagerProcess(tmp_path)
    await manager.start()
    phase, original_run = 0, None

    def response(request):
        nonlocal phase, original_run
        phase += 1
        if prompt_change_at is not None:
            prefix = "\n".join(
                message.content or "" for message in request.messages if message.role == "system"
            )
            new = prompt_change_at == "before" or phase > 3
            assert ("NEW_SYSTEM" if new else "OLD_SYSTEM") in prefix
            assert ("NEW_PERSONA" if new else "OLD_PERSONA") in prefix
            assert ("OLD_SYSTEM" if new else "NEW_SYSTEM") not in prefix
        if phase == 1:
            name, args = (
                "task_control",
                {"action": "accept", "goal": "Resume the original computer work"},
            )
        elif phase == 2:
            command = [sys.executable, "-I", str(CLI), kind]
            if kind == "help-request":
                command += ["--note", "Please handle the screen"]
            name, args = "terminal_exec", {"command": shlex.join(command), "cwd": str(tmp_path)}
        elif phase == 3:
            original_run = manager.facts(
                "SELECT id FROM environment_jobs WHERE kind='terminal_exec'"
            )[0][0]
            name, args = (
                "task_control",
                {"action": "wait", "conditions": [{"kind": "owned_run", "run_id": original_run}]},
            )
        elif phase == 4:
            name, args = "terminal_read", {"run_id": original_run}
        elif phase == 5:
            assert request.messages[-1].role == "tool" and NOTE in str(request.messages[-1].content)
            name, args = "send_message", {"text": "Original computer receipt received."}
        elif phase == 6:
            name, args = "task_control", {"action": "complete", "result": "Original work finished"}
        else:
            raise AssertionError(f"Unexpected additional model call {phase}")
        return ChatResponse(
            content="",
            latency_seconds=0,
            tool_calls=(ToolCall(str(uuid4()), ToolFunction(name, json.dumps(args))),),
        )

    prompt_sources = {}
    if prompt_change_at is not None:
        system, persona = tmp_path / "hot-system.md", tmp_path / "hot-persona.md"
        system.write_text("OLD_SYSTEM\n{{YUKI_PERSONA_CORE}}", encoding="utf-8")
        persona.write_text("OLD_PERSONA", encoding="utf-8")
        prompt_sources = {"system_prompt_file": system, "bot_persona_file": persona}
    app, env, message, sender = await _deployment(
        database,
        tmp_path,
        FakeLLMProvider(response),
        sandbox_socket=manager.socket,
        **prompt_sources,
    )
    identity = app.client_api.identity
    await app.app_gateway.start()
    await app.client_computer.start()
    api = FastAPI()
    attach_client_api(api, lambda: app.client_api)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://test"
    ) as http:
        try:
            async with database.immediate_session() as writer:
                helper = await ensure_person(writer, "computer-helper", display_name="Helper")
            await CharacterRepository(identity).set_owner(
                str(uuid4()), helper, str(uuid4()), granted=True
            )

            async def paired(person):
                code = await identity.issue_for_operator(person, str(uuid4()), str(uuid4()))
                device = await identity.pair(code["code"], "original helper device")
                return device, {"Authorization": "Bearer " + device["token"]}

            _device, headers = await paired(helper)

            async def change_prompts():
                current = await http.get("/client/v1/character/prompts", headers=headers)
                assert current.status_code == 200, current.text
                for document in current.json()["data"]["documents"]:
                    content = (
                        "NEW_SYSTEM\n{{YUKI_PERSONA_CORE}}"
                        if document["key"] == "system_prompt"
                        else "NEW_PERSONA"
                    )
                    result = await http.put(
                        f"/client/v1/character/prompts/{document['key']}",
                        headers=headers,
                        json={
                            "request_id": str(uuid4()),
                            "expected_revision": document["revision"],
                            "content": content,
                        },
                    )
                    assert result.status_code == 200, result.text
                assert app.settings.bot_persona == "NEW_PERSONA"

            if prompt_change_at == "before":
                await change_prompts()
            taken = await http.post(
                "/client/v1/computer/control",
                headers=headers,
                json={"request_id": str(uuid4()), "action": "take"},
            )
            assert taken.status_code == 200, taken.text
            lease = taken.json()["data"]["control"]["lease_id"]
            assert (await app.processor.handle(message, sender)).reason == "chat"
            async with database.sessions() as reader:
                waiting = dict((await reader.execute(select(work))).mappings().one())
                task = (await reader.scalars(select(SandboxTaskRunModel))).one()
                original_budget = dict(
                    (
                        await reader.execute(
                            select(budgets).where(budgets.c.root_id == waiting["id"])
                        )
                    )
                    .mappings()
                    .one()
                )
                original_inputs = list((await reader.execute(select(inputs))).mappings())
            assert phase == 3 and waiting["state"] == "waiting_external"
            assert task.run_id == original_run
            assert json.loads(task.source_json)["work_id"] == waiting["id"]
            requester = json.loads(waiting["source_json"])["actor_person_id"]
            assert requester != helper
            await observe(
                lambda: manager.facts(
                    "SELECT 1 FROM help_requests WHERE run_id=? AND result IS NULL", (original_run,)
                )
            )
            await observe(lambda: app.client_computer.run_kind(original_run, waiting["id"]))
            _requester_device, requester_headers = await paired(requester)
            before_steps = await http.get(
                f"/client/v1/works/{waiting['id']}/steps", headers=requester_headers
            )
            assert before_steps.status_code == 200, before_steps.text
            initial_steps = [
                step
                for step in before_steps.json()["data"]["steps"]
                if step["category"] == "computer"
            ]
            assert len(initial_steps) == 1
            original_step = initial_steps[0]["step_id"]
            current_work = await http.get(
                f"/client/v1/works/{waiting['id']}", headers=requester_headers
            )
            assert current_work.status_code == 200, current_work.text
            projected = current_work.json()["data"]
            assert projected["help_requested"] is (kind == "help-request")
            assert projected["wait"]["conditions"][0]["run_kind"] == (
                "help_request" if kind == "help-request" else "wait_control"
            )
            if prompt_change_at == "waiting":
                await change_prompts()
            # A transfer must not finish the original wait-control run.
            other, other_headers = await paired(helper)
            transferred = await http.post(
                "/client/v1/computer/control",
                headers=other_headers,
                json={"request_id": str(uuid4()), "action": "transfer", "lease_id": lease},
            )
            assert transferred.status_code == 200, transferred.text
            final_lease = transferred.json()["data"]["control"]["lease_id"]
            assert final_lease != lease
            assert (
                manager.facts("SELECT state FROM jobs WHERE id=?", (original_run,))[0][0]
                == "running"
            )
            command = {
                "request_id": str(uuid4()),
                "action": "return",
                "lease_id": final_lease,
                "note": NOTE,
            }
            if crash:
                (tmp_path / "crash-after-return").touch()
            returned = await http.post(
                "/client/v1/computer/control", headers=other_headers, json=command
            )
            if crash:
                assert (
                    returned.status_code == 409 and returned.json()["problem"]["code"] == "unknown"
                ), returned.text
                await asyncio.wait_for(manager.process.wait(), 10)
                assert manager.process.returncode == 73
                assert not (
                    manager.runtime / "computer/results" / (original_run + ".json")
                ).exists()
                settings = app.settings.model_copy(deep=True)
                assert not await app.lifecycle._close_entries(app.lifecycle._entries)
                # Rebuild services around the persisted ledger; never re-ingest
                # the original user event with a new fixture timestamp.
                app = ApplicationContainer(settings, database=database)
                app.models.apply_catalog(
                    _catalog(_profile()),
                    ModelClientPool(injected_profiles={"main": FakeLLMProvider(response)}),
                )
                app.gateway_registry.connect(
                    env.bot, provider_id="snowluma", presence_id=env.presence
                )
                await app.model_invocations.writer.start()
                await app._freeze_main_manifest()
                await app.app_gateway.start()
                await app.client_computer.start()
                await app.client_api.identity.receipts.recover_interrupted_controls()
                await manager.start()
                returned = await http.post(
                    "/client/v1/computer/control", headers=other_headers, json=command
                )
            assert returned.status_code == 200, returned.text
            assert returned.json()["data"]["by"]["device_id"] == other["device_id"]
            await observe(
                lambda: manager.facts(
                    "SELECT 1 FROM jobs WHERE id=? AND state='succeeded'", (original_run,)
                )
            )
            result = json.loads(
                (manager.runtime / "computer/results" / (original_run + ".json")).read_text()
            )
            assert result["lease_id"] == final_lease and result["note"] == NOTE
            event = json.loads(
                manager.facts(
                    "SELECT payload FROM completion_outbox WHERE run_id=?", (original_run,)
                )[0][0]
            )
            assert event["result"]["step_label"] == "computer"
            original_execute = app.sandbox_client.execute
            lost_ack = True

            async def uncertain_ack(name, args, **kwargs):
                nonlocal lost_ack
                if name == "ack_code_completion" and lost_ack:
                    lost_ack = False
                    return {"error": "sandbox_unavailable"}
                return await original_execute(name, args, **kwargs)

            monkeypatch.setattr(app.sandbox_client, "execute", uncertain_ack)
            with pytest.raises(RuntimeError, match="ack"):
                await app.sandbox_completions.drain_once()
            repository = WorkRepository(database)
            await app.work_scheduler.poll_waits_once()
            assert (await repository.get(waiting["id"]))["state"] == "queued"
            await app.work_scheduler.drain_once()
            final = await repository.get(waiting["id"])
            assert final["state"] == "completed" and phase == 6
            assert (
                final["source_key"] == waiting["source_key"]
                and final["source_json"] == waiting["source_json"]
            )
            assert final["model_requests"] == waiting["model_requests"] + 3
            assert final["tool_calls"] == waiting["tool_calls"] + 2
            assert manager.facts("SELECT COUNT(*) FROM jobs")[0][0] == 1
            assert await app.sandbox_completions.drain_once() == 1
            await app.sandbox_tasks.receive(event)
            await app.sandbox_tasks.receive(event)
            await app.work_scheduler.poll_waits_once()
            await app.work_scheduler.drain_once()
            assert await repository.get(waiting["id"]) == final and phase == 6
            async with database.sessions() as reader:
                final_budget = dict(
                    (
                        await reader.execute(
                            select(budgets).where(budgets.c.root_id == waiting["id"])
                        )
                    )
                    .mappings()
                    .one()
                )
                assert final_budget["root_id"] == original_budget["root_id"]
                assert final_budget["model_limit"] == original_budget["model_limit"]
                assert final_budget["tool_limit"] == original_budget["tool_limit"]
                assert final_budget["models"] == original_budget["models"] + 3
                assert final_budget["tools"] == original_budget["tools"] + 2
                mailbox = list((await reader.execute(select(inputs))).mappings())
                wakeups = [
                    item
                    for item in mailbox
                    if item["id"] not in {row["id"] for row in original_inputs}
                ]
                assert len(wakeups) == 1
                wakeup = wakeups[0]
                assert wakeup["kind"] == "control" and wakeup["state"] == "consumed"
                assert wakeup["event_id"] is None and wakeup["work_id"] == waiting["id"]
                payload = json.loads(wakeup["payload_json"])
                assert (
                    payload["signal"] is True
                    and NOTE not in payload["text"]
                    and helper not in payload["text"]
                )
                signal = json.loads(payload["text"])
                assert signal["kind"] == "work_signal"
                assert signal["conditions"] == [
                    {
                        "kind": "owned_run",
                        "run_id": original_run,
                        "matched": {
                            "kind": "owned_run",
                            "run_id": original_run,
                            "status": "succeeded",
                        },
                    }
                ]
                assert (
                    await reader.scalar(
                        select(func.count()).select_from(SandboxTaskContinuationModel)
                    )
                    == 1
                )
            public_steps = await http.get(
                f"/client/v1/works/{waiting['id']}/steps", headers=requester_headers
            )
            assert public_steps.status_code == 200, public_steps.text
            same_step = [
                step
                for step in public_steps.json()["data"]["steps"]
                if step["step_id"] == original_step
            ]
            assert len(same_step) == 1
            assert same_step[0]["category"] == "computer" and same_step[0]["status"] == "succeeded"
            assert not manager.facts("SELECT * FROM completion_outbox")
            assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1
        finally:
            assert not await app.lifecycle._close_entries(app.lifecycle._entries)
            await manager.close()
