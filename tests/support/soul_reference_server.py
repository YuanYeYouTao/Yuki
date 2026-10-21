"""Loopback SOUL/Desktop fixture with the real container, SQLite and device API.

Run as ``python -m tests.support.soul_reference_server --root DIR
--state-file FILE --port 0``. The state file contains the prefixed base URL and
one-time pairing codes for two real Persons. Test controls only exist here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import FastAPI
from tests.conftest import make_settings
from tests.support.model_profiles import write_fake_profiles

from yuki.client_api.http import attach_client_api
from yuki.container import ApplicationContainer
from yuki.identity.canonical_repository import create_presence
from yuki.identity.db_models import CanonicalPersonModel
from yuki.persistence.database import Database


async def serve(root: Path, state_file: Path, port: int, base_path: str) -> None:
    soul_file = root / "character" / "SOUL.md"
    database = Database(f"sqlite+aiosqlite:///{(root / 'yuki.sqlite3').as_posix()}")
    runtime = ApplicationContainer(
        make_settings(
            database.url,
            soul_file=soul_file,
            model_profiles_file=write_fake_profiles(root / "models.toml"),
            plugin_system_enabled=False,
            plugin_directory=root / "plugins",
            workspace_directory=root / "workspace",
            conversation_media_cache_directory=root / "media",
            social_transfer_directory=root / "transfer",
            runtime_work_enabled=True,
            telegram_bot_token="",
        ),
        database=database,
    )
    try:
        await database.create_schema()
        await runtime.character.initialize("初始角色")
        operator_id, owner_id, nonowner_id = (str(uuid4()) for _ in range(3))
        now = datetime.now(UTC)
        async with database.immediate_session() as session:
            session.add_all(
                CanonicalPersonModel(
                    id=person_id, display_name=name, created_at=now, updated_at=now
                )
                for person_id, name in ((owner_id, "Owner"), (nonowner_id, "Visitor"))
            )
            await create_presence(session, external_account_id="yuki-app", platform="app", now=now)
        await runtime.character.set_owner(operator_id, owner_id, str(uuid4()), granted=True)

        async def issue_code(person_id: str) -> dict:
            return await runtime.client_identity.issue_for_operator(
                person_id, operator_id, str(uuid4())
            )

        owner_code = await issue_code(owner_id)
        nonowner_code = await issue_code(nonowner_id)
        await runtime.model_invocations.writer.start()
        await runtime.app_gateway.start()
        await runtime._freeze_main_manifest()
        await runtime.work_scheduler.start()
        api = FastAPI()
        attach_client_api(api, lambda: runtime.client_api)

        @api.get("/healthz")
        async def ready() -> dict:
            return {"ok": True}

        @api.post("/__test/link-code")
        async def owner_link_code() -> dict:
            return await issue_code(owner_id)

        @api.post("/__test/revoke-owner")
        async def revoke_owner() -> dict:
            return await runtime.character.set_owner(
                operator_id, owner_id, str(uuid4()), granted=False
            )

        http = FastAPI()
        http.mount(base_path or "/", api)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))
            actual_port = listener.getsockname()[1]
            server = uvicorn.Server(
                uvicorn.Config(
                    http,
                    host="127.0.0.1",
                    port=actual_port,
                    log_level="critical",
                    timeout_graceful_shutdown=1,
                )
            )
            async with asyncio.TaskGroup() as tasks:
                serving = tasks.create_task(server.serve(sockets=[listener]))
                while not server.started:
                    if serving.done():
                        await serving
                        return
                    await asyncio.sleep(0.01)
                state = {
                    "base_url": f"http://127.0.0.1:{actual_port}{base_path}",
                    "owner_pairing_code": owner_code["code"],
                    "nonowner_pairing_code": nonowner_code["code"],
                }
                await asyncio.to_thread(state_file.write_text, json.dumps(state), encoding="utf-8")
                print(json.dumps(state), flush=True)
                await serving
    finally:
        assert not await runtime.lifecycle._close_entries(runtime.lifecycle._entries)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--base-path", default="/role/test")
    arguments = parser.parse_args()
    root, state_file = arguments.root.resolve(), arguments.state_file.resolve()
    root.mkdir(parents=True, exist_ok=True)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    # All relative runtime stores belong to this isolated fixture process.
    os.chdir(root)
    soul_file = root / "character" / "SOUL.md"
    soul_file.parent.mkdir(parents=True, exist_ok=True)
    if not soul_file.exists():
        soul_file.write_bytes("你是 {{name}}，喜欢清晰、简短地交流。\n".encode())
    asyncio.run(
        serve(
            root,
            state_file,
            arguments.port,
            arguments.base_path.rstrip("/"),
        )
    )
