"""V02/V09 focused real loopback TLS + subprocess evidence; no production traffic."""

import asyncio
import json
import sys

from sqlalchemy import select
from tests.integration.test_external_agent_bridge import bridge as bridge
from tests.integration.test_local_device_transport import actual_connector, python_command
from tests.integration.test_subagent_common_capabilities import _message_child
from tests.support import automation_unified_delivery_helpers
from tests.support.runtime_execution import make_child_executor

from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.runtime.work_schema_v1 import inputs
from yuki.runtime.work_wait import WorkWaitRepository
from yuki.sandbox.client import SandboxClient
from yuki.sandbox.continuation_worker import SandboxContinuationWorker
from yuki.sandbox.db_models import SandboxTaskRunModel

pytest_plugins = ["tests.conftest"]


def response(name, arguments, key):
    return ChatResponse(
        "", 0, tool_calls=(ToolCall(key, ToolFunction(name, json.dumps(arguments))),)
    )


async def test_v02_two_independent_unicode_special_cwds_real_read_write_and_exit(
    bridge, tmp_path, monkeypatch
):
    async with actual_connector(bridge, tmp_path, monkeypatch) as connector:
        client = SandboxClient(tmp_path / "no-manager.sock", tasks=bridge.service.tasks)
        client.local_devices = bridge.service.local_devices
        script = tmp_path / "执行 脚本 '[]$.py"
        input_name = "输入 ' [x] $.txt"
        output_name = "输出 ' [y] $.bin"
        script.write_text(
            "from pathlib import Path\nimport sys\n"
            f"data=Path({input_name!r}).read_bytes()\n"
            f"Path({output_name!r}).write_bytes(data+b'\\x00'+str(Path.cwd()).encode())\n"
            "print('cwd='+str(Path.cwd()),flush=True)\n"
            "sys.exit(int(Path('exit-code').read_text()))\n",
            encoding="utf-8",
        )
        records = []
        for index, (name, code) in enumerate((("甲 目录 '[]$", 0), ("乙 目录 '#&$", 7))):
            directory = tmp_path / name
            directory.mkdir()
            original = f"原始文件-{index}-雪".encode()
            (directory / input_name).write_bytes(original)
            (directory / "exit-code").write_text(str(code))
            request_id = f"v02-original-{index}"
            started = await client.execute(
                "terminal_exec",
                {
                    "device_id": bridge.device["device_id"],
                    "command": python_command(script)
                    + ("; exit $LASTEXITCODE" if sys.platform == "win32" else ""),
                    "cwd": str(directory),
                },
                request_id=request_id,
                source={
                    **bridge.source,
                    "conversation_id": bridge.env.context.conversation_id,
                    "parent_execution_id": "isolated-v02",
                },
            )
            await asyncio.wait_for(connector.terminal.runs[started["run_id"]].done.wait(), 12)
            page = await bridge.http.get(
                f"/agents/v1/terminals/{started['run_id']}", headers=bridge.headers
            )
            assert page.status_code == 200, page.text
            observed = page.json()
            assert observed["exit_code"] == code
            assert observed["status"] == ("succeeded" if code == 0 else "failed")
            assert str(directory.resolve()) in observed["output"]
            assert (directory / output_name).read_bytes() == (
                original + b"\x00" + str(directory.resolve()).encode()
            )
            stored = await bridge.service.tasks.get(request_id)
            assert stored.status == "completed"
            assert json.loads(stored.completion_json)["exit_code"] == code
            assert "output" not in json.loads(stored.progress_json)
            records.append({"run_id": started["run_id"], "cwd": str(directory), "exit_code": code})
        assert len({item["run_id"] for item in records}) == 2
        assert not connector.drivers
        print("V02_REAL_EVIDENCE=" + json.dumps(records, ensure_ascii=False))


