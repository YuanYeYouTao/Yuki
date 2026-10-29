"""Terminal output stays in the Connector log; only execution facts are durable."""

import json
from unittest.mock import patch

import pytest
from tests.unit.test_local_device_bridge import setup_device


async def start_terminal(c):
    c.connection.request.side_effect = None
    c.connection.request.return_value = {
        "status": "running",
        "pending": True,
        "admitted": True,
        "shell": "powershell.exe",
        "cwd": "C:/Users/test",
    }
    return await c.local.execute(
        "terminal_exec",
        {"device_id": c.device_id, "command": "original command"},
        request_id="original-log-owner",
        source=c.source,
    )


async def test_running_read_returns_output_without_database_write_or_commit_hint(
    database, tmp_path
):
    c = await setup_device(database, tmp_path)
    try:
        started = await start_terminal(c)
        original = await c.service.tasks.get("original-log-owner")
        hints = database.commit_hints.subscribe()
        with patch.object(
            database, "immediate_session", wraps=database.immediate_session
        ) as writer:
            for cursor, output in ((0, "first-雪"), (9, "second-雪"), (19, "")):
                c.connection.request.return_value = {
                    "run_id": started["run_id"],
                    "status": "running",
                    "pending": True,
                    "output": output,
                    "output_offset": cursor,
                    "next_cursor": cursor + len(output.encode()),
                    "eof": True,
                }
                result = await c.local.read(c.env.person, started["run_id"], cursor)
                assert result["output"] == output
                assert result["next_cursor"] == cursor + len(output.encode())
        stored = await c.service.tasks.get("original-log-owner")
        assert stored.progress_json == original.progress_json
        assert stored.updated_at == original.updated_at
        assert writer.call_count == 0
        assert hints.empty()
        database.commit_hints.unsubscribe(hints)
    finally:
        await c.service.close()


async def test_first_completion_read_is_saved_repeated_read_is_readonly_and_conflict_survives(
    database, tmp_path
):
    c = await setup_device(database, tmp_path)
    try:
        started = await start_terminal(c)
        completion = {
            "run_id": started["run_id"],
            "status": "succeeded",
            "pending": False,
            "exit_code": 0,
        }
        c.connection.request.return_value = {
            **completion,
            "completion": completion,
            "output": "original tail",
            "next_cursor": 13,
        }
        first = await c.local.read(c.env.person, started["run_id"])
        stored = await c.service.tasks.get("original-log-owner")
        assert first["output"] == "original tail"
        assert stored.status == "completed"
        assert json.loads(stored.completion_json) == completion
        assert "output" not in json.loads(stored.progress_json)

        hints = database.commit_hints.subscribe()
        c.connection.request.return_value = {
            **completion,
            "completion": dict(reversed(list(completion.items()))),
            "output": "",
            "next_cursor": 13,
        }
        with patch.object(
            database, "immediate_session", wraps=database.immediate_session
        ) as writer:
            repeated = await c.local.read(c.env.person, started["run_id"], 13)
        assert repeated["output"] == "" and repeated["status"] == "succeeded"
        assert writer.call_count == 0
        assert hints.empty()

        conflicting = {**completion, "status": "failed", "exit_code": 1}
        c.connection.request.return_value = {"completion": conflicting}
        with pytest.raises(ValueError, match="conflicting_task_completion"):
            await c.local.read(c.env.person, started["run_id"])
        after = await c.service.tasks.get("original-log-owner")
        assert after.completion_json == stored.completion_json
        assert after.updated_at == stored.updated_at
        assert hints.empty()
        database.commit_hints.unsubscribe(hints)
    finally:
        await c.service.close()


async def test_unchanged_original_ack_query_does_not_open_writer(database, tmp_path):
    c = await setup_device(database, tmp_path)
    try:
        started = await start_terminal(c)
        original = await c.service.tasks.get("original-log-owner")
        hints = database.commit_hints.subscribe()
        with patch.object(
            database, "immediate_session", wraps=database.immediate_session
        ) as writer:
            result = await c.local.execute(
                "get_code_run_by_request",
                {"request_id": "original-log-owner"},
                request_id="query-original-ack",
                source=c.source,
            )
        assert result["run_id"] == started["run_id"] and result["status"] == "running"
        assert writer.call_count == 0
        assert hints.empty()
        stored = await c.service.tasks.get("original-log-owner")
        assert stored.progress_json == original.progress_json
        database.commit_hints.unsubscribe(hints)
    finally:
        await c.service.close()
