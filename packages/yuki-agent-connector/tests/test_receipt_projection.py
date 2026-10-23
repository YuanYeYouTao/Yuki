from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3

import pytest

from yuki_agent_connector.config import Config
from yuki_agent_connector.connector import Connector
from yuki_agent_connector.journal import Journal


def operation(key="watch", action="watch", **values):
    return {
        "id": key,
        "instance_id": "local",
        "action": action,
        "session_id": "session",
        "arguments": {},
        **values,
    }


def event(key, status, *, kind="progress", **values):
    return {
        "type": "event",
        "event_id": key,
        "instance_id": "local",
        "operation_id": "watch",
        "session_id": "session",
        "kind": kind,
        "payload": {"status": status, **values},
    }


@pytest.fixture
def journal(tmp_path):
    with contextlib.closing(Journal(tmp_path / "journal.db", namespace="fixture")) as value:
        yield value


def test_loss_updates_projection_without_readmission_and_terminal_cannot_regress(journal):
    journal.begin(operation())
    journal.complete("watch", {"result": {"admitted": True, "status": "running", "pending": True}})
    journal.append(event("loss", "unknown", reason="native_event_stream_lost"))
    lost = journal.operation("watch")
    assert lost["status"] == "unknown" and lost["pending"] and lost["admitted"]
    wire = json.loads(journal.next_event()[2])
    assert wire["operation_state"] == lost and wire["sequence"] == lost["state_sequence"]
    journal.append(event("recovered", "running"))
    assert "reason" not in journal.operation("watch")
    journal.append(event("done", "completed", kind="completed"))
    journal.append(event("late", "running", admitted=True))
    assert journal.operation("watch")["status"] == "completed"
    assert journal.operation("watch")["pending"] is False
    assert journal.observations() == []


def test_unknown_admission_is_recoverable_even_without_response_session(journal):
    original = operation("unknown", "prompt")
    journal.begin(original)
    # A crash before the command response cannot remove its original intent.
    assert journal.observations()[0][0] == original
    journal.complete("unknown", {"result": {"status": "unknown", "reason": "connection_lost"}})
    recovered_command, receipt = journal.observations()[0]
    assert recovered_command == original and receipt["session_id"] == "session"
    assert "admitted" not in receipt


def test_session_observation_keeps_turn_identity_in_event_only(journal):
    journal.begin(operation(observation_scope="session"))
    journal.complete("watch", {"result": {"status": "idle", "admitted": True, "pending": True}})
    journal.append({**event("turn-1", "running"), "execution_id": "turn-1"})
    wire = json.loads(journal.next_event()[2])
    assert wire["execution_id"] == "turn-1"
    assert "execution_id" not in wire["operation_state"]
    assert journal.observations()


def test_detach_is_not_resurrected_by_late_event_or_delayed_admission(journal):
    journal.begin(operation())
    detached = operation("detach", "detach", arguments={"operation_id": "watch"})
    journal.begin(detached)
    journal.complete("detach", {"result": {"detached": True, "admitted": True}})
    journal.append(event("late", "running", admitted=True))
    journal.complete("watch", {"result": {"status": "running", "pending": True}})
    assert journal.operation("watch")["status"] == "detached"
    assert journal.observations() == []