async def test_v09_real_child_local_shell_original_source_wait_continuation_and_read(
    bridge, database, tmp_path, monkeypatch
):
    async def original_env(_database, _tmp_path):
        return bridge.env

    monkeypatch.setattr(automation_unified_delivery_helpers, "social_env", original_env)
    case, repository, parent, child = await _message_child(database, tmp_path)
    assert case.env.person == bridge.env.person
    client = SandboxClient(tmp_path / "no-manager.sock", tasks=bridge.service.tasks)
    client.local_devices = bridge.service.local_devices
    case.chat._tools.sandbox_client = client
    executor = make_child_executor(
        repository,
        chat=case.chat,
        config=case.chat._runtime_config,
        runner=case.chat.runtime.runner,
        load_tools=case.chat.runtime.runner.main_contract.definitions,
        sandbox_tasks=bridge.service.tasks,
        sandbox_client=client,
        router=case.env.router,
    )
    directory = tmp_path / "子 Agent 原工作目录 '[]$"
    directory.mkdir()
    release = directory / "release"
    marker = directory / "starts"
    script = directory / "真实 子任务.py"
    script.write_text(
        "import time\nfrom pathlib import Path\n"
        f"with Path({str(marker)!r}).open('ab') as f: f.write(b'x')\n"
        "print('child-start-雪',flush=True)\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.02)\n"
        "print('child-finished-雪',flush=True)\n",
        encoding="utf-8",
    )
    observed = {}

    def respond(request):
        index = len(case.provider.requests) - 1
        if index == 0:
            return response(
                "terminal_exec",
                {
                    "device_id": bridge.device["device_id"],
                    "command": python_command(script),
                    "cwd": str(directory),
                },
                "child-original-exec",
            )
        if index == 1:
            result = json.loads(request.messages[-1].content)
            assert result["ok"], result
            observed["run_id"] = result["data"]["run_id"]
            return response(
                "task_control",
                {
                    "action": "wait",
                    "conditions": [{"kind": "owned_run", "run_id": observed["run_id"]}],
                },
                "child-original-wait",
            )
        if index == 2:
            return response("terminal_read", {"run_id": observed["run_id"]}, "child-original-read")
        assert index == 3
        result = json.loads(request.messages[-1].content)
        assert result["ok"], result
        assert "child-finished-雪" in result["data"]["output"]
        observed["model_observed_real_output"] = True
        return response(
            "task_control",
            {"action": "complete", "result": "real local child verified"},
            "child-complete",
        )

    case.provider._responder = respond
    async with actual_connector(bridge, tmp_path, monkeypatch) as connector:
        parent_before = await repository.get(parent["id"])
        assert await executor.run(child) is None
        waiting = await repository.get(child)
        assert waiting["state"] == "waiting_external"
        wait = await WorkWaitRepository(repository).describe(child)
        async with database.read_session() as reader:
            stored = await reader.scalar(
                select(SandboxTaskRunModel).where(SandboxTaskRunModel.run_id == observed["run_id"])
            )
        assert stored is not None
        source = json.loads(stored.source_json)
        assert source["work_id"] == child
        assert source["actor_person_id"] == bridge.env.person
        assert source["conversation_id"] == parent["conversation_id"]
        assert source["terminal_target"]["device_id"] == bridge.device["device_id"]
        request_id = stored.request_id
        release.write_bytes(b"release original child")
        await asyncio.wait_for(connector.terminal.runs[observed["run_id"]].done.wait(), 12)
        async with asyncio.timeout(8):
            # Observe the independently delivered WSS completion's real DB commit.
            while (await bridge.service.tasks.get(request_id)).status != "completed":  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        await SandboxContinuationWorker(repository).drain_once()
        assert await WorkWaitRepository(repository).deliver_due() == 1
        queued = await repository.get(child)
        assert queued["state"] == "queued"
        assert queued["model_requests"] == waiting["model_requests"]
        assert queued["tool_calls"] == waiting["tool_calls"]
        assert await executor.run(child) is None
        completed = await repository.get(child)
        assert completed["state"] == "completed"
        assert completed["model_requests"] > waiting["model_requests"] > 0
        assert completed["tool_calls"] > waiting["tool_calls"] > 0
        assert observed["model_observed_real_output"]
        assert marker.read_bytes() == b"x"
        assert await repository.get(parent["id"]) == parent_before
        async with database.read_session() as reader:
            keys = list(
                await reader.scalars(select(inputs.c.source_key).where(inputs.c.work_id == child))
            )
        assert keys.count("completion:" + request_id) == 1
        assert keys.count("wait:" + wait["wait_id"]) == 1
        assert not any(action.startswith("send_") for action, _ in case.env.bot.calls)
        assert not connector.drivers
        print(
            "V09_REAL_EVIDENCE="
            + json.dumps(
                {
                    "child_work_id": child,
                    "parent_work_id": parent["id"],
                    "run_id": observed["run_id"],
                    "request_id": request_id,
                    "device_id": bridge.device["device_id"],
                    "original_starts": 1,
                    "before_usage": {
                        k: waiting[k] for k in ("model_requests", "tool_calls", "sent_messages")
                    },
                    "after_usage": {
                        k: completed[k] for k in ("model_requests", "tool_calls", "sent_messages")
                    },
                    "model_calls_are_fake": True,
                    "real_qq_calls": 0,
                    "fixture_gateway_actions": [action for action, _ in case.env.bot.calls],
                },
                ensure_ascii=False,
            )
        )
