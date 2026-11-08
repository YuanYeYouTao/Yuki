"""Work and schedule tools after the tool-surface cutover, end to end on real SQLite.

Failure cases listed before the implementation (docs/operations/
tool-surface-cutover-20261107.md, section F): the five Work tools are the only
lifecycle controls; ``wait`` covers subtask/command runs, the next message,
plugin events, timers, any/all and a deadline (strict single-key items are
enforced by the frozen schema, docs/operations/control-call-schema-20261108.md);
a recorded ``task_control`` name is history only and is never dispatched again;
schedules are created and paused through ``manage_schedule`` with an explicit
persisted confirmation and the real ``id``.

Every scenario writes normalized evidence under
``test-results/tool-surface-cutover/work-tools/`` for byte-for-byte reruns.
"""

import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.support.automation_unified_delivery_helpers import setup_run
from tests.support.social_identity_cases import social_env

from yuki.automation.models import AutomationStatus
from yuki.capabilities.validation import JsonSchemaCapabilityValidator
from yuki.runtime.subagent_repository import SubagentRepository
from yuki.runtime.subagent_tools import subagent_tools
from yuki.runtime.work_control import WORK_CONTROL_NAMES, WorkControl, work_control_tools
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_wait import WorkWaitRepository
from yuki.services.agent_tools import ToolRuntime

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)?")
_EPOCH = re.compile(r"\b1[0-9]{9}(\.[0-9]+)?\b")


