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
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import FastAPI
from tests.conftest import make_settings
from tests.support.model_profiles import write_fake_profiles

from yuki.character.db_models import CharacterOwnerGrantModel
from yuki.client_api.http import attach_client_api
from yuki.container import ApplicationContainer
from yuki.identity.canonical_repository import create_presence
from yuki.identity.db_models import CanonicalPersonModel
from yuki.persistence.database import Database


async def serve(
    root: Path,
    state_file: Path,
    port: int,
    base_path: str,
    *,
    desktop: bool = False,
    real_model_source: Path | None = None,
) -> None:
    soul_file = root / "character" / "SOUL.md"
    database_path = root / "yuki.sqlite3"
    first_start = not database_path.exists()
    database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")
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
            automation_enabled=desktop and os.environ.get("YUKI_DESKTOP_CHARACTER_FIXTURE") == "1",
            self_memory_enabled=desktop and os.environ.get("YUKI_DESKTOP_CHARACTER_FIXTURE") == "1",
            telegram_bot_token="",
            asr_enabled=desktop and real_model_source is None,
            web_mode="disabled" if real_model_source is not None else "native",
        ),
        database=database,
    )
    computer_fixture = None
    computer_scenario = None
    native_scenario = None
    fault_fixture = None
    work_script: list = []
    try:
        if first_start:
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
                await create_presence(
                    session, external_account_id="yuki-app", platform="app", now=now
                )
            await runtime.character.set_owner(operator_id, owner_id, str(uuid4()), granted=True)
        else:
            original_state = json.loads(
                await asyncio.to_thread(state_file.read_text, encoding="utf-8")
            )
            owner_id = original_state["owner_person_id"]
            nonowner_id = original_state["nonowner_person_id"]
            async with database.read_session() as session:
                assert await session.get(CanonicalPersonModel, owner_id) is not None
                assert await session.get(CanonicalPersonModel, nonowner_id) is not None
                grant = await session.get(CharacterOwnerGrantModel, owner_id)
                assert grant is not None
                operator_id = grant.granted_by_principal

        async def issue_code(person_id: str) -> dict:
            return await runtime.client_identity.issue_for_operator(
                person_id, operator_id, str(uuid4())
            )

        owner_code = await issue_code(owner_id)
        nonowner_code = await issue_code(nonowner_id)
        work_model_gate = asyncio.Event()
        work_model_gate.set()
        if desktop and real_model_source is None:
            if os.environ.get("YUKI_DESKTOP_NATIVE_FIXTURE") == "1":
                from tests.support.desktop_native_work_fixture import DesktopNativeWorkScenario

                native_scenario = DesktopNativeWorkScenario(runtime)
            manager_socket = os.environ.get("YUKI_DESKTOP_MANAGER_SOCKET")
            if manager_socket:
                from tests.support.desktop_external_computer import DesktopExternalComputer

                computer_fixture = DesktopExternalComputer(
                    runtime, Path(manager_socket), Path(os.environ["YUKI_DESKTOP_MANAGER_ROOT"])
                )
            elif os.environ.get("YUKI_DESKTOP_HEADED_FIXTURE") == "1":
                from tests.support.desktop_headed_computer_fixture import (
                    DesktopHeadedComputerFixture,
                )

                computer_fixture = DesktopHeadedComputerFixture(root, runtime)
            elif os.environ.get("YUKI_DESKTOP_COMPUTER_FIXTURE") == "1":
                from tests.support.desktop_computer_fixture import DesktopComputerFixture

                computer_fixture = DesktopComputerFixture(
                    root,
                    runtime,
                    native_work=os.environ.get("YUKI_DESKTOP_COMPUTER_WORK_FIXTURE") == "1",
                )
            from tests.support.desktop_reference_server import ControlledASR

            asr_provider = ControlledASR()
            runtime.asr._provider = asr_provider
            from tests.integration.test_current_material_main_contract import _catalog, _profile

            from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
            from yuki.llm.fake import FakeLLMProvider
            from yuki.model_runtime.pool import ModelClientPool

            def respond(request):
                from yuki.runtime.work_activation import current_work_control

                if computer_scenario is not None:
                    response = computer_scenario.respond(request)
                    if response is not None:
                        return response
                if work_script:
                    name, arguments = work_script.pop(0)
                    return ChatResponse(
                        content="",
                        latency_seconds=0,
                        tool_calls=(
                            ToolCall(str(uuid4()), ToolFunction(name, json.dumps(arguments))),
                        ),
                    )
                has_receipt = any(
                    message.role == "tool" or message.tool_calls for message in request.messages
                )
                control = current_work_control.get()
                if (
                    has_receipt
                    and control is not None
                    and control.current is not None
                    and not control.ending
                ):
                    return ChatResponse(
                        content="",
                        latency_seconds=0,
                        tool_calls=(
                            ToolCall(
                                str(uuid4()),
                                ToolFunction(
                                    "task_control",
                                    json.dumps(
                                        {
                                            "action": "complete",
                                            "result": "Desktop reference complete",
                                        }
                                    ),
                                ),
                            ),
                        ),
                    )
                if has_receipt:
                    return ChatResponse(content="已完成。", latency_seconds=0)
                return ChatResponse(
                    content="",
                    latency_seconds=0,
                    tool_calls=(
                        ToolCall(
                            str(uuid4()),
                            ToolFunction("send", json.dumps({"text": "Desktop reference reply"})),
                        ),
                    ),
                )

            class DesktopModel(FakeLLMProvider):
                async def _complete(self, request):
                    from yuki.runtime.work_activation import current_work_control

                    control = current_work_control.get()
                    if control is not None and control.current is not None:
                        await work_model_gate.wait()
                    if native_scenario is not None:
                        response = await native_scenario.respond(request)
                        if response is not None:
                            return response
                    if fault_fixture is not None:
                        response = await fault_fixture.respond(request)
                        if response is not None:
                            return response
                    return await super()._complete(request)

            desktop_model = DesktopModel(respond)
            runtime.models.apply_catalog(
                _catalog(_profile()),
                ModelClientPool(injected_profiles={"main": desktop_model}),
            )
        elif real_model_source is not None:
            from tests.support.desktop_reference_server import load_real_models

            catalog, pool = load_real_models(real_model_source)
            runtime.models.apply_catalog(catalog, pool)
        await runtime.model_invocations.writer.start()
        await runtime.app_gateway.start()
        if computer_fixture is not None:
            await computer_fixture.start(owner_id)
            if computer_fixture.native_work:
                from tests.support.desktop_computer_work_fixture import DesktopComputerWorkScenario

                computer_scenario = DesktopComputerWorkScenario(root, runtime, computer_fixture)
        await runtime._freeze_main_manifest()
        await runtime.work_scheduler.start()
        artifact_state = {}
        if desktop and real_model_source is None:
            from tests.support.desktop_reference_server import publish_artifact

            if first_start:
                artifact_state = await publish_artifact(runtime, await issue_code(owner_id))
                await asyncio.gather(*tuple(runtime.app_gateway.tasks))
            else:
                artifact_state = {
                    key: value
                    for key, value in original_state.items()
                    if key.startswith("artifact_")
                }
        api = FastAPI(debug=desktop)
        attach_client_api(api, lambda: runtime.client_api)
        if desktop and real_model_source is None:
            from tests.support.desktop_fault_fixture import DesktopFaultFixture

            fault_fixture = DesktopFaultFixture(
                runtime, root, owner_id, lambda: issue_code(nonowner_id), desktop_model
            )
            fault_fixture.attach(api)
        if computer_fixture is not None:
            computer_fixture.attach(api)
        if computer_scenario is not None:
            computer_scenario.attach(api)
        if native_scenario is not None:
            native_scenario.attach(api)
        if (
            desktop
            and real_model_source is None
            and os.environ.get("YUKI_DESKTOP_CHARACTER_FIXTURE") == "1"
        ):
            from tests.support.desktop_character_fixture import attach_character_fixture

            await attach_character_fixture(api, runtime, owner_id)
        if desktop:
            from yuki.external_agents.http import attach_external_agent_api

            attach_external_agent_api(api, lambda: runtime.external_agents)

        if desktop and real_model_source is None:
            from types import SimpleNamespace

            qq_bot = SimpleNamespace(self_id="8000")

            @api.post("/__test/avatar-inputs")
            async def avatar_inputs() -> dict:
                from tests.support.desktop_reference_server import seed_avatar_inputs

                return await asyncio.to_thread(seed_avatar_inputs, root)

            @api.post("/__test/qq-entry")
            async def qq_entry() -> dict:
                from tests.support.desktop_reference_server import seed_qq_entry

                return await seed_qq_entry(runtime, owner_id, qq_bot)

            @api.get("/__test/events")
            async def original_events() -> list[dict]:
                from sqlalchemy import select

                from yuki.persistence.event_view import event_view
                from yuki.persistence.models import ChatEventModel
                from yuki.persistence.repository_helpers import _event_record

                async with database.read_session() as session:
                    rows = await session.scalars(select(ChatEventModel).order_by(ChatEventModel.id))
                    return [event_view(_event_record(row)) for row in rows]

            @api.get("/__test/asr-started")
            async def asr_started() -> dict:
                return {"started": asr_provider.started.is_set()}

            @api.post("/__test/resume-asr")
            async def resume_asr() -> dict:
                asr_provider.release.set()
                return {"released": True}

            @api.post("/__test/work")
            async def work_fixture() -> dict:
                from tests.support.desktop_reference_server import seed_work

                return await seed_work(runtime, artifact_state)

            @api.post("/__test/history")
            async def history_fixture(count: int = 61) -> dict:
                from tests.support.desktop_reference_server import seed_app_history

                return await seed_app_history(runtime, artifact_state, count)

        if desktop and real_model_source is None:

            @api.post("/__test/hold-work")
            async def hold_work_model() -> dict:
                work_model_gate.clear()
                return {"held": True}

            @api.post("/__test/release-work")
            async def release_work_model() -> dict:
                work_model_gate.set()
                return {"held": False}

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

        @api.post("/__test/shutdown")
        async def shutdown() -> dict:
            server.should_exit = True
            return {"ok": True}

        http = FastAPI()
        http.mount(base_path or "/", api)
        config = uvicorn.Config(
            http,
            host="127.0.0.1",
            port=port,
            log_level="critical",
            timeout_graceful_shutdown=1,
        )
        with config.bind_socket() as listener:
            actual_port = listener.getsockname()[1]
            server = uvicorn.Server(config)
            async with asyncio.TaskGroup() as tasks:
                serving = tasks.create_task(server.serve(sockets=[listener]))
                while not server.started:
                    if serving.done():
                        await serving
                        return
                    await asyncio.sleep(0.01)
                state = {
                    "base_url": f"http://127.0.0.1:{actual_port}{base_path}",
                    "owner_person_id": owner_id,
                    "nonowner_person_id": nonowner_id,
                    "owner_pairing_code": owner_code["code"],
                    "nonowner_pairing_code": nonowner_code["code"],
                    **artifact_state,
                }
                await asyncio.to_thread(state_file.write_text, json.dumps(state), encoding="utf-8")
                print("Isolated backend ready; connection details in state file.", flush=True)
                await serving
    finally:
        try:
            try:
                if fault_fixture is not None:
                    await fault_fixture.close()
                if computer_fixture is not None:
                    await computer_fixture.close()
            finally:
                await runtime.work_scheduler.close()
                assert not await runtime.lifecycle._close_entries(runtime.lifecycle._entries)
        finally:
            await database.close()


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
