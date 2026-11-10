"""Tool-surface one-shot cutover, end to end on the real application and SQLite.

Failure cases were listed before the implementation in
docs/operations/tool-surface-cutover-20261107.md. This file covers the
cross-cutting ones; each domain has its own E2E test.

* A1/A2/A6: the frozen manifest of the offline application is exactly the 29
  tools (direct mode; the 27 of the cutover plus computer/browser, contract 36),
  no old or deleted name is declared, every history-map successor is declared,
  and Code Mode keeps its frozen direct view.
* B7: every Host tool carries its capability namespace and effect on the
  declaration itself (no separate name table).
* B12/D25: a model calling an old name is refused as an unknown tool and
  nothing is dispatched under any successor name.
* C15/C16/C17: success results are the business data without an envelope;
  failures are ``{"ok": false, "error", "message"}``; the Provider failure
  marker reads exactly those, and ``process.succeeded``.
* D24: Client API steps classify current-name receipts in the same Desktop
  categories as their recorded predecessors.

Artifacts: ``test-results/tool-surface-cutover/{manifest,old-name-calls,
history-steps}.json`` (commit and worktree state recorded; UUIDs normalized).
"""

import asyncio
import json
import re
import subprocess
import time
from pathlib import Path

import pytest
from sqlalchemy import insert
from tests.conftest import make_settings
from tests.support.automation_unified_delivery_helpers import setup_run
from tests.support.model_profiles import write_fake_profiles
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_client_api import bearer
from tests.unit.test_client_works import owner, seeded
from tests.unit.test_sticker_library import LocalDirectoryBridge

from yuki.capabilities.results import ToolExecutionResult
from yuki.codemode.tool_visibility import DIRECT_TOOL_NAMES
from yuki.container import ApplicationContainer
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.llm.protocol_state import tool_result_failed
from yuki.runtime.effect_outcomes import RECORDED_TOOL_NAMES
from yuki.runtime.work_control import WORK_CONTROL_NAMES
from yuki.runtime.work_schema_v1 import effects

admin = admin_fixture
app_env = app_fixture

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "test-results" / "tool-surface-cutover"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

TOOLS = {
    "send",
    "recall_message",
    "read_chat",
    "find_contacts",
    "get_members",
    "search_memory",
    "save_memory",
    "edit_memory",
    "set_short_note",
    "web_search",
    "read_webpage",
    "view_file",
    "copy_file",
    "read_result",
    "run_command",
    "read_command",
    "control_command",
    "get_environment",
    "computer",
    "browser",
    "get_tasks",
    "manage_task",
    "start_subtask",
    "message_task",
    "wait",
    "get_schedules",
    "manage_schedule",
    "get_coding_agents",
    "use_coding_agent",
}
DELETED = {"environment_service", "get_my_capabilities", "time_get_current"}
# Task book §6.1: production direct declaration before the cutover (trace 411675).
BASELINE = {"tools": 29, "parameters": 176, "json_bytes_utf8": 45800}


def _revision() -> dict:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, cwd=ROOT).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], text=True, cwd=ROOT)
    return {"commit": head, "worktree_dirty": bool(dirty.strip())}


def _write(name: str, value: dict) -> None:
    ARTIFACT.mkdir(parents=True, exist_ok=True)
    seen: dict[str, str] = {}
    text = json.dumps({**_revision(), **value}, ensure_ascii=False, indent=2, sort_keys=True)
    text = _UUID.sub(lambda m: seen.setdefault(m.group(0), f"<uuid:{len(seen) + 1}>"), text)
    (ARTIFACT / name).write_text(text + "\n", encoding="utf-8")


def _parameter_count(schema: dict) -> int:
    return len(schema.get("properties", {}))


