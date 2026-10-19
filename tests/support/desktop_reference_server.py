"""Loopback-only desktop integration fixture using the actual container and ledger.

The model is scripted, so this exercises the runtime without paid provider calls.
All state lives in the explicit temporary directory; no deployed configuration is read.
"""

import asyncio
import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select
from tests.conftest import make_settings
from tests.integration.test_current_material_main_contract import _catalog, _profile
from tests.support.canonical_ingress import fixture_ingress
from tests.support.commands_and_chat_helpers import inbound
from tests.support.model_profiles import write_fake_profiles

from yuki.asr.provider import QwenASRProvider
from yuki.client_api.http import attach_client_api
from yuki.container import ApplicationContainer
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.identity.canonical_repository import create_presence, ensure_person
from yuki.identity.person_identity import merge_persons
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.pool import ModelClientPool
from yuki.persistence.database import Database
from yuki.persistence.models import ChatEventModel, ConversationMediaItemModel


async def serve(root: Path, port: int) -> None:
    database = Database(f"sqlite+aiosqlite:///{root / 'test.sqlite3'}")
    await database.create_schema()
    async with database.immediate_session() as session:
        await create_presence(
            session, external_account_id="yuki-app", platform="app", now=datetime.now(UTC)
        )
        person = await ensure_person(session, "desktop-test-person", display_name="測試本人")
    container = ApplicationContainer(
        make_settings(
            database.url,
            model_profiles_file=write_fake_profiles(root / "models.toml"),
            plugin_system_enabled=False,
            plugin_directory=root / "plugins",
            workspace_directory=root / "workspace",
            conversation_media_cache_directory=root / "media",
            social_transfer_directory=root / "transfer",
            runtime_work_enabled=True,
            asr_enabled=True,
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
    container.asr._provider = QwenASRProvider(
        base_url="http://asr.test/v1", api_key="fixture-only", client=asr_http
    )

    def respond(request):
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
    identity = container.client_api.identity
    code = await identity.issue_for_operator(person, str(uuid4()), str(uuid4()))
    base = f"http://127.0.0.1:{port}/role/yuki"
    ready = root / "ready.json"
    ready.write_text(json.dumps({"url": base, "code": code["code"]}), encoding="utf-8")
    os.chmod(ready, 0o600)
    personal = FastAPI()
    attach_client_api(personal, lambda: container.client_api)
    lose_response = ""
    uploaded_requests = []
    pause_next_me = False
    me_blocked, resume_me = asyncio.Event(), asyncio.Event()

    @personal.middleware("http")
    async def failure_injection(request: Request, call_next):
        nonlocal lose_response, pause_next_me
        if request.method == "POST" and request.url.path.endswith("/uploads"):
            uploaded_requests.append(request.query_params.get("request_id"))
        if pause_next_me and request.url.path.endswith("/client/v1/me"):
            pause_next_me = False
            me_blocked.set()
            await resume_me.wait()
        response = await call_next(request)
        if (
            lose_response
            and request.method == "POST"
            and request.url.path.endswith(f"/{lose_response}")
        ):
            lose_response = ""
            return JSONResponse({"problem": {"code": "unknown"}}, status_code=503)
        return response

    @personal.post("/__test/lose-next-response")
    async def lose_next_response():
        nonlocal lose_response
        lose_response = "messages"
        return {"ok": True}

    @personal.post("/__test/model-delay/{seconds}")
    async def model_delay(seconds: float):
        model._delay_seconds = seconds
        return {"ok": True}

    @personal.post("/__test/qq-entry")
    async def qq_entry():
        async with database.immediate_session() as session:
            await create_presence(
                session, external_account_id="8000", platform="qq", now=datetime.now(UTC)
            )
        ingress = fixture_ingress(database)
        message = replace(
            inbound(
                "QQ 原入口的真實帳本內容", message_id=str(uuid4()), user_id="1001", group_id=None
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
    http.mount("/role/yuki", personal)
    try:
        await uvicorn.Server(
            uvicorn.Config(
                http, host="127.0.0.1", port=port, log_level="critical", timeout_graceful_shutdown=1
            )
        ).serve()
    finally:
        resume_asr.set()
        assert not await container.lifecycle._close_entries(container.lifecycle._entries)
        await asr_http.aclose()


if __name__ == "__main__":
    asyncio.run(serve(Path(sys.argv[1]).resolve(), int(sys.argv[2])))
