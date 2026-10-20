"""Loopback-only desktop integration fixture using the actual container and ledger.

The model is scripted, so this exercises the runtime without paid provider calls.
All state lives in the explicit temporary directory; no deployed configuration is read.
"""

import asyncio
import json
import os
import shlex
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select
from tests.conftest import make_settings
from tests.integration.test_current_material_main_contract import _catalog, _profile
from tests.support.canonical_identity import ensure_person
from tests.support.canonical_ingress import fixture_ingress
from tests.support.commands_and_chat_helpers import inbound
from tests.support.model_profiles import write_fake_profiles
from tests.support.workspace_snapshots import snapshot_bytes

from yuki.asr.provider import HTTPASRProvider
from yuki.client_api.http import attach_client_api
from yuki.container import ApplicationContainer
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.identity.canonical_repository import create_presence
from yuki.identity.person_identity import merge_persons
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.pool import ModelClientPool
from yuki.persistence.database import Database
from yuki.persistence.models import ChatEventModel, ConversationMediaItemModel
from yuki.runtime.work_schema_v1 import inputs, work
from yuki.sandbox.db_models import SandboxTaskRunModel
from yuki.social.service import SocialContext


async def serve(root: Path, port: int) -> None:
    database = Database(f"sqlite+aiosqlite:///{root / 'test.sqlite3'}")
    await database.create_schema()
    async with database.immediate_session() as session:
        await create_presence(
            session, external_account_id="yuki-app", platform="app", now=datetime.now(UTC)
        )
        person = await ensure_person(session, "desktop-test-person", display_name="測試本人")
    prompt_options = {}
    if os.environ.get("YUKI_DESKTOP_PROMPT_FIXTURE") in {"1", "readonly"}:
        system, persona = root / "system_prompt.md", root / "persona.md"
        if not system.exists():
            system.write_bytes(b"# Desktop prompt\r\nORIGINAL_SYSTEM {{YUKI_PERSONA_CORE}}\r\n")
        if not persona.exists():
            persona.write_bytes(b"ORIGINAL_PERSONA\n")
        prompt_options = {
            "system_prompt_file": system
            if os.environ["YUKI_DESKTOP_PROMPT_FIXTURE"] == "1"
            else None,
            "bot_persona_file": persona,
        }
    deployment_fixture = os.environ.get("YUKI_DESKTOP_DEPLOYMENT_ROOT")
    operator_options = {}
    if deployment_fixture:
        deployment = await asyncio.to_thread(Path(deployment_fixture).resolve)
        parent = await asyncio.to_thread(root.parent.resolve)
        if not deployment.is_relative_to(parent):
            raise ValueError("deployment fixture must remain in the isolated test directory")
        operator_options["control_operators_file"] = deployment / "config/control-operators.toml"
    container = ApplicationContainer(
        make_settings(
            database.url,
            model_profiles_file=write_fake_profiles(root / "models.toml"),
            plugin_system_enabled=False,
            plugin_directory=root / "plugins",
            workspace_directory=root / "workspace",
            conversation_media_cache_directory=root / "media",
            social_transfer_directory=root / "transfer",
            **prompt_options,
            **operator_options,
            **(
                {"max_input_characters": 6000}
                if os.environ.get("YUKI_DESKTOP_LONG_TEXT_FIXTURE") == "1"
                else {}
            ),
            runtime_work_enabled=True,
            asr_enabled=True,
            automation_enabled=os.environ.get("YUKI_DESKTOP_CHARACTER_FIXTURE") == "1",
            self_memory_enabled=os.environ.get("YUKI_DESKTOP_CHARACTER_FIXTURE") == "1",
        ),
        database=database,
    )
    asr_started, resume_asr = asyncio.Event(), asyncio.Event()

    async def transcribe_audio(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["messages"][0]["content"][0]["input_audio"]["data"].startswith(
            "data:audio/mpeg;base64,"
        )
        asr_started.set()
        await resume_asr.wait()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "原語音轉寫：請把附件留下。"}}
                ]
            },
        )

    asr_http = httpx.AsyncClient(transport=httpx.MockTransport(transcribe_audio), trust_env=False)
    container.asr._provider = HTTPASRProvider(
        base_url="http://asr.test/v1", api_key="fixture-only", client=asr_http
    )

    work_script = []
    computer_work = None
    return_note = "畫面處理完成，請沿原工作接續。"

    def respond(request):
        if computer_work is not None:
            computer_work["phase"] += 1
            phase = computer_work["phase"]
            if phase == 1:
                name, arguments = (
                    "task_control",
                    {
                        "action": "accept",
                        "goal": computer_work["goal"],
                    },
                )
            elif phase == 2:
                cli = Path(__file__).parents[2] / "src/yuki/sandbox/yuki_computer.py"
                command = [
                    sys.executable,
                    "-I",
                    str(cli),
                    "help-request",
                    "--note",
                    "請協助原工作的畫面步驟",
                ]
                if getattr(computer, "external", False):
                    # Both the wait and file creation execute inside the real environment.
                    program = Path(__file__).with_name("desktop_browser_work.py").read_text()
                    shell = shlex.join(
                        ["python", "-u", "-c", program]
                        + (["wait-control"] if computer_work["wait_control"] else [])
                    )
                    cwd = "/workspace"
                else:
                    shell, cwd = shlex.join(command), str(root)
                name, arguments = "terminal_exec", {"command": shell, "cwd": cwd}
            elif phase == 3:
                receipt = json.loads(request.messages[-1].content)
                computer_work["run_id"] = receipt["data"]["run_id"]
                name, arguments = (
                    "task_control",
                    {
                        "action": "wait",
                        "conditions": [{"kind": "owned_run", "run_id": computer_work["run_id"]}],
                    },
                )
            elif phase == 4:
                name, arguments = "terminal_read", {"run_id": computer_work["run_id"]}
            elif phase == 5:
                assert request.messages[-1].role == "tool" and return_note in str(
                    request.messages[-1].content
                )
                if getattr(computer, "external", False):
                    name, arguments = (
                        "workspace_publish",
                        {"path": "original-computer.md", "name": "原電腦成果.md"},
                    )
                else:
                    name, arguments = (
                        "send_message",
                        {
                            "artifact_id": computer_work["artifact"]["artifact_id"],
                            "attachment_kind": "file",
                            "text": "原電腦工作成果",
                        },
                    )
            elif phase == 6 and getattr(computer, "external", False):
                computer_work["artifact"] = json.loads(request.messages[-1].content)["data"]
                name, arguments = (
                    "send_message",
                    {
                        "artifact_id": computer_work["artifact"]["artifact_id"],
                        "attachment_kind": "file",
                        "text": "原電腦工作成果",
                    },
                )
            elif phase == 6 or (phase == 7 and getattr(computer, "external", False)):
                name, arguments = (
                    "task_control",
                    {"action": "complete", "result": "已讀原交回回執並交付原版本成果"},
                )
            else:
                raise AssertionError(f"Unexpected original computer model phase: {phase}")
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(ToolCall(str(uuid4()), ToolFunction(name, json.dumps(arguments))),),
            )
        if work_script:
            name, arguments = work_script.pop(0)
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(ToolCall(str(uuid4()), ToolFunction(name, json.dumps(arguments))),),
            )
        if request.messages[-1].role == "tool":
            return "sent"
        return ChatResponse(
            content="",
            latency_seconds=0,
            tool_calls=(
                ToolCall(str(uuid4()), ToolFunction("send_message", '{"text":"真實帳本回覆"}')),
            ),
        )

    model = FakeLLMProvider(respond)
    container.models.apply_catalog(
        _catalog(_profile()), ModelClientPool(injected_profiles={"main": model})
    )
    await container.model_invocations.writer.start()
    await container.app_gateway.start()
    await container._freeze_main_manifest()
    await container.work_scheduler.start()
    computer = None
    if external_socket := os.environ.get("YUKI_DESKTOP_MANAGER_SOCKET"):
        from tests.support.desktop_external_computer import DesktopExternalComputer

        computer = DesktopExternalComputer(
            container, Path(external_socket), Path(os.environ["YUKI_DESKTOP_MANAGER_ROOT"])
        )
        await computer.start(person)
    elif os.environ.get("YUKI_DESKTOP_COMPUTER_FIXTURE") == "1":
        from tests.support.desktop_computer_fixture import DesktopComputerFixture

        computer = DesktopComputerFixture(
            root,
            container,
            native_work=os.environ.get("YUKI_DESKTOP_COMPUTER_WORK_FIXTURE") == "1",
        )
        await computer.start(person)
    identity = container.client_api.identity
    code = await identity.issue_for_operator(person, str(uuid4()), str(uuid4()))
    base_path = os.environ.get("YUKI_DESKTOP_BASE_PATH", "/role/yuki")
    base = f"http://127.0.0.1:{port}{base_path}"
    ready = root / "ready.json"
    ready.write_text(json.dumps({"url": base, "code": code["code"]}), encoding="utf-8")
    os.chmod(ready, 0o600)
    personal = FastAPI()
    attach_client_api(personal, lambda: container.client_api)
    if deployment_fixture:
        from yuki.admin_api.http import attach_admin_api

        attach_admin_api(personal, lambda: container.admin_api)
    if os.environ.get("YUKI_DESKTOP_EXECUTOR_FIXTURE") == "1":
        from tests.support.desktop_executor_fixture import attach_executor_fixture

        attach_executor_fixture(personal, container, work_script)
    if computer:
        computer.attach(personal)
    if os.environ.get("YUKI_DESKTOP_CHARACTER_FIXTURE") == "1":
        from tests.support.desktop_character_fixture import attach_character_fixture

        await attach_character_fixture(personal, container, person)
    if prompt_options:
        from tests.support.desktop_prompt_fixture import attach_prompt_fixture

        attach_prompt_fixture(personal, container, root, model)
    lose_response = ""
    hold_original_queries = False
    uploaded_requests = []
    pause_next_me = False
    me_blocked, resume_me = asyncio.Event(), asyncio.Event()

    @personal.middleware("http")
    async def failure_injection(request: Request, call_next):
        nonlocal lose_response, pause_next_me, hold_original_queries
        if (
            hold_original_queries
            and request.method == "GET"
            and "/client/v1/requests/" in request.url.path
        ):
            return JSONResponse({"problem": {"code": "unavailable"}}, status_code=503)
        if request.method == "POST" and request.url.path.endswith("/uploads"):
            uploaded_requests.append(request.query_params.get("request_id"))
        if pause_next_me and request.url.path.endswith("/client/v1/me"):
            pause_next_me = False
            me_blocked.set()
            await resume_me.wait()
        response = await call_next(request)
        if (
            lose_response
            and (
                request.method == "POST"
                or (
                    lose_response.startswith(("character/avatar", "character/prompts/"))
                    and request.method in {"PUT", "DELETE"}
                )
            )
            and request.url.path.endswith(f"/{lose_response}")
        ):
            if lose_response in {"corrections", "character/avatar"} or lose_response.startswith(
                "character/prompts/"
            ):
                hold_original_queries = True
            lose_response = ""
            return JSONResponse({"problem": {"code": "unknown"}}, status_code=503)
        return response

    @personal.post("/__test/lose-next-response")
    async def lose_next_response():
        nonlocal lose_response
        lose_response = "messages"
        return {"ok": True}

    @personal.post("/__test/release-original-queries")
    async def release_original_queries():
        nonlocal hold_original_queries
        hold_original_queries = False
        return {"ok": True}

    @personal.post("/__test/lose-next-avatar-response")
    async def lose_avatar_response():
        nonlocal lose_response
        lose_response = "character/avatar"
        return {"ok": True}

    @personal.post("/__test/lose-next-prompt-response/{key}")
    async def lose_prompt_response(key: str):
        nonlocal lose_response
        assert key in {"system_prompt", "persona", "reload"}
        lose_response = f"character/prompts/{key}"
        return {"ok": True}

    @personal.post("/__test/lose-next-memory-response")
    async def lose_memory_response():
        nonlocal lose_response
        lose_response = "corrections"
        return {"ok": True}

    @personal.post("/__test/lose-next-computer-response")
    async def lose_computer_response():
        nonlocal lose_response
        lose_response = "control"
        return {"ok": True}

    @personal.post("/__test/artifacts")
    async def send_artifacts(long_names: bool = False):
        async with database.sessions() as session:
            original = await session.scalar(
                select(ChatEventModel)
                .where(
                    ChatEventModel.author_person_id == person, ChatEventModel.direction == "inbound"
                )
                .order_by(ChatEventModel.id.desc())
                .limit(1)
            )
        assert original is not None
        name = "研究Data_" * 5 + "最終.md" if long_names else "same.md"
        files = [
            (name, b"# First artifact\n\nOriginal immutable content.\n"),
            (name, b"# Another artifact\n\nDifferent immutable content.\n"),
            ("manual.pdf", pdf_document()),
            ("unknown.custom", b"binary artifact content\x00\x01"),
        ]
        metadata = [
            snapshot_bytes(
                container.workspace_service.store,
                name,
                content,
                artifact_id=str(
                    uuid5(NAMESPACE_URL, f"{root}/{index}" + ("/long-name" if long_names else ""))
                ),
            )
            for index, (name, content) in enumerate(files)
        ]
        delivered = []
        for index in [0, 1, 2, 3, 0]:
            item = metadata[index]
            result = await container.social_service.execute(
                "send_message",
                {
                    "artifact_id": item["artifact_id"],
                    "attachment_kind": "file",
                    "text": "成果 " + item["name"],
                },
                SocialContext(
                    f"fixture:{uuid4()}",
                    "artifact",
                    original.canonical_conversation_id,
                    person_refs={"current_speaker": person},
                    trigger_event_id=original.id,
                    reply_presence_id=container.app_gateway.registry.resolve_by_handle(
                        container.app_gateway.handle
                    ).snapshot.presence_id,
                ),
            )
            assert result["status"] == "succeeded", result
            delivered.append({**item, "event_id": result.get("event_id")})
        return delivered

    @personal.post("/__test/work-scenario")
    async def work_scenario():
        work_script[:] = [
            ("task_control", {"action": "accept", "goal": "原工作整合測試"}),
            ("send_message", {"text": "需要你提供城市"}),
            ("task_control", {"action": "need_input", "reason": "city missing"}),
            ("send_message", {"text": "已依原工作完成"}),
            ("task_control", {"action": "complete", "result": "original work finished"}),
        ]
        return {"ok": True}

    @personal.post("/__test/computer-work-scenario")
    async def computer_work_scenario(goal: str = "原電腦協助工作", wait_control: bool = False):
        nonlocal computer_work
        assert computer is not None and computer.native_work
        assert not wait_control or getattr(computer, "external", False)
        artifact = (
            None
            if getattr(computer, "external", False)
            else await asyncio.to_thread(
                snapshot_bytes,
                container.workspace_service.store,
                "原電腦成果.md",
                b"# Original computer artifact\n\nDelivered after the original return receipt.\n",
            )
        )
        computer_work = {
            "phase": 0,
            "run_id": None,
            "artifact": artifact,
            "goal": goal,
            "wait_control": wait_control,
        }
        return artifact

    @personal.get("/__test/computer-work-facts")
    async def computer_work_facts():
        assert computer is not None and computer.native_work
        async with database.sessions() as session:
            mailbox = [
                dict(row)
                for row in (await session.execute(select(inputs).order_by(inputs.c.id))).mappings()
            ]
            work_ids = list((await session.execute(select(work.c.id))).scalars())
            runs = [
                {
                    "run_id": run.run_id,
                    "status": run.status,
                    "completion": json.loads(run.completion_json) if run.completion_json else None,
                }
                for run in (await session.scalars(select(SandboxTaskRunModel))).all()
            ]
        return {
            "phase": computer_work["phase"] if computer_work else None,
            "run_id": computer_work["run_id"] if computer_work else None,
            "work_ids": work_ids,
            "inputs": mailbox,
            "runs": runs,
            "artifact": computer_work["artifact"] if computer_work else None,
            **await computer.work_facts(),
        }

    @personal.post("/__test/lose-next-work-response/{action}")
    async def lose_work_response(action: str):
        nonlocal lose_response
        assert action in {"inputs", "cancel"}
        lose_response = action
        return {"ok": True}

    @personal.post("/__test/model-delay/{seconds}")
    async def model_delay(seconds: float):
        model._delay_seconds = seconds
        return {"ok": True}

    @personal.post("/__test/qq-entry")
    async def qq_entry(long_text: bool = False):
        async with database.immediate_session() as session:
            await create_presence(
                session, external_account_id="8000", platform="qq", now=datetime.now(UTC)
            )
        ingress = fixture_ingress(database)
        message = replace(
            inbound(
                "QQ 原入口的真實帳本內容"
                + (
                    "\n\n" + "原入口的中英混排長內容 Context remains separate. " * 150
                    if long_text
                    else ""
                ),
                message_id=str(uuid4()),
                user_id="1001",
                group_id=None,
            ),
            bot_user_id="8000",
        )
        admitted = await ingress.pre_admit(None, message)
        appended = await ingress.uow.append_inbound(admitted.message, admitted)
        async with database.immediate_session() as session:
            row = await session.get(ChatEventModel, appended.event.id)
            assert row is not None and row.author_person_id is not None
            await merge_persons(session, row.author_person_id, person)
        return {
            "event_id": appended.event.id,
            "conversation_id": appended.event.canonical_conversation_id,
        }

    @personal.post("/__test/lose-next-upload-response")
    async def lose_upload_response():
        nonlocal lose_response
        lose_response = "uploads"
        return {"ok": True}

    @personal.get("/__test/uploads")
    async def upload_requests():
        return {"requests": uploaded_requests}

    @personal.post("/__test/disconnect")
    async def disconnect_clients():
        for queue in tuple(database.commit_hints.subscribers):
            database.commit_hints.unsubscribe(queue)
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(False)
        return {"ok": True}

    @personal.get("/__test/asr-started")
    async def asr_state():
        return {"started": asr_started.is_set()}

    @personal.post("/__test/resume-asr")
    async def release_asr():
        resume_asr.set()
        return {"ok": True}

    @personal.post("/__test/other-person-code")
    async def other_person_code():
        async with database.immediate_session() as writer:
            other_person = await ensure_person(
                writer, "desktop-logout-other-person", display_name="另一位測試本人"
            )
        return await identity.issue_for_operator(other_person, str(uuid4()), str(uuid4()))

    @personal.post("/__test/link-code")
    async def another_code():
        return await identity.issue_for_operator(person, str(uuid4()), str(uuid4()))

    @personal.post("/__test/expire-attachments/{event_id}")
    async def expire_attachments(event_id: int):
        async with database.immediate_session() as session:
            items = await session.scalars(
                select(ConversationMediaItemModel).where(
                    ConversationMediaItemModel.source_event_id == event_id
                )
            )
            for item in items:
                item.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        return {"ok": True}

    @personal.post("/__test/pause-next-me")
    async def pause_me():
        nonlocal pause_next_me
        pause_next_me = True
        resume_me.clear()
        me_blocked.clear()
        return {"ok": True}

    @personal.get("/__test/me-blocked")
    async def me_is_blocked():
        return {"blocked": me_blocked.is_set()}

    @personal.post("/__test/resume-me")
    async def release_me():
        resume_me.set()
        return {"ok": True}

    @personal.delete("/__test/events/{event_id}")
    async def delete_event(event_id: int, lose_hint: bool = False):
        queues = tuple(database.commit_hints.subscribers) if lose_hint else ()
        for queue in queues:
            database.commit_hints.unsubscribe(queue)
        async with database.immediate_session() as session:
            await session.execute(delete(ChatEventModel).where(ChatEventModel.id == event_id))
        for queue in queues:
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(False)
        return {"ok": True}

    @personal.get("/__test/events")
    async def event_facts():
        async with database.sessions() as session:
            rows = list(await session.scalars(select(ChatEventModel).order_by(ChatEventModel.id)))
            return [{"id": row.id, "text": row.content, "direction": row.direction} for row in rows]

    @personal.get("/healthz")
    async def health():
        return {"ok": True}

    http = FastAPI()
    http.mount(base_path, personal)
    try:
        await uvicorn.Server(
            uvicorn.Config(
                http, host="127.0.0.1", port=port, log_level="critical", timeout_graceful_shutdown=1
            )
        ).serve()
    finally:
        resume_asr.set()
        if computer:
            await computer.close()
        assert not await container.lifecycle._close_entries(container.lifecycle._entries)
        await asr_http.aclose()


def pdf_document() -> bytes:
    stream = b"BT /F1 14 Tf 20 180 Td (Actual artifact PDF) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    result, offsets = b"%PDF-1.4\n", []
    for index, item in enumerate(objects, 1):
        offsets.append(len(result))
        result += str(index).encode() + b" 0 obj\n" + item + b"\nendobj\n"
    xref = len(result)
    result += b"xref\n0 6\n0000000000 65535 f \n"
    result += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    return (
        result
        + b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
        + str(xref).encode()
        + b"\n%%EOF\n"
    )


if __name__ == "__main__":
    asyncio.run(serve(Path(sys.argv[1]).resolve(), int(sys.argv[2])))