@pytest.mark.parametrize("code_enabled", [False, True])
async def test_offline_app_freezes_exactly_the_new_tool_surface(
    database, tmp_path, monkeypatch, code_enabled
):
    app = ApplicationContainer(
        make_settings(
            database.url,
            model_profiles_file=write_fake_profiles(tmp_path / "models.toml"),
            plugin_system_enabled=False,
            plugin_directory=tmp_path / "plugins",
            workspace_directory=tmp_path / "artifacts",
            conversation_media_cache_directory=tmp_path / "conversation-media",
            social_transfer_directory=tmp_path / "transfer",
            semantic_participation_state_path=tmp_path / "participation.sqlite3",
            code_mode_enabled=code_enabled,
            web_mode="tavily",
            tavily_api_key="offline-fixture",
            automation_enabled=True,
            memory_embedding_enabled=False,
            memory_embedding_worker_enabled=False,
            memory_self_reflection_enabled=False,
            memory_dream_enabled=False,
            memory_maintenance_enabled=False,
            conversation_proactive_wakeup_enabled=False,
            conversation_autonomous_enabled=False,
        ),
        database=database,
    )
    await app.character.initialize("Offline fixture")
    bridge = LocalDirectoryBridge(
        tmp_path / "manager-workspace",
        app.workspace_service.store,
        cache_root=app.conversation_media.root.parent,
    )

    async def manager_read_or_file(method, arguments, *, request_id, **_kwargs):
        if method == "list_code_completions":
            return {"events": [], "next_cursor": 0, "has_more": False}
        return await bridge.execute(method, arguments, request_id=request_id)

    monkeypatch.setattr(app.sandbox_client, "execute", manager_read_or_file)
    try:
        await app.start()
        full = {tool.name: tool for tool in await app.main_agent_contract.definitions()}
        model = {tool.name: tool for tool in await app.main_agent_contract.model_definitions()}
    finally:
        await asyncio.wait_for(app.close(), 5)

    business = set(full) - {"execute_code", "lookup_tools"}
    # A1/A2: exactly the new surface; nothing old or deleted is declared.
    assert business == TOOLS
    assert not (set(full) & (set(RECORDED_TOOL_NAMES) | DELETED))
    assert set(RECORDED_TOOL_NAMES.values()) <= TOOLS
    if code_enabled:
        # A6: Code Mode keeps its own frozen direct view over the same manifest.
        assert set(model) == (DIRECT_TOOL_NAMES & set(full))
        assert {"execute_code", "lookup_tools"} <= set(model)
    else:
        assert set(model) == TOOLS
    # B7: registry-bound Host tools carry namespace and effect on the declaration;
    # Work controls and the short note are appended to the manifest directly.
    for name in TOOLS - WORK_CONTROL_NAMES - {"set_short_note"}:
        assert full[name].namespace and full[name].effect, name

    if code_enabled:
        return
    manifest = {
        name: {
            "parameters": sorted(tool.parameters.get("properties", {})),
            "required": sorted(tool.parameters.get("required", [])),
            "description_characters": len(tool.description),
            "json_bytes_utf8": len(
                json.dumps(
                    {
                        "name": name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                    ensure_ascii=False,
                ).encode()
            ),
        }
        for name, tool in sorted(model.items())
    }
    totals = {
        "tools": len(manifest),
        "parameters": sum(_parameter_count(tool.parameters) for tool in model.values()),
        "description_characters": sum(item["description_characters"] for item in manifest.values()),
        "json_bytes_utf8": sum(item["json_bytes_utf8"] for item in manifest.values()),
    }
    _write("manifest.json", {"baseline": BASELINE, "totals": totals, "tools": manifest})
    assert totals["tools"] == 29
    # Except read_chat (its filters) and computer (its coordinates, frozen once by the
    # computer/browser task book §5.1), no tool exceeds 8 parameters (§6.8).
    assert all(
        len(item["parameters"]) <= 8
        for name, item in manifest.items()
        if name not in {"read_chat", "computer"}
    ), {name: item["parameters"] for name, item in manifest.items()}


async def test_old_name_calls_are_refused_and_never_dispatched(database, tmp_path):
    case = await setup_run(database, tmp_path, mode="silent")
    old_calls = (
        ("old-task", "task_control", {"action": "accept", "goal": "旧名接纳"}),
        ("old-terminal", "terminal_exec", {"command": "true"}),
        ("old-history", "query_chat_history", {"view": "recent"}),
        ("old-memory", "memory_change", {"operation": "create", "new_content": "x"}),
        ("old-clock", "time_get_current", {}),
    )
    seen: dict[str, str] = {}

    def respond(request):
        count = len(case.provider.requests)
        for message in request.messages:
            if message.role == "tool" and message.tool_call_id:
                seen.setdefault(message.tool_call_id, message.content or "")
        if count == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=tuple(
                    ToolCall(identity, ToolFunction(name, json.dumps(arguments)))
                    for identity, name, arguments in old_calls
                ),
            )
        if count == 2:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "new-note",
                        ToolFunction("set_short_note", json.dumps({"slot": 1, "text": "新名"})),
                    ),
                ),
            )
        return "done"

    case.provider._responder = respond
    case.clock.advance(2)
    await case.dispatch()
    results = {identity: json.loads(seen[identity]) for identity, *_ in old_calls}
    note = json.loads(seen["new-note"])
    async with database.read_session() as reader:
        tools = [
            json.loads(row).get("invocation", {}).get("tool_id")
            for row in await reader.scalars(
                effects.select().with_only_columns(effects.c.receipt_json)
            )
        ]
    _write(
        "old-name-calls.json",
        {"old_calls": results, "new_call": note, "receipt_tools": sorted(filter(None, tools))},
    )
    # B12/D25: every old name is an unknown tool; nothing ran under a successor.
    for identity, name, _ in old_calls:
        assert results[identity]["ok"] is False, (name, results[identity])
        assert results[identity]["error"] == "tool_not_declared", (name, results[identity])
        assert name in results[identity]["message"]
        assert tool_result_failed(seen[identity])
    assert not [action for action, _ in case.env.bot.calls if action.startswith("send_")]
    assert not {"manage_task", "run_command", "read_chat", "save_memory"} & set(tools)
    # C15: success is the business data itself.
    assert note == {"slot": 1, "text": "新名"}
    assert not tool_result_failed(seen["new-note"])


