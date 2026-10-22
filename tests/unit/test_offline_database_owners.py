"""Offline read utilities release their actual native SQLite connections."""

import sqlite3
from pathlib import Path

import pytest
from scripts.prepare_person_authority import export_candidates
from scripts.verify_work_backup import verify


@pytest.mark.parametrize("utility", ["backup", "authority"])
def test_offline_reader_closes_its_native_connection(database, tmp_path, monkeypatch, utility):
    path = Path(database.url.removeprefix("sqlite+aiosqlite:///"))
    connect = sqlite3.connect
    connections = []

    def observed_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", observed_connect)
    if utility == "backup":
        assert verify(path, tmp_path) == {"protocol_objects": 0, "tool_artifacts": 0}
    else:
        assert export_candidates(path, ())["candidates"] == []
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