def test_old_journal_upgrade_preserves_scope_response_unknown_and_detach(tmp_path):
    path = tmp_path / "legacy.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE installation(namespace TEXT PRIMARY KEY);
        INSERT INTO installation VALUES ('fixture');
        CREATE TABLE operations(id TEXT PRIMARY KEY, command TEXT NOT NULL, response TEXT,
            observing INTEGER NOT NULL DEFAULT 0, receipt TEXT);
        CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, event TEXT);
    """)
    commands = [
        operation(),
        operation("unknown", "prompt"),
        operation("detach", "detach", arguments={"operation_id": "watch"}),
    ]
    responses = [
        {"result": {"status": "running", "session_id": "session", "admitted": True}},
        {"result": {"status": "unknown"}},
        {"result": {"admitted": True, "detached": True}},
    ]
    for command, response in zip(commands, responses, strict=True):
        db.execute(
            "INSERT INTO operations VALUES (?, ?, ?, 0, NULL)",
            (command["id"], json.dumps(command), json.dumps(response)),
        )
    db.execute(
        "INSERT INTO events(id,event) VALUES (?,?)", ("late", json.dumps(event("late", "running")))
    )
    db.commit()
    db.close()
    with contextlib.closing(Journal(path, namespace="fixture")) as current:
        saved = current.db.execute(
            "SELECT command,response FROM operations WHERE id='watch'"
        ).fetchone()
        assert json.loads(saved[0])["observation_scope"] == "execution"
        assert json.loads(saved[1]) == responses[0]
        assert current.operation("watch")["status"] == "detached"
        assert [command["id"] for command, _ in current.observations()] == ["unknown"]
        assert json.loads(current.next_event()[2])["operation_state"]["status"] == "detached"
        current.begin(operation("new", observation_scope="session"))
    with contextlib.closing(Journal(path, namespace="fixture")) as reopened:
        assert reopened.observation("new")[0]["observation_scope"] == "session"


@pytest.mark.asyncio
@pytest.mark.parametrize("producer", ["_events", "_catalog"])
async def test_producer_failure_ends_connection_instead_of_leaving_fake_online(journal, producer):
    connector = Connector(Config("ws://localhost", "device", "fixture"), journal)
    stopped = asyncio.Event()

    class Socket:
        async def send(self, value):
            pass

        async def __aiter__(self):
            try:
                await asyncio.Event().wait()
                yield ""
            finally:
                stopped.set()

    async def failed(socket):
        raise OSError("producer lost")

    setattr(connector, producer, failed)
    with pytest.raises(OSError, match="producer lost"):
        await asyncio.wait_for(connector.connection(Socket()), 1)
    assert connector.stream is None and stopped.is_set()


def test_early_milestone_allows_later_admission_to_supply_status(journal):
    journal.begin(operation())
    journal.append(
        {
            **event("early", "running"),
            "payload": {"admitted": True, "item": {"type": "agentMessage"}},
        }
    )
    early = journal.operation("watch")["state_sequence"]
    journal.complete("watch", {"result": {"status": "running", "pending": True, "admitted": True}})
    assert journal.operation("watch")["status"] == "running"
    assert journal.operation("watch")["state_sequence"] > early
    assert "reason" not in journal.operation("watch")


def test_known_loss_wins_over_delayed_admission(journal):
    journal.begin(operation(execution_id="original-turn"))
    assert journal.operation("watch")["execution_id"] == "original-turn"
    journal.append(event("lost", "unknown", reason="native_event_stream_lost"))
    journal.complete("watch", {"result": {"status": "running", "pending": True, "admitted": True}})
    assert journal.operation("watch")["status"] == "unknown"
    assert journal.operation("watch")["admitted"] is True
    assert journal.operation("watch")["reason"] == "native_event_stream_lost"


def test_lost_detach_response_query_advances_original_projection(journal):
    journal.begin(operation())
    journal.complete("watch", {"result": {"status": "running", "pending": True}})
    before = journal.operation("watch")["state_sequence"]
    journal.begin(operation("detach", "detach", arguments={"operation_id": "watch"}))
    journal.complete("detach", {"result": {"admitted": True, "detached": True}})
    assert journal.operation("watch")["status"] == "detached"
    assert journal.operation("watch")["state_sequence"] > before


def test_stale_snapshot_cannot_replace_progress_but_original_terminal_still_counts(journal):
    journal.begin(operation())
    initial = journal.operation("watch")["state_sequence"]
    journal.append(event("new", "running", admitted=True))
    journal.append({**event("old", "unknown"), "snapshot_sequence": initial})
    assert journal.operation("watch")["status"] == "running"
    journal.append(
        {**event("terminal", "completed", kind="completed"), "snapshot_sequence": initial}
    )
    assert journal.operation("watch")["status"] == "completed"
    assert journal.observations() == []


def test_detach_after_native_terminal_keeps_terminal_and_stops_event_upload(journal):
    journal.begin(operation())
    journal.append(event("terminal", "completed", kind="completed"))
    journal.ack("terminal")
    journal.begin(operation("detach", "detach", arguments={"operation_id": "watch"}))
    journal.complete("detach", {"result": {"admitted": True, "detached": True}})
    journal.append(event("late", "running"))
    assert journal.next_event() is None
    assert journal.operation("watch")["status"] == "completed"
    assert journal.operation("watch")["detached"] is True


@pytest.mark.asyncio
async def test_command_journal_failure_closes_transport_and_preserves_uncertain_original(journal):
    connector = Connector(Config("ws://localhost", "device", "fixture"), journal)
    closed = []

    class Socket:
        async def send(self, value):
            raise AssertionError("No response can be claimed after failed durable commit")

        async def close(self, **kwargs):
            closed.append(kwargs)

    class Driver:
        async def execute(self, value):
            return {"admitted": True, "status": "running"}

    connector.drivers["local"] = Driver()

    def failed_complete(key, response):
        raise sqlite3.OperationalError("disk full")

    journal.complete = failed_complete
    with pytest.raises(sqlite3.OperationalError):
        await connector._command(operation(), Socket())
    assert closed[0]["code"] == 1011
    assert journal.operation("watch")["status"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_driver", [True, False])
async def test_native_unavailability_does_not_prevent_local_detach(journal, has_driver):
    journal.begin(operation())
    journal.complete("watch", {"result": {"status": "running", "pending": True}})
    connector = Connector(Config("ws://localhost", "device", "fixture"), journal)
    connector.errors["local"] = "native_disconnected"

    class Driver:
        async def execute(self, command):
            assert command["action"] == "detach"
            return {"status": "detached", "admitted": True, "detached": True, "pending": False}

    if has_driver:
        connector.drivers["local"] = Driver()
    response = await connector.dispatch(
        operation("detach", "detach", arguments={"operation_id": "watch"})
    )
    assert response["result"]["status"] == "detached"
    assert journal.operation("watch")["detached"] is True
    assert journal.observations() == []


@pytest.mark.asyncio
async def test_rejected_native_command_returns_same_ordered_receipt_as_get(journal):
    from yuki_agent_connector.drivers import DriverError

    class Driver:
        async def execute(self, command):
            raise DriverError("unsupported_action", "Native capability unavailable")

    connector = Connector(Config("ws://localhost", "device", "fixture"), journal)
    connector.drivers["local"] = Driver()
    response = await connector.dispatch(operation())
    assert response["result"] == journal.operation("watch")
    assert response["result"]["status"] == "failed"
    assert response["result"]["pending"] is False
    assert response["result"]["admitted"] is False
    assert response["result"]["state_sequence"] > 0
    assert response["result"]["error"]["code"] == "unsupported_action"
