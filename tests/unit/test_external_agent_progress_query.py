"""Explicit progress identity and native facts survive the cloud projection."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from tests.unit.test_external_agent_repository import event, prepare
from tests.unit.test_external_agent_repository import native as native
from tests.unit.test_external_agent_work import case as case
from tests.unit.test_external_agent_writer_contract import owned
from yuki_agent_connector.config import Instance
from yuki_agent_connector.drivers.codex import CodexDriver

from yuki.external_agents.repository import execution_key
from yuki.external_agents.service import ExternalAgentService


async def test_session_watch_with_old_execution_reads_that_turn_and_keeps_page_ids(
    case, monkeypatch
):
    native, _ = await owned(case)
    native.service = ExternalAgentService(case.db, case.tasks)
    operation, _ = await prepare(
        native,
        identity="session-watch",
        action="watch",
        native_execution_id=None,
        command={"id": "session-watch", "action": "watch", "observation_scope": "session"},
        lease=case.control.lease,
    )
    await native.repository.response(
        operation.id,
        {"status": "running", "pending": True, "session_id": "native-thread", "execution_id": "B"},
    )
    old = await native.repository.remember_execution(native.session, "A")
    calls = []

    async def request(command):
        calls.append(command)
        return {
            "session_id": "native-thread",
            "execution_id": "A",
            "status": "completed",
            "selected_turn": {
                "id": "A",
                "items": [{"type": "agentMessage", "text": "original A result"}],
            },
            "native": {"turns": [{"id": "earlier"}]},
        }

    async def connection(*_, **__):
        return SimpleNamespace(request=request)

    monkeypatch.setattr(native.service, "_connection", connection)
    result = await native.service._get(
        {"operation_id": operation.id, "execution_id": old, "native_cursor": "old-items"},
        "query",
        native.actor,
    )
    assert calls[0]["execution_id"] == "A"
    assert calls[0]["arguments"]["cursor"] == "old-items"
    assert "operation_id" not in calls[0]["arguments"]
    assert result["execution_id"] == old and result["related_operation_id"] == operation.id
    assert result["selected_turn"]["execution_id"] == old
    earlier = result["native"]["turns"][0]["execution_id"]
    assert earlier == execution_key(native.session, "earlier")
    assert (await native.repository.native_execution(earlier, native.actor)).native_id == "earlier"


async def test_explicit_execution_missing_in_native_read_stays_unconfirmed(native, monkeypatch):
    old = await native.repository.remember_execution(native.session, "A")

    async def request(_):
        return {"session_id": "native-thread", "execution_id": None, "status": "completed"}

    async def connection(*_, **__):
        return SimpleNamespace(request=request)

    monkeypatch.setattr(native.service, "_connection", connection)
    result = await native.service._get({"execution_id": old}, "query", native.actor)
    assert result["execution_id"] == old and result["status"] == "unknown"
    assert result["reason"] == "execution_not_confirmed" and result["pending"]


async def test_event_ingestion_time_does_not_replace_old_native_time(native):
    operation, _ = await prepare(native)
    fact = event(
        operation=operation.id,
        payload={"occurred_at": "2020-01-01T00:00:00Z", "text": "old result"},
    )
    await native.repository.receive_events(native.device["device_id"], [fact])
    page = await native.repository.event_page(operation.id, 0)
    assert page[0]["received_at"] != fact["payload"]["occurred_at"]
    assert page[0]["payload"]["occurred_at"] == fact["payload"]["occurred_at"]


@pytest.mark.parametrize("native_times", [{"startedAt": 1700000000, "completedAt": 1700000060}, {}])
async def test_late_native_completion_keeps_native_received_and_main_observation_times(
    case, native_times
):
    native, _ = await owned(case)
    operation, _ = await prepare(
        native,
        identity="late-session-watch",
        action="watch",
        native_execution_id=None,
        command={"id": "late-session-watch", "action": "watch", "observation_scope": "session"},
        lease=case.control.lease,
    )
    await native.repository.response(
        operation.id,
        {
            "status": "running",
            "pending": True,
            "session_id": "native-thread",
        },
    )
    projected = []

    async def collect(kind, value):
        projected.append((kind, value))

    driver = CodexDriver(Instance("profile", "codex", "isolated", "unused"), collect)
    driver.watches[operation.id] = "native-thread"
    driver.watch_turns[operation.id] = None
    # An old original turn is observed now. Do not invent an occurrence time
    # when the native protocol omitted it, or call receipt arrival "just happened".
    await driver._notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "native-thread",
                "turn": {"id": "old-turn", "status": "completed", "items": [], **native_times},
            },
        }
    )
    kind, value = projected[0]
    assert kind == "progress" and value["payload"]["execution_status"] == "completed"
    fact = event(
        "old-native-completion",
        operation=operation.id,
        kind=kind,
        execution_id="old-turn",
        sequence=7,
        payload=value["payload"],
        operation_state={
            "status": "idle",
            "pending": True,
            "state_sequence": 7,
            "session_id": "native-thread",
            "execution_id": "old-turn",
        },
    )
    await native.repository.receive_events(native.device["device_id"], [fact])
    page = await native.repository.event_page(operation.id, 0)
    messages = await case.control.take_inputs("actual-main-observation")
    observed_at = datetime.now(UTC)
    material = next(
        json.loads(message.content.split("\n", 1)[1])
        for message in messages
        if "external_agent_event" in message.content
    )
    received_at = datetime.fromisoformat(material["received_at"])
    assert received_at <= observed_at
    assert material["received_at"] == page[0]["received_at"]
    assert material["sequence"] == 7
    assert material["operation_id"] == operation.id
    for name in ("startedAt", "completedAt"):
        assert (name in material["payload"]) is (name in native_times)
        if name in native_times:
            assert material["payload"][name] == native_times[name] == page[0]["payload"][name]
    assert case.control.current["id"] == operation.work_id
