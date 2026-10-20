"""Use a separately operated real Manager; never substitute its display or runtime."""

import asyncio
import sqlite3
from pathlib import Path
from uuid import uuid4

from yuki.character.repository import CharacterRepository


class DesktopExternalComputer:
    native_work = True
    external = True

    def __init__(self, container, socket: Path, manager_root: Path):
        self.container = container
        self.root = manager_root
        container.sandbox_client.socket = socket

    async def start(self, person):
        status = await self.container.sandbox_client.execute(
            "environment_status", {}, request_id=str(uuid4())
        )
        assert status.get("ready") is True, status
        await self.container.sandbox_client.execute("display_wake", {}, request_id=str(uuid4()))
        async with asyncio.timeout(90):
            while True:
                display = await self.container.sandbox_client.execute(
                    "display_status", {}, request_id=str(uuid4())
                )
                if display.get("display") == "running":
                    break
                assert display.get("display") != "unavailable", display
                await asyncio.sleep(0.2)
        await CharacterRepository(self.container.client_api.identity).set_owner(
            str(uuid4()), person, str(uuid4()), granted=True
        )
        await self.container.sandbox_completions.start()
        await self.container.client_api.computer.start()

    async def close(self):
        await self.container.client_api.computer.close()
        await self.container.sandbox_completions.close()

    def rows(self, query):
        # Observation only: this process never owns, starts or stops the Manager.
        with sqlite3.connect((self.root / "jobs.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(query)]

    async def work_facts(self):
        jobs, waits = await asyncio.gather(
            asyncio.to_thread(
                self.rows, "SELECT id FROM environment_jobs WHERE kind='terminal_exec'"
            ),
            asyncio.to_thread(self.rows, "SELECT * FROM help_requests"),
        )
        runs = [
            await self.container.sandbox_client.execute(
                "terminal_read", {"run_id": row["id"]}, request_id=str(uuid4())
            )
            for row in jobs
        ]
        snapshot = await self.container.sandbox_client.execute(
            "control_get", {}, request_id=str(uuid4())
        )
        return {
            "manager_jobs": [
                {"run_id": run["run_id"], "status": run["status"], "output": run.get("output", "")}
                for run in runs
            ],
            "waits": waits,
            "control": snapshot["control"],
        }

    def attach(self, app):
        @app.get("/__test/computer")
        async def facts():
            status = await self.container.sandbox_client.execute(
                "environment_status", {}, request_id=str(uuid4())
            )
            return {"external_manager": True, "environment": status, **await self.work_facts()}
