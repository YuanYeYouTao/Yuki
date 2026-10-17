"""ACP sessions use the actual personal API, Main, SQLite and Social owners."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from tests.integration.test_current_material_main_contract import _deployment
from tests.unit.test_client_api import bearer
from tests.unit.test_client_sdk import DropAcknowledgement

from qq_ai_bot.client_api.http import attach_client_api
from qq_ai_bot.client_sdk import Outbox, PersonalClient
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.work_repository import WorkRepository


def sent_reply(request):
    if request.messages[-1].role == "tool":
        return "This model final must never become a second user message."
    return ChatResponse(
        content="",
        latency_seconds=0,
        tool_calls=(
            ToolCall(
                str(uuid4()),
                ToolFunction("send_message", json.dumps({"text": "Confirmed ACP reply"})),
            ),
        ),
    )


@pytest.fixture
async def acp_runtime(database, tmp_path):
    provider = FakeLLMProvider(sent_reply)
    app, qq, *_ = await _deployment(database, tmp_path, provider)
    await app.app_gateway.start()
    await app.acp_gateway.start()
    await app._freeze_main_manifest()
    issued = await app.client_api.identity.issue_for_operator(qq.person, str(uuid4()), str(uuid4()))
    device = await app.client_api.identity.pair(issued["code"], "ACP integration device")
    http_app = FastAPI()
    attach_client_api(http_app, lambda: app.client_api)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=http_app), base_url="http://test"
    ) as http:
        yield SimpleNamespace(
            app=app, http=http, http_app=http_app, device=device, provider=provider
        )
    assert not await app.lifecycle._close_entries(app.lifecycle._entries)


async def new_session(env):
    result = await env.http.post(
        "/client/v1/acp/sessions",
        headers=bearer(env.device["token"]),
        json={"request_id": str(uuid4())},
    )
    assert result.status_code == 200, result.text
    return result.json()["data"]


def selected(env, session):
    return {**bearer(env.device["token"]), "X-Yuki-Conversation": session["session_id"]}


async def settled(env, session, request_id):
    async with asyncio.timeout(15):
        while True:
            response = await env.http.get(
                f"/client/v1/requests/{request_id}", headers=selected(env, session)
            )
            assert response.status_code == 200, response.text
            facts = response.json()["data"]
            if facts.get("turn", {}).get("state") == "ended":
                return facts
            await asyncio.sleep(0.01)


async def test_two_sessions_share_original_person_and_fixed_tools_without_sharing_context(
    acp_runtime, database, tmp_path
):
    env = acp_runtime
    first, second = await new_session(env), await new_session(env)
    assert first["binding_id"] == second["binding_id"]
    assert first["session_id"] != second["session_id"]
    request_id = str(uuid4())
    for name, session in (("FIRST_SESSION_ONLY", first), ("SECOND_SESSION_ONLY", second)):
        client = PersonalClient(
            "http://test",
            env.device["token"],
            Outbox(tmp_path / f"{name}.sqlite"),
            http=env.http,
            entry_conversation_id=session["session_id"],
        )
        await client.prepare_message(name, request_id=request_id)
        await client.submit(request_id)
        facts = await settled(env, session, request_id)
        assert facts["turn"]["outcome"] == "chat"
        assert len(facts["result_event_ids"]) == 1
        original = await client.resolve(request_id)
        assert original["state"] == "result"
        page = await client.get(f"conversations/{session['session_id']}/events")
        assert [(row["direction"], row["text"]) for row in page["items"]] == [
            ("inbound", name),
            ("outbound", "Confirmed ACP reply"),
        ]
    requests = env.provider.requests
    assert len(requests) == 4
    assert requests[0].tools == requests[2].tools
    assert "read_personal_entry" not in {tool.name for tool in requests[0].tools}
    assert "FIRST_SESSION_ONLY" not in str(requests[2].messages)
    assert "SECOND_SESSION_ONLY" not in str(requests[0].messages)
    app_me = await env.http.get("/client/v1/me", headers=bearer(env.device["token"]))
    app_conversation = app_me.json()["data"]["conversation_id"]
    async with database.sessions() as reader:
        assert not list(
            await reader.scalars(
                select(ChatEventModel.id).where(
                    ChatEventModel.canonical_conversation_id == app_conversation
                )
            )
        )


async def test_sdk_reopens_original_session_outbox_after_lost_ack_without_new_model_request(
    acp_runtime, tmp_path
):
    env = acp_runtime
    session = await new_session(env)
    transport = DropAcknowledgement(env.http_app)
    path = tmp_path / "cold-session.sqlite"
    async with httpx.AsyncClient(transport=transport) as http:
        first = PersonalClient(
            "http://test",
            env.device["token"],
            Outbox(path),
            http=http,
            entry_conversation_id=session["session_id"],
        )
        request_id = await first.prepare_message("ORIGINAL_PAID_INPUT")
        with pytest.raises(httpx.ReadTimeout):
            await first.submit(request_id)
        assert Outbox(path).get(request_id)["state"] == "submitted"
        await settled(env, session, request_id)
        restored = PersonalClient(
            "http://test",
            env.device["token"],
            Outbox(path),
            http=http,
            entry_conversation_id=session["session_id"],
        )
        resolved = await restored.submit(request_id)
        assert resolved["state"] == "result"
        assert transport.posts == 1
        assert len(env.provider.requests) == 2
        loaded = await restored.get(f"acp/sessions/{session['session_id']}")
        assert loaded["conversation_id"] == session["session_id"]


async def test_finished_model_without_delivery_has_real_end_receipt(acp_runtime):
    env = acp_runtime
    env.provider._responder = lambda _: "Private model final, not a sent reply."
    session = await new_session(env)
    request_id = str(uuid4())
    response = await env.http.post(
        "/client/v1/messages",
        headers=selected(env, session),
        json={"request_id": request_id, "text": "Finish without sending"},
    )
    assert response.status_code == 200, response.text
    facts = await settled(env, session, request_id)
    assert facts["turn"]["state"] == "ended" and facts["turn"]["outcome"] == "chat"
    assert facts["result_event_ids"] == []
    assert len(env.provider.requests) == 1
    replay = await env.http.post(
        "/client/v1/messages",
        headers=selected(env, session),
        json={"request_id": request_id, "text": "Finish without sending"},
    )
    assert replay.status_code == 200 and replay.json()["data"]["replay"]
    assert len(env.provider.requests) == 1


async def test_cancel_only_the_original_prompt_while_another_session_keeps_running(
    acp_runtime, monkeypatch
):
    env = acp_runtime
    first, second = await new_session(env), await new_session(env)
    entered = {name: asyncio.Event() for name in ("KEEP_ALIVE_ORIGINAL", "CANCEL_ONLY_ORIGINAL")}
    release = asyncio.Event()

    async def waiting_model(request):
        name = next(name for name in entered if name in str(request.messages))
        env.provider.requests.append(request)
        entered[name].set()
        await release.wait()
        return ChatResponse(content="Private model final", latency_seconds=0)

    monkeypatch.setattr(env.provider, "complete", waiting_model)
    identities = [str(uuid4()), str(uuid4())]
    for session, name, request_id in zip((first, second), entered, identities, strict=True):
        result = await env.http.post(
            "/client/v1/messages",
            headers=selected(env, session),
            json={"request_id": request_id, "text": name},
        )
        assert result.status_code == 200, result.text
    async with asyncio.timeout(15):
        await asyncio.gather(*(event.wait() for event in entered.values()))
    result = await env.http.post(
        f"/client/v1/acp/sessions/{second['session_id']}/requests/{identities[1]}/cancel",
        headers=bearer(env.device["token"]),
    )
    assert result.status_code == 200, result.text
    facts = result.json()["data"]
    assert facts["cancelled"] and facts["turn"]["outcome"] == "cancelled", facts
    still_running = await env.http.get(
        f"/client/v1/requests/{identities[0]}", headers=selected(env, first)
    )
    assert still_running.json()["data"]["turn"]["state"] == "running"
    release.set()
    remaining = await settled(env, first, identities[0])
    assert remaining["turn"]["outcome"] == "chat"
    for session in (first, second):
        loaded = await env.http.get(
            f"/client/v1/acp/sessions/{session['session_id']}",
            headers=bearer(env.device["token"]),
        )
        assert loaded.json()["data"]["generation"] == session["generation"]


async def test_original_work_waits_and_resumes_with_confirmed_delivery_in_same_session(
    acp_runtime, database, tmp_path
):
    env = acp_runtime

    def calls(steps):
        scripted = iter(steps)

        def respond(_):
            name, arguments = next(scripted)
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(ToolCall(str(uuid4()), ToolFunction(name, json.dumps(arguments))),),
            )

        return respond

    env.provider._responder = calls(
        [
            ("task_control", {"action": "accept", "goal": "Keep this original ACP Work"}),
            ("send_message", {"text": "Which city should I use?"}),
            ("task_control", {"action": "need_input", "reason": "The city is still missing"}),
        ]
    )
    session = await new_session(env)
    client = PersonalClient(
        "http://test",
        env.device["token"],
        Outbox(tmp_path / "work-session.sqlite"),
        http=env.http,
        entry_conversation_id=session["session_id"],
    )
    request_id = await client.prepare_message("Start the original ACP Work")
    await client.submit(request_id)
    before = await settled(env, session, request_id)
    original_work = before["work"]
    assert original_work["state"] == "waiting_user"
    assert len(before["result_event_ids"]) == 1
    repository = WorkRepository(database)
    original = await repository.get(original_work["work_id"])
    input_id = await client.prepare_input(original["id"], "Use Taipei for the original Work")
    accepted = await client.submit(input_id)
    assert accepted["result"]["input_id"] > 0
    queued = await repository.get(original["id"])
    assert queued["state"] == "queued"
    env.provider._responder = calls(
        [
            ("send_message", {"text": "Taipei result from the original ACP Work"}),
            ("task_control", {"action": "complete", "result": "Original work is complete"}),
        ]
    )
    await env.app.work_resumer.resume(queued)
    completed = await repository.get(original["id"])
    assert completed["state"] == "completed"
    assert completed["source_key"] == original["source_key"]
    assert completed["model_requests"] > original["model_requests"]
    after = await client.get(f"requests/{request_id}")
    assert after["work"]["work_id"] == original["id"]
    assert after["work"]["state"] == "completed"
    assert len(after["result_event_ids"]) == 2
    events = await client.get(f"conversations/{session['session_id']}/events")
    replies = [row["text"] for row in events["items"] if row["direction"] == "outbound"]
    assert replies == ["Which city should I use?", "Taipei result from the original ACP Work"]