def _stable(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    seen: dict[str, str] = {}
    text = _UUID.sub(lambda m: seen.setdefault(m.group(0), f"<uuid:{len(seen) + 1}>"), text)
    return _EPOCH.sub("<epoch>", _TIME.sub("<time>", text)) + "\n"


def _write(name, value):
    directory = (
        Path(__file__).resolve().parents[2] / "test-results" / "tool-surface-cutover" / "work-tools"
    )
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(_stable(value), encoding="utf-8")


async def _running_work(database, tmp_path):
    database.subagents_enabled = True
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(
        repository,
        lease,
        "work-tool-surface",
        {"actor_person_id": env.person, "principal_kind": "person", "origin": "user_message"},
        validate,
    )
    started = json.loads(
        await control.execute("manage_task", {"action": "start", "goal": "整理资料"}, "start")
    )
    assert started["status"] == "running" and "ok" not in started
    return env, repository, lease, control


async def _call(control, name, args, key):
    return json.loads(await control.execute(name, args, key))


async def _plugin_grant(database, lease, plugin_id):
    from yuki.conversation.canonical_db_models import CanonicalConversationModel
    from yuki.persistence.models import ChatEventModel
    from yuki.plugin_host.db_models import PluginBackgroundTargetGrantModel, PluginInstallationModel

    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        original = await writer.scalar(select(ChatEventModel))
        conversation = await writer.get(CanonicalConversationModel, lease.conversation_id)
        writer.add(
            PluginInstallationModel(
                plugin_id=plugin_id,
                name="synthetic",
                version="1",
                plugin_api="2.0",
                yuki_requires="*",
                manifest_hash="0" * 64,
                entrypoint="unused",
                status="approved",
                enabled=True,
                discovered_at=now,
                updated_at=now,
            )
        )
        await writer.flush()
        writer.add(
            PluginBackgroundTargetGrantModel(
                plugin_id=plugin_id,
                target_type="group",
                target_id="20001",
                bot_user_id="80001",
                enabled=True,
                created_by_user_id="10001",
                canonical_target_space_id=conversation.space_id,
                canonical_created_by_person_id=original.author_person_id,
                canonical_presence_id=original.ingress_presence_id,
                created_at=now,
                updated_at=now,
            )
        )
        values = {
            column.name: getattr(original, column.name)
            for column in ChatEventModel.__table__.columns
            if column.name != "id"
        }
    return values


async def _event(database, values, **overrides):
    from yuki.persistence.models import ChatEventModel

    async with database.immediate_session() as writer:
        event = ChatEventModel(
            **{
                **values,
                "canonical_event_id": str(uuid4()),
                "origin_key": None,
                "native_message_id": None,
                **overrides,
            }
        )
        writer.add(event)
        await writer.flush()
        return event.id


def test_declarations_are_the_new_work_and_schedule_tools():
    tools = {tool.name: tool for tool in (*work_control_tools(), *subagent_tools())}
    assert set(tools) == WORK_CONTROL_NAMES
    validator = JsonSchemaCapabilityValidator()
    validator.admit_tools(tuple(tools.values()))
    manifest = {
        name: {
            "parameters": sorted(tool.parameters["properties"]),
            "required": tool.parameters.get("required", []),
            "description_characters": len(tool.description),
        }
        for name, tool in sorted(tools.items())
    }
    assert manifest["wait"]["parameters"] == ["all", "deadline", "for"]
    assert manifest["manage_task"]["parameters"] == [
        "action",
        "goal",
        "id",
        "note",
        "result",
        "text",
    ]
    assert "version" not in tools["manage_task"].parameters["properties"]["note"]["properties"]
    assert manifest["get_tasks"]["parameters"] == ["cursor", "id", "limit", "parent", "status"]
    assert manifest["message_task"]["parameters"] == ["ask", "id", "reply_to", "text"]
    assert manifest["start_subtask"]["parameters"] == ["acceptance", "context", "files", "goal"]
    _write("declarations", manifest)


@pytest.mark.parametrize("signal", ["subtask", "message", "plugin_event"])
async def test_wait_for_subtask_or_message_or_plugin_event(database, tmp_path, signal):
    _env, repository, lease, control = await _running_work(database, tmp_path)
    plugin_values = await _plugin_grant(database, lease, "synthetic-wait")
    child = await _call(
        control, "start_subtask", {"goal": "查证一个来源", "acceptance": "给出链接"}, "spawn"
    )
    assert child["status"] == "running"
    # Fact refusals happen before anything is registered. Malformed items are
    # refused earlier by the frozen schema (test_control_call_schema_e2e).
    refusals = {
        "naive_at": await _call(control, "wait", {"for": [{"at": "2026-11-08T09:00"}]}, "bad-3"),
        "foreign_run": await _call(control, "wait", {"for": [{"run": str(uuid4())}]}, "bad-4"),
    }
    for refusal in refusals.values():
        assert refusal["ok"] is False and refusal["message"]
    assert await WorkWaitRepository(repository).describe(control.current["id"]) is None
    waited = await _call(
        control,
        "wait",
        {
            "for": [
                {"run": child["id"]},
                {"message": True},
                {"plugin_event": {"plugin_id": "synthetic-wait", "event_type": "done"}},
            ],
            "deadline": "2099-01-01T00:00:00+08:00",
        },
        "wait",
    )
    assert waited == {"id": control.current["id"], "status": "waiting"}
    await control.settle(pending_inputs=False)
    assert control.current["state"] == "waiting_external"
    registered = await WorkWaitRepository(repository).describe(control.current["id"])
    assert registered["mode"] == "any"
    assert [condition["kind"] for condition in registered["conditions"]] == [
        "owned_run",
        "conversation",
        "plugin_event",
    ]
    waits = WorkWaitRepository(repository)
    if signal == "subtask":
        workers = SubagentRepository(repository)
        child_lease = await workers.acquire(child["id"])
        row = await repository.get(child["id"])
        await repository.transition(child_lease, child["id"], row["revision"], "failed")
        await repository.release(child_lease)
        delivered = await waits.deliver_due(now=registered["registered_at"] + 1)
    elif signal == "message":
        event_id = await _event(
            database,
            plugin_values,
            origin_key="next-human",
            native_message_id="next-human",
            content="新的消息",
        )
        delivered = await waits.match_event(event_id=event_id, kind="conversation")
    else:
        event_id = await _event(
            database,
            plugin_values,
            content="插件事件",
            event_kind="external_event",
            direction="external",
            author_kind="system",
            author_person_id=None,
            origin="plugin_background",
            source_plugin_id="synthetic-wait",
            external_source="synthetic",
            external_event_key="done-1",
            external_event_type="done",
            external_payload_json="{}",
            external_target_id="20001",
            external_resume_wait=True,
        )
        delivered = await waits.match_event(event_id=event_id, kind="plugin_event")
    assert delivered
    observed = await waits.describe(control.current["id"])
    matched = [c["kind"] for c in observed["conditions"] if c.get("matched")]
    assert matched == [{"subtask": "owned_run", "message": "conversation"}.get(signal, signal)]
    assert (await repository.get(control.current["id"]))["state"] == "queued"
    task = await _call(control, "get_tasks", {"id": control.current["id"]}, "read")
    assert task["id"] == control.current["id"] and "creator_user_id" not in task
    assert "generation" not in task and "work_id" not in task
    _write(
        f"wait-{signal}",
        {
            "refusals": {name: value["error"] for name, value in refusals.items()},
            "registered": [condition["kind"] for condition in registered["conditions"]],
            "matched": matched,
            "task_view_keys": sorted(task),
            "wait_view": task.get("wait", {}).get("status"),
        },
    )
    await repository.release(lease)


async def test_recorded_old_control_name_is_history_only(database, tmp_path):
    _env, repository, lease, control = await _running_work(database, tmp_path)
    before = await repository.get(control.current["id"])
    refused = await _call(
        control, "task_control", {"action": "complete", "result": "旧调用"}, "old-name"
    )
    assert refused["ok"] is False and refused["error"] == "unknown_work_control"
    assert await repository.get(control.current["id"]) == before
    # A recorded lifecycle control is not a business effect, under either name.
    for name in ("task_control", "manage_task", "subagent_control"):
        control.observe_evidence({"tool": name, "executed": True, "run_id": None})
    assert control.known_effects == []
    _write("old-name", {"refused": refused, "known_effects": control.known_effects})
    await repository.release(lease)


async def test_waiting_only_for_the_next_message_is_waiting_for_the_user(database, tmp_path):
    _env, repository, lease, control = await _running_work(database, tmp_path)
    waited = await _call(control, "wait", {"for": [{"message": True}]}, "ask-user")
    assert waited == {"id": control.current["id"], "status": "waiting"}
    await control.settle(pending_inputs=False)
    # ACP and Client end a turn on a question; the conversation wait still wakes it.
    assert control.current["state"] == "waiting_user"
    registered = await WorkWaitRepository(repository).describe(control.current["id"])
    assert [condition["kind"] for condition in registered["conditions"]] == ["conversation"]
    _write("wait-message-only", {"state": control.current["state"], "waited": waited})
    await repository.release(lease)


async def test_schedule_is_created_and_paused_with_persisted_confirmation(database, tmp_path):
    case = await setup_run(database, tmp_path, mode="silent")
    tools = case.chat._automation_tools
    names = {tool.name: tool for tool in tools.definitions()}
    assert set(names) == {"get_schedules", "manage_schedule"}
    assert {(tool.namespace, tool.effect) for tool in names.values()} == {
        ("automation.read", "read_state"),
        ("automation.write", "write_state"),
    }
    runtime = ToolRuntime(
        inbound=None,
        actor_context=replace(case.actor, execution_id="schedule-e2e", event_id=None),
        execution_id="schedule-e2e",
        conversation_id=case.actor.conversation_id,
        conversation_key=case.actor.conversation_id,
    )

    async def call(name, arguments):
        return await tools.execute(name, json.dumps(arguments), runtime)

    created = await call(
        "manage_schedule",
        {
            "action": "create",
            "goal": "每天早上提醒我喝水",
            "trigger": {"type": "daily", "hour": 8, "minute": 30},
            "name": "喝水提醒",
        },
    )
    assert created.ok and created.data["confirmation"] == "persisted"
    identifier = created.data["id"]
    paused = await call("manage_schedule", {"action": "pause", "id": identifier})
    assert paused.ok and paused.data == {"id": identifier, "changed": True}
    assert (await case.repository.get(identifier)).status is AutomationStatus.PAUSED
    listed = await call("get_schedules", {})
    row = next(item for item in listed.data["schedules"] if item["id"] == identifier)
    assert row["status"] == "paused" and row["name"] == "喝水提醒"
    detail = await call("get_schedules", {"id": identifier})
    assert detail.data["goal"] == "每天早上提醒我喝水"
    unknown = await call("manage_schedule", {"action": "pause", "automation_id": identifier})
    assert unknown.error_code == "invalid_arguments"
    payload = unknown.model_payload()
    assert payload["ok"] is False and "automation_id" in payload["message"]
    _write(
        "schedule",
        {
            "created": created.model_payload(),
            "paused": paused.model_payload(),
            "listed": row,
            "refused": payload,
        },
    )