def test_projection_and_provider_failure_marker():
    success = ToolExecutionResult(
        ok=True,
        data={"status": "succeeded", "ok_count": 2, "absent": None},
        mutation_committed=True,
        provider_id="core",
        tool_name="get_environment",
    )
    failure = ToolExecutionResult(
        ok=False,
        error_code="choose_one_source",
        public_message="path、artifact_id、event_id+attachment 只能给一个。",
        provider_id="core",
        tool_name="view_file",
    )
    unknown = ToolExecutionResult(
        ok=False,
        error_code="copy_unconfirmed",
        public_message="写入未确认。",
        uncertain=True,
        data={"sha256": "abc"},
        provider_id="core",
        tool_name="copy_file",
    )
    failed_process = ToolExecutionResult(
        ok=True,
        data={"run_id": "run-1", "status": "failed", "exit_code": 7, "pending": False},
        provider_id="core",
        tool_name="run_command",
    )
    projected = {
        "success": success.model_payload(),
        "failure": failure.model_payload(),
        "unknown": unknown.model_payload(),
        "failed_process": failed_process.model_payload(),
    }
    _write("result-shapes.json", projected)
    # C15: no envelope, no null, no provider or tool name.
    assert projected["success"] == {"status": "succeeded", "ok_count": 2}
    # C16: one failure shape; an effect that may have happened is marked unknown.
    assert projected["failure"] == {
        "ok": False,
        "error": "choose_one_source",
        "message": "path、artifact_id、event_id+attachment 只能给一个。",
    }
    assert projected["unknown"] == {
        "ok": False,
        "error": "copy_unconfirmed",
        "message": "写入未确认。",
        "status": "unknown",
        "sha256": "abc",
    }
    assert projected["failed_process"]["process"] == {"succeeded": False}
    # C17: Provider markers read exactly ok:false and process.succeeded:false.
    marks = {name: tool_result_failed(json.dumps(value)) for name, value in projected.items()}
    assert marks == {
        "success": False,
        "failure": True,
        "unknown": True,
        "failed_process": True,
    }
    assert not tool_result_failed(json.dumps({"error": "a field of business data"}))


# Current-name receipts and the category their recorded predecessor projects to.
CURRENT_STEPS = [
    (
        "run_command",
        {"action": None},
        {"ok": True, "status": "succeeded", "run_id": "r1"},
        "terminal",
    ),
    ("read_command", {}, {"ok": True, "side_effecting": False}, "terminal"),
    ("view_file", {}, {"ok": True}, "file"),
    ("copy_file", {}, {"ok": True}, "file"),
    ("save_memory", {}, {"ok": True}, "memory"),
    ("edit_memory", {}, {"ok": True}, "memory"),
    ("search_memory", {}, {"ok": True, "side_effecting": False}, "memory"),
    (
        "use_coding_agent",
        {"action": "prompt"},
        {"ok": True, "status": "running", "run_id": "r2"},
        "agent",
    ),
    ("get_coding_agents", {}, {"ok": True, "side_effecting": False}, "agent"),
    ("web_search", {}, {"ok": True}, "search"),
    ("read_webpage", {}, {"ok": True}, "web"),
    ("send", {}, {"ok": True, "status": "succeeded"}, "message"),
    ("manage_task", {}, {"ok": True}, "other"),
    ("computer", {"action": "click"}, {"ok": True}, "computer"),
    ("browser", {"action": "open"}, {"ok": True}, "browser"),
]


async def test_current_names_project_like_their_recorded_predecessors(app_env, database):
    env = app_env
    device, _ = await owner(env, database)
    original = await seeded(env, database)
    identity = original.row["id"]
    async with database.immediate_session() as writer:
        for index, (name, invocation, outcome, _) in enumerate(CURRENT_STEPS):
            await writer.execute(
                insert(effects).values(
                    effect_key=f"current-{index:02d}-{name}",
                    work_id=identity,
                    kind="tool",
                    state="accepted",
                    receipt_json=json.dumps(
                        {
                            "invocation": {
                                "tool_id": name,
                                **{k: v for k, v in invocation.items() if v is not None},
                            },
                            "outcome": {"tool": name, **outcome},
                        }
                    ),
                    created=time.time() + index,
                    updated=time.time() + index,
                )
            )
    response = await env.client.get(
        f"/client/v1/works/{identity}/steps",
        params={"limit": 100},
        headers=bearer(device["token"]),
    )
    assert response.status_code == 200, response.text
    steps = response.json()["data"]["steps"][-len(CURRENT_STEPS) :]
    projected = [
        {
            "tool": name,
            "category": step["category"],
            "label": step["label"],
            "status": step["status"],
        }
        for (name, *_), step in zip(CURRENT_STEPS, steps, strict=True)
    ]
    _write("history-steps.json", {"current_name_steps": projected})
    # D24: same Desktop categories as the recorded predecessors.
    assert [item["category"] for item in projected] == [expected for *_, expected in CURRENT_STEPS]
    assert projected[0]["status"] == "succeeded"
    assert projected[7]["status"] == "running"
