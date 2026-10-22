"""The real Manager entry owns its SQLite connection during startup failures."""

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

from yuki.sandbox import manager as entry
from yuki.sandbox import persistent
from yuki.workspace.store import WorkspaceStore


@pytest.mark.parametrize(
    "failure_stage",
    ["recover_cancel", "server_start", "socket_chmod", "signal_handler", "serving_cancel"],
)
async def test_manager_startup_failure_closes_its_real_job_store(
    tmp_path, monkeypatch, failure_stage
):
    owners = []
    workers = []
    exits = []
    failure = (
        asyncio.CancelledError("cancelled during recovery")
        if failure_stage in {"recover_cancel", "serving_cancel"}
        else OSError("socket startup failed")
    )

    class Manager(persistent.PersistentManager):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            owners.append(self)

        async def recover(self):
            if failure_stage == "recover_cancel":
                raise failure

        async def worker(self):
            await asyncio.Event().wait()

        async def close(self):
            exits.append("manager")
            await super().close()

    class Server:
        def close(self):
            exits.append("server")

        async def wait_closed(self):
            pass

        async def serve_forever(self):
            raise failure

    async def server(*args, **kwargs):
        if failure_stage == "server_start":
            raise failure
        (tmp_path / "socket").touch()
        return Server()

    chmod = Path.chmod

    def chmod_socket(path, *args, **kwargs):
        if failure_stage == "socket_chmod" and path == tmp_path / "socket":
            raise failure
        return chmod(path, *args, **kwargs)

    def signal_handler(*args, **kwargs):
        if failure_stage == "signal_handler":
            raise failure

    create_task = asyncio.create_task

    def start_worker(coroutine, *args, **kwargs):
        task = create_task(coroutine, *args, **kwargs)
        workers.append(task)
        return task

    monkeypatch.setattr(persistent, "PersistentManager", Manager)
    monkeypatch.setattr(asyncio, "start_unix_server", server, raising=False)
    monkeypatch.setattr(asyncio, "create_task", start_worker)
    monkeypatch.setattr(Path, "chmod", chmod_socket)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", signal_handler)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sandbox-manager",
            "--root",
            str(tmp_path / "jobs"),
            "--workspace",
            str(tmp_path / "workspace"),
            "--socket",
            str(tmp_path / "socket"),
            "--image",
            "unused-image",
            "--persistent-home",
            str(tmp_path / "home"),
        ],
    )
    try:
        if failure_stage == "serving_cancel":
            await entry.main()
        else:
            with pytest.raises(type(failure)) as caught:
                await entry.main()
            assert caught.value is failure
        assert len(owners) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            owners[0].db.execute("SELECT 1")
        assert all(task.done() and task.cancelled() for task in workers)
        assert exits == (
            ["server", "manager"]
            if failure_stage in {"socket_chmod", "signal_handler", "serving_cancel"}
            else ["manager"]
        )
    finally:
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        for owner in owners:
            await owner.close()


@pytest.mark.parametrize("failure_stage", ["name", "schema"])
def test_manager_failed_constructor_releases_only_its_job_connection(
    tmp_path, monkeypatch, failure_stage
):
    opened = []
    connect = sqlite3.connect
    schema_failure = sqlite3.OperationalError("schema initialization failed")

    class Connection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if failure_stage == "schema" and sql.startswith("CREATE TABLE IF NOT EXISTS jobs"):
                raise schema_failure
            return super().execute(sql, *args, **kwargs)

    def open_database(path, *args, **kwargs):
        if str(path).endswith("jobs.sqlite3"):
            kwargs["factory"] = Connection
            result = connect(path, *args, **kwargs)
            opened.append(result)
            return result
        return connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", open_database)
    with pytest.raises((ValueError, sqlite3.OperationalError)):
        persistent.PersistentManager(
            tmp_path / "jobs",
            WorkspaceStore(tmp_path / "workspace"),
            "unused-image",
            "unused-network",
            "",
            tmp_path / "home",
            name="invalid" if failure_stage == "name" else "yuki-environment",
        )
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
