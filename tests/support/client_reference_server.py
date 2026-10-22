"""Separate-process personal API fixture, using original services and a fake model."""

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import FastAPI
from tests.conftest import make_settings
from tests.integration.test_current_material_main_contract import _catalog, _profile
from tests.support.model_profiles import write_fake_profiles

from yuki.client_api.http import attach_client_api
from yuki.container import ApplicationContainer
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.pool import ModelClientPool
from yuki.persistence.database import Database


async def serve(root: Path, database_url: str, port: int, suppress_hints: bool) -> None:
    database = Database(database_url)
    profiles = write_fake_profiles(root / "models.toml")
    app = ApplicationContainer(
        make_settings(
            database_url,
            model_profiles_file=profiles,
            plugin_system_enabled=False,
            plugin_directory=root / "plugins",
            workspace_directory=root / "workspace",
            conversation_media_cache_directory=root / "media",
            social_transfer_directory=root / "transfer",
            runtime_work_enabled=True,
        ),
        database=database,
    )
    await app.character.initialize("Yuki")

    scripted = iter(
        (
            ("task_control", {"action": "accept", "goal": "persist original result"}),
            ("send", {"text": "persistent reply"}),
            ("task_control", {"action": "complete", "result": "original result complete"}),
        )
    )

    def respond(request):
        import json

        name, arguments = next(scripted)
        return ChatResponse(
            content="",
            latency_seconds=0,
            tool_calls=(ToolCall(str(uuid4()), ToolFunction(name, json.dumps(arguments))),),
        )

    app.models.apply_catalog(
        _catalog(_profile()), ModelClientPool(injected_profiles={"main": FakeLLMProvider(respond)})
    )
    await app.model_invocations.writer.start()
    await app.app_gateway.start()
    await app._freeze_main_manifest()
    await app.work_scheduler.start()
    if suppress_hints:
        # Failure injection: facts commit, but this process cannot deliver its hints.
        database.commit_hints.publish = lambda: None
    http = FastAPI()
    attach_client_api(http, lambda: app.client_api)

    @http.get("/healthz")
    async def ready():
        return {"ok": True}

    try:
        await uvicorn.Server(
            uvicorn.Config(
                http, host="127.0.0.1", port=port, log_level="critical", timeout_graceful_shutdown=1
            )
        ).serve()
    finally:
        assert not await app.lifecycle._close_entries(app.lifecycle._entries)


if __name__ == "__main__":
    asyncio.run(serve(Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4] == "suppress"))
