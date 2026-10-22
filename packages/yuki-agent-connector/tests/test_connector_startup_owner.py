from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from typing import Any, ClassVar

import pytest

from yuki_agent_connector.config import Config, Instance
from yuki_agent_connector.connector import Connector
from yuki_agent_connector.journal import Journal


@pytest.mark.asyncio
async def test_cancelled_startup_closes_started_and_initializing_owned_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = {name: asyncio.Event() for name in ("started", "initializing")}
    processes: dict[str, asyncio.subprocess.Process] = {}
    stopped: set[str] = set()

    class Driver:
        capabilities: ClassVar[list[str]] = []

        def __init__(self, spec: Instance, emit: Any) -> None:
            self.spec = spec

        async def start(self) -> None:
            processes[self.spec.id] = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(60)"
            )
            ready[self.spec.id].set()
            if self.spec.id == "initializing":
                await asyncio.Event().wait()

        async def stop(self) -> None:
            process = processes[self.spec.id]
            if process.returncode is None:
                process.terminate()
            await process.wait()
            stopped.add(self.spec.id)

    monkeypatch.setattr("yuki_agent_connector.connector.make_driver", Driver)
    journal = Journal(tmp_path / "journal.sqlite3", namespace="test/device")
    connector = Connector(
        Config(
            "ws://localhost/",
            "device",
            "test-only-token",
            instances=[Instance(name, "fake", name, "fake") for name in ready],
        ),
        journal,
    )
    task = asyncio.create_task(connector.run())
    try:
        async with asyncio.timeout(10):
            await asyncio.gather(*(event.wait() for event in ready.values()))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert stopped == set(ready)
        assert all(process.returncode is not None for process in processes.values())
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        for process in processes.values():
            if process.returncode is None:
                process.terminate()
            await process.wait()
        journal.close()
