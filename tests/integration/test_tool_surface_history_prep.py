"""Tool-surface switch preparation: wire names and original receipts stay fixed.

Failure cases written before the implementation:

F1. A model environment tool reaches the Manager under a different RPC method
    name, or an environment tool has no RPC method and fails at dispatch.
F2. The terminal_exec task row is prepared from a different ``{"tool": ...}``
    payload, so an original request ID conflicts on retry.
F3. A submitted RPC loses or gains the uncertain flag when the socket fails
    after submission (mutating vs read methods).
F4. Old-name receipts (send_message, web_read, local_device) project to another
    Desktop category, label or status.
F5. Current and retired names (terminal_*, web_search, workspace_*, memory,
    external_agent, run_python, task_control) change projection or status.
F6. An owned-run wait reports another run kind.
F7. A recovery read of an old-name receipt without typed outcome loses its
    delivered artifacts or message, or rewrites the original tool name.
F8. Code Mode wrapper names of the environment and workspace tools change.
F9. A history-map name becomes a declared (callable) tool.

The run writes ``test-results/tool-surface-prep/history-and-rpc.json``.
"""

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import insert
from tests.conftest import make_settings
from tests.support.service_dependencies import tool_dependencies
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_client_api import bearer
from tests.unit.test_client_works import owner, seeded

from yuki.capabilities.invocation import ToolInvocationContext, current_invocation
from yuki.codemode.api_projection import project
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.memory.repository import MemoryFactRepository
from yuki.memory.service import MemoryFactService
from yuki.persistence.repositories import EventLedgerRepository
from yuki.runtime.effect_outcomes import ResultCapture, current_result_capture
from yuki.runtime.work_journal import WorkJournal
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import effects
from yuki.runtime.work_wait_schema import waits
from yuki.sandbox.client import SandboxClient, sandbox_tools
from yuki.services.agent_tools import AgentToolService, ToolRuntime
from yuki.workspace.tools import workspace_tools

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Unix socket Manager transport")

admin = admin_fixture
app_env = app_fixture

ARTIFACT = Path(__file__).resolve().parents[2] / "test-results" / "tool-surface-prep"

# Fixed original receipts: (name, state, receipt, expected category, label, status).
HISTORY = [
    (
        "send_message_outcome",
        "accepted",
        {
            "invocation": {"tool_id": "send_message"},
            "outcome": {"tool": "send_message", "ok": True, "status": "succeeded"},
        },
        ("message", "傳送訊息", "succeeded"),
    ),
    (
        "send_message_legacy_result",
        "accepted",
        {
            "invocation": {"tool_id": "send_message"},
            "result": json.dumps(
                {
                    "ok": True,
                    "data": {
                        "file": {"status": "succeeded", "artifact_id": "art-original"},
                        "caption": {"status": "succeeded"},
                    },
                }
            ),
        },
        ("message", "傳送訊息", "succeeded"),
    ),
    (
        "send_message_without_invocation",
        "accepted",
        {"outcome": {"tool": "send_message", "ok": True}},
        ("message", "傳送訊息", "succeeded"),
    ),
    (
        "web_read",
        "accepted",
        {
            "invocation": {"tool_id": "web_read"},
            "outcome": {"ok": True, "side_effecting": False},
        },
        ("web", "讀網頁", "succeeded"),
    ),
    (
        "local_device_pending",
        "accepted",
        {
            "invocation": {"tool_id": "local_device"},
            "outcome": {"tool": "local_device", "ok": False, "pending": True, "status": "running"},
        },
        ("device", "本機操作（歷史）", "running"),
    ),
    (
        "terminal_exec_finished",
        "accepted",
        {
            "invocation": {"tool_id": "terminal_exec"},
            "outcome": {
                "ok": True,
                "status": "succeeded",
                "pending": False,
                "run_id": "run-terminal",
            },
        },
        ("terminal", "終端", "succeeded"),
    ),
    (
        "terminal_exec_unsettled",
        "accepted",
        {"invocation": {"tool_id": "terminal_exec"}, "outcome": {"ok": True}},
        ("terminal", "終端", "unknown"),
    ),
    (
        "terminal_read_browser",
        "accepted",
        {
            "invocation": {"tool_id": "terminal_read"},
            "outcome": {"ok": True, "side_effecting": False, "step_label": "browser"},
        },
        ("browser", "使用瀏覽器", "succeeded"),
    ),
    (
        "external_agent_prompt",
        "accepted",
        {
            "invocation": {"tool_id": "external_agent", "action": "prompt"},
            "outcome": {"ok": True, "status": "running", "run_id": "run-agent"},
        },
        ("agent", "委託其他 Agent", "running"),
    ),
    (
        "web_search",
        "accepted",
        {"invocation": {"tool_id": "web_search"}, "outcome": {"ok": True}},
        ("search", "搜尋", "succeeded"),
    ),
    (
        "read_webpage",
        "failed",
        {"invocation": {"tool_id": "read_webpage"}, "outcome": {"ok": False}},
        ("web", "讀網頁", "failed"),
    ),
    (
        "workspace_inspect",
        "accepted",
        {"invocation": {"tool_id": "workspace_inspect"}, "outcome": {"ok": True}},
        ("file", "讀取檔案", "succeeded"),
    ),
    (
        "save_attachment",
        "accepted",
        {
            "invocation": {"tool_id": "save_conversation_attachment_to_workspace"},
            "outcome": {"ok": True},
        },
        ("file", "保存檔案", "succeeded"),
    ),
    (
        "workspace_publish",
        "accepted",
        {
            "invocation": {"tool_id": "workspace_publish"},
            "outcome": {"ok": True},
            "result": {"artifact_id": "art-published", "version": "v1"},
        },
        ("file", "發布檔案", "succeeded"),
    ),
    (
        "memory_change",
        "accepted",
        {"invocation": {"tool_id": "memory_change"}, "outcome": {"ok": True}},
        ("memory", "記憶", "succeeded"),
    ),
    (
        "retired_run_python",
        "accepted",
        {"invocation": {"tool_id": "run_python"}, "outcome": {"ok": True}},
        ("other", "其他", "succeeded"),
    ),
    (
        "task_control_prepared",
        "prepared",
        {"invocation": {"tool_id": "task_control"}},
        ("other", "其他", "recorded"),
    ),
]

