"""Scripted model inputs use the current Work/terminal/send contracts and real receipts."""

import asyncio
import json
import shlex
import sys
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from tests.support.workspace_snapshots import snapshot_bytes

from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.runtime.work_schema_v1 import inputs, work
from yuki.sandbox.db_models import SandboxTaskRunModel


class DesktopComputerWorkScenario:
    def __init__(self, root, container, computer):
        self.root, self.container, self.computer = root, container, computer
        self.scenario = None

    def respond(self, request):
        if self.scenario is None:
            return None
        state = self.scenario
        state["phase"] += 1
        phase = state["phase"]
        if phase == 1:
            name, arguments = "task_control", {"action": "accept", "goal": state["goal"]}
        elif phase == 2:
            if getattr(self.computer, "external", False):
                program = Path(__file__).with_name("desktop_browser_work.py").read_text("utf-8")
                python = sys.executable if getattr(self.computer, "headed", False) else "python"
                command = shlex.join(
                    [python, "-u", "-c", program]
                    + (["wait-control"] if state["wait_control"] else [])
                )
                cwd = "/workspace"
            else:
                cli = Path(__file__).parents[2] / "src/yuki/sandbox/yuki_computer.py"
                command = shlex.join(
                    [
                        sys.executable,
                        "-I",
                        str(cli),
                        "help-request",
                        "--note",
                        "請協助原工作的畫面步驟",
                    ]
                )
                cwd = str(self.root)
            name, arguments = "terminal_exec", {"command": command, "cwd": cwd}
        elif phase == 3:
            receipt = json.loads(request.messages[-1].content)
            state["run_id"] = receipt["data"]["run_id"]
            name, arguments = (
                "task_control",
                {
                    "action": "wait",
                    "conditions": [{"kind": "owned_run", "run_id": state["run_id"]}],
                },
            )
        elif phase == 4:
            name, arguments = "terminal_read", {"run_id": state["run_id"]}
        elif phase == 5:
            assert "畫面處理完成，請沿原工作接續。" in request.messages[-1].content
            name = "send"
            arguments = {"attachment_kind": "file", "text": "原電腦工作成果"}
            if getattr(self.computer, "external", False):
                arguments.update(path="original-computer.md", name="原電腦成果.md")
            else:
                arguments["artifact_id"] = state["artifact"]["artifact_id"]
        elif phase == 6:
            if getattr(self.computer, "external", False):
                store = self.container.workspace_service.store
                with store._transaction(write=False) as manifest:
                    row = manifest.execute(
                        "SELECT a.id,a.sha256 FROM artifacts a JOIN artifact_snapshots s "
                        "ON s.id=a.id WHERE a.name='原電腦成果.md' "
                        "ORDER BY a.created_at DESC LIMIT 1"
                    ).fetchone()
                assert row is not None
                state["artifact"] = {"artifact_id": row["id"], "sha256": row["sha256"]}
            name, arguments = (
                "task_control",
                {
                    "action": "complete",
                    "result": "已讀原交回回執並交付原版本成果",
                },
            )
        else:
            return None
        return ChatResponse(
            content="",
            latency_seconds=0,
            tool_calls=(ToolCall(str(uuid4()), ToolFunction(name, json.dumps(arguments))),),
        )

    def attach(self, app):
        @app.post("/__test/computer-work-scenario")
        async def setup(goal: str = "原電腦協助工作", wait_control: bool = False):
            assert self.computer.native_work
            assert not wait_control or getattr(self.computer, "external", False)
            artifact = None
            if not getattr(self.computer, "external", False):
                artifact = await asyncio.to_thread(
                    snapshot_bytes,
                    self.container.workspace_service.store,
                    "原電腦成果.md",
                    b"# Original computer artifact\nDelivered after the original return receipt.\n",
                )
            self.scenario = {
                "phase": 0,
                "run_id": None,
                "artifact": artifact,
                "goal": goal,
                "wait_control": wait_control,
            }
            return artifact

        @app.get("/__test/computer-work-facts")
        async def facts():
            async with self.container.database.read_session() as reader:
                mailbox = [
                    dict(row)
                    for row in (
                        await reader.execute(select(inputs).order_by(inputs.c.id))
                    ).mappings()
                ]
                work_ids = list((await reader.execute(select(work.c.id))).scalars())
                runs = [
                    {"run_id": row.run_id, "status": row.status}
                    for row in (await reader.scalars(select(SandboxTaskRunModel))).all()
                ]
            return {
                "phase": self.scenario["phase"] if self.scenario else None,
                "run_id": self.scenario["run_id"] if self.scenario else None,
                "work_ids": work_ids,
                "inputs": mailbox,
                "runs": runs,
                "artifact": self.scenario["artifact"] if self.scenario else None,
                **await self.computer.work_facts(),
            }