# (model tool, model arguments, Manager RPC method, wire arguments): the model
# surface was renamed at the cutover; the wire and its task rows were not.
RPC_CALLS = [
    ("run_command", {"command": "true"}, "terminal_exec", {"command": "true"}),
    ("read_command", {"id": "run-1"}, "terminal_read", {"run_id": "run-1"}),
    (
        "read_command",
        {"id": "run-1", "cursor": "12"},
        "terminal_read",
        {"run_id": "run-1", "cursor": 12},
    ),
    (
        "control_command",
        {"id": "run-1", "action": "interrupt"},
        "terminal_control",
        {"run_id": "run-1", "action": "interrupt"},
    ),
    ("get_environment", {}, "environment_status", {}),
    # 2026-11-09: screen/browser actions add the owner and the deployment's image limits.
    (
        "computer",
        {"action": "screenshot"},
        "computer_action",
        {"action": "screenshot", "owner": None, "max_dimension": 4096, "max_pixels": 16777216},
    ),
    (
        "browser",
        {"action": "snapshot"},
        "browser_action",
        {"action": "snapshot", "owner": None, "max_dimension": 4096, "max_pixels": 16777216},
    ),
]


def revision() -> dict:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip())
    return {"commit": head, "worktree_dirty": dirty}


def write(name: str, value: dict) -> None:
    ARTIFACT.mkdir(parents=True, exist_ok=True)
    (ARTIFACT / name).write_text(
        json.dumps({**revision(), **value}, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


class RecordingTasks:
    def __init__(self) -> None:
        self.prepared: list[dict] = []

    async def prepare(self, request_id, arguments, source, **_):
        self.prepared.append(arguments)

    async def bind_run(self, request_id, run_id):
        return None

    async def reject(self, request_id, result):
        return None


async def recording_manager(received: list[dict], *, answer: bool = True):
    async def accept(reader, writer):
        request = json.loads(await reader.readline())
        received.append(request)
        if answer:
            reply = {"run_id": "run-1", "status": "running", "pending": True}
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
        writer.close()

    directory = Path(tempfile.mkdtemp(prefix="yk-"))
    server = await asyncio.start_unix_server(accept, str(directory / "m.sock"))
    return server, directory


async def test_model_environment_tools_keep_manager_rpc_wire_names(database):
    received: list[dict] = []
    server, directory = await recording_manager(received)
    settings = make_settings(database.url)
    tools = AgentToolService(
        settings=settings,
        ledger=EventLedgerRepository(database),
        memories=MemoryFactService(MemoryFactRepository(database)),
        **tool_dependencies(database, settings),
    )
    recorded = RecordingTasks()
    tools.sandbox_client = SandboxClient(directory / "m.sock", tasks=recorded)
    runtime = ToolRuntime(
        inbound=InboundMessage(
            "rpc-trigger",
            "rpc-trigger",
            "message:private:normal",
            ScopeType.PRIVATE,
            SenderIdentity("10001"),
            "run it",
            bot_user_id="80001",
        ),
        execution_id="rpc-execution",
        conversation_id="rpc-conversation",
        target_presence_id="presence-test",
    )
    declared = {tool.name for tool in sandbox_tools()}
    try:
        for index, (name, arguments, _, _) in enumerate(RPC_CALLS):
            token = current_invocation.set(ToolInvocationContext(runtime, f"call-{index}"))
            try:
                result = await tools.execute(name, json.dumps(arguments), runtime)
            finally:
                current_invocation.reset(token)
            assert result.error_code is None, (name, result)
    finally:
        server.close()
        await server.wait_closed()
        shutil.rmtree(directory, ignore_errors=True)

    wire = [(item["method"], item["args"]) for item in received]
    write(
        "rpc-wire.json",
        {
            "declared_environment_tools": sorted(declared),
            "wire": [
                {"tool": name, "method": method, "args": args}
                for (name, *_), (method, args) in zip(RPC_CALLS, wire, strict=True)
            ],
            "task_rows": recorded.prepared,
        },
    )
    # F1: every declared environment tool reaches the Manager under its original method.
    assert {name for name, *_ in RPC_CALLS} == declared
    assert [method for method, _ in wire] == [method for _, _, method, _ in RPC_CALLS]
    assert [args for _, args in wire] == [arguments for *_, arguments in RPC_CALLS]
    # F2: the task row payload keeps the original method name.
    assert recorded.prepared == [{"tool": "terminal_exec", "arguments": {"command": "true"}}]


@pytest.mark.parametrize(
    ("method", "uncertain"),
    [
        ("terminal_read", False),
        ("terminal_control", True),
        ("environment_status", False),
        ("environment_service", True),
        ("workspace_copy", True),
        ("workspace_publish", True),
        ("workspace_media_read", False),
        ("sticker_index", False),
        ("acp_prompt_cancel", True),
    ],
)
async def test_submitted_rpc_keeps_uncertainty_by_method(method, uncertain):
    # F3: the Manager accepts the line and closes before answering.
    received: list[dict] = []
    server, directory = await recording_manager(received, answer=False)
    try:
        result = await SandboxClient(directory / "m.sock").execute(
            method, {}, request_id=f"submitted-{method}"
        )
    finally:
        server.close()
        await server.wait_closed()
        shutil.rmtree(directory, ignore_errors=True)
    assert [item["method"] for item in received] == [method]
    assert result == {"error": "sandbox_unavailable", "retryable": False, "uncertain": uncertain}


async def test_original_receipts_keep_projection_and_recovery_reads(app_env, database):
    env = app_env
    device, _ = await owner(env, database)
    original = await seeded(env, database)
    identity = original.row["id"]
    now = time.time()
    async with database.immediate_session() as writer:
        for index, (name, state, receipt, _) in enumerate(HISTORY):
            await writer.execute(
                insert(effects).values(
                    effect_key=f"history-{index:02d}-{name}",
                    work_id=identity,
                    kind="tool",
                    state=state,
                    receipt_json=json.dumps(receipt, ensure_ascii=False),
                    created=float(index + 1),
                    updated=float(index + 1),
                )
            )
        await writer.execute(
            insert(waits).values(
                id=str(uuid4()),
                work_id=identity,
                conversation_id=original.row["conversation_id"],
                generation=1,
                principal_kind="self",
                principal_id="self",
                call_key=f"history-wait-{uuid4()}",
                request_json="{}",
                mode="any",
                conditions_json=json.dumps(
                    [
                        {"kind": "owned_run", "run_id": "run-terminal"},
                        {"kind": "owned_run", "run_id": "run-agent"},
                    ]
                ),
                status="active",
                created=now,
                updated=now,
            )
        )
    response = await env.client.get(
        f"/client/v1/works/{identity}/steps",
        params={"limit": 100},
        headers=bearer(device["token"]),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    steps = data["steps"]
    projected = [
        {
            "sample": name,
            "category": step["category"],
            "label": step["label"],
            "status": step["status"],
            "record": step["record"],
            "artifacts": step["artifacts"],
        }
        for (name, *_), step in zip(HISTORY, steps, strict=True)
    ]

    # F7: the original recovery reader, with its typed capture, over old-name rows.
    journal = WorkJournal(WorkRepository(database))
    recovered = {}
    for index, (name, _, receipt, _) in enumerate(HISTORY):
        tool = receipt.get("invocation", {}).get("tool_id", "legacy_tool")
        if name not in {"send_message_legacy_result", "send_message_outcome", "web_read"}:
            continue
        capture = ResultCapture(identity, f"history-{index:02d}-{name}")
        token = current_result_capture.set(capture)
        try:
            await journal.effect_result(f"history-{index:02d}-{name}", original_tool=tool)
        finally:
            current_result_capture.reset(token)
        recovered[name] = capture.evidence

    write(
        "history-projection.json",
        {
            "samples": [
                {"sample": name, "state": state, "receipt": receipt}
                for name, state, receipt, _ in HISTORY
            ],
            "steps": projected,
            "wait": data["wait"],
            "recovered_evidence": recovered,
        },
    )
    # F4, F5: categories, labels and statuses of original receipts are unchanged.
    assert [(item["category"], item["label"], item["status"]) for item in projected] == [
        expected for *_, expected in HISTORY
    ]
    assert projected[[n for n, *_ in HISTORY].index("workspace_publish")]["artifacts"] == [
        {"artifact_id": "art-published", "version": "v1"}
    ]
    # F6: owned-run waits keep their run kind.
    assert [item["run_kind"] for item in data["wait"]["conditions"]] == ["terminal", "acp"]
    # F7: recovery keeps the original name and the original delivery facts.
    assert recovered["send_message_legacy_result"] == {
        "tool": "send_message",
        "artifacts": ["art-original"],
        "delivered_artifacts": ["art-original"],
        "caption_delivered": True,
        "delivered_message": False,
        "delivery_target": None,
        "run_id": None,
        "side_effecting": True,
        "ok": True,
        "pending": False,
        "uncertain": False,
        "status": None,
        "error_code": None,
        "retryable": False,
        "mutation_committed": None,
        "executed": True,
    }
    assert recovered["send_message_outcome"]["tool"] == "send_message"
    assert recovered["web_read"] == {"ok": True, "side_effecting": False}


def test_code_mode_wrappers_and_declarations_keep_current_names():
    from yuki.runtime.effect_outcomes import RECORDED_TOOL_NAMES

    declared = (*sandbox_tools(), *workspace_tools())
    api = project(declared, "history-prep")
    wrappers = sorted(api.wrappers.items())
    write(
        "code-mode-wrappers.json",
        {"wrappers": wrappers, "recorded_tool_names": dict(RECORDED_TOOL_NAMES)},
    )
    # F8: wrapper names stay the deployed tool names.
    assert wrappers == sorted((f"yuki_{tool.name}", tool.name) for tool in declared)
    # F9: the history map only reads originals; none of its names is declared.
    from yuki.external_agents.tools import external_agent_tools

    every = {tool.name for tool in (*declared, *external_agent_tools())}
    assert not set(RECORDED_TOOL_NAMES) & every
    # Each original name of these tools reads as a declared successor.
    for original in (
        "terminal_exec",
        "terminal_read",
        "terminal_control",
        "environment_status",
        "workspace_inspect",
        "inspect_conversation_attachment",
        "save_conversation_attachment_to_workspace",
        "external_agent",
    ):
        assert RECORDED_TOOL_NAMES[original] in every
    assert "environment_service" not in {*RECORDED_TOOL_NAMES, *every}
