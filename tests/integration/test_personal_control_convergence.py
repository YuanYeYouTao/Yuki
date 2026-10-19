"""Personal request facts converge through actual HTTP, SQLite, Runtime and stdio."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from acp import RequestError
from fastapi import FastAPI
from sqlalchemy import event, select
from tests.integration.test_acp_runtime_integration import acp_runtime as acp_runtime
from tests.integration.test_acp_runtime_integration import new_session, settled
from tests.integration.test_acp_stdio import process
from tests.integration.test_client_continuation import live
from tests.integration.test_current_material_main_contract import _catalog, _profile
from tests.unit.test_acp_adapter import Peer, text
from tests.unit.test_client_api import bearer

from yuki.client_api.http import attach_client_api
from yuki.client_api.prompts import ClientPrompts
from yuki.client_api.requests import ClientRequests
from yuki.client_sdk import ClientProblem, Outbox, PersonalClient
from yuki.container import ApplicationContainer
from yuki.conversation.ordinary_admission_db_models import OrdinaryTurnAdmissionModel
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.gateway.providers.app.ingress import AppGateway
from yuki.identity.canonical_repository import find_identity_binding
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel, PresenceModel
from yuki.identity.routing import RouteSendError
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.pool import ModelClientPool
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import effects, journal
from yuki.services.main_agent_contract import MainAgentContract
from yuki.services.personal_entry_tool import read_entry
from yuki.social.service import SocialContext


def scripted(steps):
    remaining = iter(steps)

    def respond(_):
        name, arguments = next(remaining)
        return ChatResponse(
            content="",
            latency_seconds=0,
            tool_calls=(ToolCall(str(uuid4()), ToolFunction(name, json.dumps(arguments))),),
        )

    return respond


async def test_personal_gets_and_stream_authentication_do_not_write(acp_runtime, database):
    env = acp_runtime
    headers = bearer(env.device["token"])
    created = await env.http.post(
        "/client/v1/acp/sessions", headers=headers, json={"request_id": str(uuid4())}
    )
    session_id = created.json()["data"]["session_id"]
    dml = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().split(None, 1)[0].upper() in {
            "INSERT",
            "UPDATE",
            "DELETE",
            "REPLACE",
        }:
            dml.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", observe)
    try:
        me = await env.http.get("/client/v1/me", headers=headers)
        assert me.status_code == 200
        assert all("last_used_at" not in device for device in me.json()["data"]["devices"])
        for path, selected in (
            ("/client/v1/home", headers),
            (
                f"/client/v1/conversations/{session_id}/events",
                {**headers, "X-Yuki-Conversation": session_id},
            ),
            (
                f"/client/v1/requests/{uuid4()}",
                {**headers, "X-Yuki-Conversation": session_id},
            ),
        ):
            assert (await env.http.get(path, headers=selected)).status_code == 200

        disconnected = asyncio.Event()
        bodies = []

        async def receive():
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                bodies.append(message["body"])
                disconnected.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/client/v1/stream",
            "raw_path": b"/client/v1/stream",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"authorization", headers["Authorization"].encode())],
            "client": ("127.0.0.1", 1),
            "server": ("test", 80),
        }
        await asyncio.wait_for(env.http_app(scope, receive, send), 5)
        assert b"event: sync" in bodies[0]
        assert not dml
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", observe)

    principal = await env.app.client_api.identity.authenticate(env.device["token"])
    async with database.immediate_session() as writer:
        person = await writer.get(CanonicalPersonModel, principal.source_person_id)
        person.enabled = False
    assert (await env.http.get("/client/v1/home", headers=headers)).status_code == 401
    async with database.immediate_session() as writer:
        person = await writer.get(CanonicalPersonModel, principal.source_person_id)
        person.enabled = True
    assert (await env.http.get("/client/v1/me", headers=headers)).status_code == 200
    revoked = await env.http.delete(
        f"/client/v1/devices/{env.device['device_id']}", headers=headers
    )
    assert revoked.status_code == 200
    assert (await env.http.get("/client/v1/me", headers=headers)).status_code == 401


@pytest.mark.parametrize("platform", ["app", "acp"])
async def test_disabled_local_presence_can_restart_without_blocking_other_entries(
    acp_runtime, database, platform
):
    app = acp_runtime.app
    gateway = app.app_gateway if platform == "app" else app.acp_gateway
    await gateway.close()
    async with database.immediate_session() as writer:
        presence = await writer.scalar(
            select(PresenceModel).where(PresenceModel.platform == platform)
        )
        presence_id = presence.id
        presence.enabled = False
    await gateway.start()
    assert presence_id in app.gateway_registry.connected_presence_ids(platform)
    connection = app.gateway_registry.resolve_by_handle(gateway.handle).snapshot
    with pytest.raises(RouteSendError, match="original_presence_unavailable"):
        await app.presence_router.resolve_presence(presence_id)
    other = "acp" if platform == "app" else "app"
    other_ids = app.gateway_registry.connected_presence_ids(other)
    assert other_ids
    assert (await app.presence_router.resolve_presence(other_ids[0])).platform == other
    async with database.immediate_session() as writer:
        presence = await writer.get(PresenceModel, presence_id)
        assert presence is not None and not presence.enabled
        presence.enabled = True
    await gateway.start()
    assert presence_id in app.gateway_registry.connected_presence_ids(platform)
    resolved = await app.presence_router.resolve_presence(presence_id)
    assert resolved.connection.snapshot.connection_id == connection.connection_id
    assert app.gateway_registry.connected_presence_ids(other) == other_ids


@pytest.mark.parametrize("failure,send_first", [(False, False), (True, False), (True, True)])
async def test_sdk_settles_ended_requests_and_keeps_delivery_before_failure(
    acp_runtime, tmp_path, failure, send_first
):
    env = acp_runtime
    calls = 0

    def respond(_):
        nonlocal calls
        calls += 1
        if send_first and calls == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall("sent", ToolFunction("send_message", '{"text":"Partial result"}')),
                ),
            )
        return "" if failure else "Private final without a Social message."

    env.provider._responder = respond
    client = PersonalClient(
        "http://test", env.device["token"], Outbox(tmp_path / "request.sqlite"), http=env.http
    )
    identity = await client.prepare_message("Finish this original request")
    await client.submit(identity)
    await asyncio.gather(*tuple(env.app.app_gateway.tasks), return_exceptions=True)
    facts = await client.query(identity)
    assert facts["turn"]["state"] == "ended"
    assert bool(facts["result_event_ids"]) == send_first
    if failure:
        assert facts["problem_code"] == "empty_llm_response"
        assert facts["problem"]["outcome"] == "empty_llm_response"
        with pytest.raises(ClientProblem, match="empty_llm_response"):
            await client.resolve(identity)
    else:
        assert facts["problem_code"] is None
        assert (await client.resolve(identity))["state"] == "result"
    saved = client.outbox.get(identity)
    assert saved["state"] == "result" and not client.outbox.pending()
    assert saved["result"]["result_event_ids"] == facts["result_event_ids"]
    model_calls = len(env.provider.requests)
    restored = PersonalClient(
        "http://test", env.device["token"], Outbox(client.outbox.path), http=env.http
    )
    if failure:
        with pytest.raises(ClientProblem, match="empty_llm_response"):
            await restored.submit(identity)
    else:
        assert (await restored.submit(identity))["state"] == "result"
    assert len(env.provider.requests) == model_calls


async def test_sdk_keeps_running_and_unknown_with_confirmed_output_pending(
    acp_runtime, tmp_path, monkeypatch
):
    env = acp_runtime
    complete = env.provider._complete
    awaiting_final, release = asyncio.Event(), asyncio.Event()

    async def blocked(request):
        if request.messages[-1].role == "tool":
            awaiting_final.set()
            await release.wait()
        return await complete(request)

    monkeypatch.setattr(env.provider, "_complete", blocked)
    client = PersonalClient(
        "http://test", env.device["token"], Outbox(tmp_path / "pending.sqlite"), http=env.http
    )
    identity = await client.prepare_message("Send before the turn ends")
    await client.submit(identity)
    try:
        await asyncio.wait_for(awaiting_final.wait(), 5)
        running = await client.resolve(identity)
        assert running["state"] == "accepted"
        assert running["result"]["turn"]["state"] == "running"
        delivered = running["result"]["result_event_ids"]
        assert delivered
        principal = await env.app.client_api.identity.authenticate(env.device["token"])
        # A new observer has original database facts but none of the old process's tasks.
        observer = AppGateway(
            database=env.app.database,
            registry=env.app.gateway_registry,
            processor=env.app.processor,
            social=env.app.social_service,
        )
        prompts = ClientPrompts(
            env.app.client_api.identity,
            {"app": observer},
            env.app.runtime.bindings,
            concurrency=env.app.concurrency,
        )
        original = await ClientRequests(env.app.client_api.identity, prompts=prompts).get(
            principal, identity
        )
        assert original["turn"]["state"] == "unknown"
        assert original["result_event_ids"] == delivered
        get = client.get

        async def observed(path, **params):
            return original if path == f"requests/{identity}" else await get(path, **params)

        monkeypatch.setattr(client, "get", observed)
        unknown = await client.resolve(identity)
        assert unknown["state"] == "accepted"
        assert unknown["result"]["turn"]["state"] == "unknown"
        assert unknown["result"]["result_event_ids"] == delivered
        assert len(client.outbox.pending()) == 1
    finally:
        release.set()
        await asyncio.gather(*tuple(env.app.app_gateway.tasks), return_exceptions=True)


async def test_request_projection_keeps_running_when_receipt_commits_during_read(
    acp_runtime, tmp_path, monkeypatch
):
    import yuki.client_api.requests as request_queries

    env = acp_runtime
    complete = env.provider._complete
    awaiting_final, release = asyncio.Event(), asyncio.Event()

    async def blocked(request):
        if request.messages[-1].role == "tool":
            awaiting_final.set()
            await release.wait()
        return await complete(request)

    monkeypatch.setattr(env.provider, "_complete", blocked)
    client = PersonalClient(
        "http://test", env.device["token"], Outbox(tmp_path / "projection.sqlite"), http=env.http
    )
    identity = await client.prepare_message("Keep the original result while its turn settles")
    await client.submit(identity)
    try:
        await awaiting_final.wait()
        principal = await env.app.client_api.identity.authenticate(env.device["token"])
        original_task = env.app.app_gateway.request_task(principal.conversation_id, identity)
        assert original_task is not None and not original_task.done()
        reader_task = asyncio.current_task()
        personal_command = request_queries.personal_command
        interleaved = False

        async def finish_after_receipt_read(session, person_id, request_id, **kwargs):
            nonlocal interleaved
            receipt = await personal_command(session, person_id, request_id, **kwargs)
            if asyncio.current_task() is reader_task and request_id == identity and not interleaved:
                interleaved = True
                assert receipt is None
                # Commit the real receipt and retire its live task after this
                # query has already observed the original absent receipt.
                release.set()
                await original_task
                assert env.app.app_gateway.request_task(principal.conversation_id, identity) is None
            return receipt

        monkeypatch.setattr(request_queries, "personal_command", finish_after_receipt_read)
        observed = await ClientRequests(
            env.app.client_api.identity, prompts=env.app.client_api.prompts
        ).get(principal, identity)
        assert interleaved
        assert observed["found"] and observed["turn"]["state"] == "running"
        assert observed["result_event_ids"]
        ended = await client.query(identity)
        assert ended["turn"] == {"state": "ended", "outcome": "chat"}
        assert ended["result_event_ids"] == observed["result_event_ids"]
    finally:
        release.set()
        await asyncio.gather(*tuple(env.app.app_gateway.tasks), return_exceptions=True)


@pytest.mark.parametrize("cancel", [False, True])
async def test_standard_stdio_answer_resumes_original_work_and_controls_its_outputs(
    acp_runtime, database, tmp_path, cancel
):
    env = acp_runtime
    env.provider._responder = scripted(
        [
            ("task_control", {"action": "accept", "goal": "Return the city result"}),
            ("send_message", {"text": "Which city?"}),
            ("task_control", {"action": "need_input", "reason": "Need the city"}),
        ]
    )
    peer = Peer()
    repository = WorkRepository(database)
    async with live(env.http_app) as url:
        async with process(peer, url, tmp_path / "stdio", token=env.device["token"]) as (
            connection,
            _,
        ):
            await connection.initialize(protocol_version=1)
            session = await connection.new_session(cwd=str(tmp_path), mcp_servers=[])
            selected = {"session_id": session.session_id}
            first_id = str(uuid4())
            first = await connection.prompt(
                session_id=session.session_id,
                prompt=[text("Start the city task")],
                **{"yuki.requestId": first_id},
            )
            assert first.stop_reason == "end_turn"
            initial = await settled(env, selected, first_id)
            work_id = initial["work"]["work_id"]
            assert initial["work"]["state"] == "waiting_user"
            original = await repository.get(work_id)
            second_id = str(uuid4())
            pending = asyncio.create_task(
                connection.prompt(
                    session_id=session.session_id,
                    prompt=[text("Use Taipei")],
                    **{"yuki.requestId": second_id},
                )
            )
            continuation = await settled(env, selected, second_id)
            assert continuation["work"]["work_id"] == work_id
            queued = await repository.get(work_id)
            assert queued["state"] == "queued"
            async with database.sessions() as reader:
                admission = await reader.get(OrdinaryTurnAdmissionModel, continuation["event_id"])
                assert admission.work_id == work_id and admission.route == "work"
            if cancel:
                await connection.cancel(session_id=session.session_id)
                assert (await asyncio.wait_for(pending, 5)).stop_reason == "cancelled"
                assert (await repository.get(work_id))["state"] == "cancelled"
            else:
                env.provider._responder = scripted(
                    [
                        ("send_message", {"text": "Taipei result"}),
                        ("task_control", {"action": "complete", "result": "City task completed"}),
                    ]
                )
                await env.app.work_resumer.resume(queued)
                assert (await asyncio.wait_for(pending, 5)).stop_reason == "end_turn"
                completed = await repository.get(work_id)
                assert completed["state"] == "completed"
                assert completed["source_key"] == original["source_key"]
                assert completed["model_requests"] > original["model_requests"]
                assert [update.content.text for _, update in peer.updates] == [
                    "Which city?",
                    "Taipei result",
                ]


async def test_standard_stdio_answer_keeps_original_work_across_cold_runtime(
    acp_runtime, database, tmp_path
):
    env = acp_runtime
    env.provider._responder = scripted(
        [
            ("task_control", {"action": "accept", "goal": "Continue the original city task"}),
            ("send_message", {"text": "Which city before restart?"}),
            ("task_control", {"action": "need_input", "reason": "Need the city"}),
        ]
    )
    state = tmp_path / "cold-stdio"
    repository = WorkRepository(database)
    first_peer = Peer()
    async with live(env.http_app) as url:
        async with process(first_peer, url, state, token=env.device["token"]) as (connection, _):
            await connection.initialize(protocol_version=1)
            session = await connection.new_session(cwd=str(tmp_path), mcp_servers=[])
            identity = str(uuid4())
            first = await connection.prompt(
                session_id=session.session_id,
                prompt=[text("Start the city task")],
                **{"yuki.requestId": identity},
            )
            assert first.stop_reason == "end_turn"
            accepted = await settled(env, {"session_id": session.session_id}, identity)
            original = await repository.get(accepted["work"]["work_id"])
            assert original["state"] == "waiting_user"
    assert [update.content.text for _, update in first_peer.updates] == [
        "Which city before restart?"
    ]
    async with database.sessions() as reader:
        original_journal = await reader.scalar(
            select(journal.c.payload_json).where(journal.c.work_id == original["id"])
        )
        original_effects = dict(
            (
                await reader.execute(
                    select(effects.c.effect_key, effects.c.receipt_json).where(
                        effects.c.work_id == original["id"]
                    )
                )
            )
            .tuples()
            .all()
        )
    assert original_journal and original_effects
    assert not await env.app.lifecycle._close_entries(env.app.lifecycle._entries)

    restored = ApplicationContainer(env.app.settings, database=database)
    provider = FakeLLMProvider(
        scripted(
            [
                ("send_message", {"text": "Taipei result after restart"}),
                ("task_control", {"action": "complete", "result": "Original city task completed"}),
            ]
        )
    )
    restored.models.apply_catalog(
        _catalog(_profile()), ModelClientPool(injected_profiles={"main": provider})
    )
    await restored.model_invocations.writer.start()
    await restored.app_gateway.start()
    await restored.acp_gateway.start()
    app = FastAPI()
    attach_client_api(app, lambda: restored.client_api)
    second_peer = Peer()
    try:
        async with live(app, port=int(url.rsplit(":", 1)[1])) as restored_url:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as http:
                observed = SimpleNamespace(http=http, device=env.device)
                async with process(second_peer, restored_url, state, token=env.device["token"]) as (
                    connection,
                    _,
                ):
                    await connection.initialize(protocol_version=1)
                    second_id = str(uuid4())
                    pending = asyncio.create_task(
                        connection.prompt(
                            session_id=session.session_id,
                            prompt=[text("Use Taipei after restart")],
                            **{"yuki.requestId": second_id},
                        )
                    )
                    continuation = await settled(
                        observed, {"session_id": session.session_id}, second_id
                    )
                    assert continuation["work"]["work_id"] == original["id"]
                    queued = await repository.get(original["id"])
                    assert queued["state"] == "queued"
                    for key in (
                        "source_key",
                        "source_json",
                        "conversation_id",
                        "generation",
                        "model_requests",
                        "tool_calls",
                    ):
                        assert queued[key] == original[key]
                    async with database.sessions() as reader:
                        assert (
                            await reader.scalar(
                                select(journal.c.payload_json).where(
                                    journal.c.work_id == original["id"]
                                )
                            )
                            == original_journal
                        )
                    await restored.work_resumer.resume(queued)
                    assert (await asyncio.wait_for(pending, 5)).stop_reason == "end_turn"
                    completed = await repository.get(original["id"])
                    assert completed["id"] == original["id"] and completed["state"] == "completed"
                    assert completed["model_requests"] == original["model_requests"] + 2
                    assert len(provider.requests) == 2 and len(env.provider.requests) == 3
                    assert [update.content.text for _, update in second_peer.updates] == [
                        "Taipei result after restart"
                    ]
                    async with database.sessions() as reader:
                        kept = dict(
                            (
                                await reader.execute(
                                    select(effects.c.effect_key, effects.c.receipt_json).where(
                                        effects.c.effect_key.in_(original_effects)
                                    )
                                )
                            )
                            .tuples()
                            .all()
                        )
                    assert kept == original_effects
                    repeated = await connection.prompt(
                        session_id=session.session_id,
                        prompt=[text("Use Taipei after restart")],
                        **{"yuki.requestId": second_id},
                    )
                    assert repeated.stop_reason == "end_turn" and len(provider.requests) == 2
    finally:
        assert not await restored.lifecycle._close_entries(restored.lifecycle._entries)


async def test_disabled_platform_binding_can_transfer_without_reviving_old_ownership(
    acp_runtime, database
):
    identity = acp_runtime.app.client_api.identity
    async with database.immediate_session() as writer:
        binding = await find_identity_binding(writer, "1001")
        target = await find_identity_binding(writer, "1002")
        original_id, original_person = binding.id, binding.person_id
        binding.status = "disabled"
    result = await identity.transfer_for_operator(
        original_id, target.person_id, str(uuid4()), str(uuid4())
    )
    async with database.sessions() as reader:
        old = await reader.get(IdentityBindingModel, original_id)
        current = await reader.get(IdentityBindingModel, result["binding_id"])
        assert old.status == "ended" and old.person_id == original_person
        assert current.id != old.id and current.person_id == target.person_id
        assert current.history_starts_at == current.created_at == old.ended_at == old.updated_at


async def test_acp_uses_fixed_personal_tools_and_explicit_person_routing(acp_runtime, database):
    env = acp_runtime
    session = await new_session(env)
    identity = env.app.client_api.identity
    base = await identity.authenticate(env.device["token"])
    principal = await identity.authenticate(
        env.device["token"], conversation_id=session["session_id"]
    )
    contract = env.app.main_agent_contract
    assert await contract.entry_for(principal.conversation_id) == "app"
    assert await contract.model_definitions("app") != await contract.model_definitions("platform")
    for code in (False, True):
        view = MainAgentContract(env.app.chat, contract.state, code_enabled=code)
        assert "read_personal_entry" in {tool.name for tool in await view.definitions("app")}
        assert "read_personal_entry" not in {
            tool.name for tool in await view.definitions("platform")
        }
        if code:
            assert "read_personal_entry" in view.app_script_api.schemas
            assert "read_personal_entry" not in view.script_api.schemas
    runtime = SimpleNamespace(
        effective_conversation_id=principal.conversation_id,
        require_actor=lambda: SimpleNamespace(person_id=principal.person_id),
    )
    original = await read_entry(database, runtime, {"conversation_id": base.conversation_id})
    assert original["conversation_id"] == base.conversation_id and original["platform"] == "app"
    result = await env.app.social_service.execute(
        "send_message",
        {"target": {"kind": "person", "target_id": principal.person_id}, "text": "choose entry"},
        SocialContext(
            str(uuid4()),
            str(uuid4()),
            principal.conversation_id,
            person_refs={"current_speaker": principal.person_id},
            reply_presence_id=principal.presence_id,
        ),
    )
    assert result["error"] == "binding_required"
    assert {item["platform"] for item in result["bindings"]} >= {"app", "acp"}
    another = await new_session(env)
    for entry, message in ((session, "First ACP entry"), (another, "Second ACP entry")):
        await env.http.post(
            "/client/v1/messages",
            headers={**bearer(env.device["token"]), "X-Yuki-Conversation": entry["session_id"]},
            json={"request_id": str(uuid4()), "text": message},
        )
        await asyncio.gather(*tuple(env.app.acp_gateway.tasks), return_exceptions=True)
    runtime.effective_conversation_id = base.conversation_id
    candidates = await read_entry(database, runtime, {"binding_id": principal.binding_id})
    assert candidates["error"] == "conversation_required"
    assert {item["conversation_id"] for item in candidates["conversations"]} == {
        session["session_id"],
        another["session_id"],
    }
    selected = await read_entry(database, runtime, {"conversation_id": session["session_id"]})
    assert "First ACP entry" in {item["text"] for item in selected["items"]}
    assert "Second ACP entry" not in {item["text"] for item in selected["items"]}


async def test_stdio_replay_of_failed_turn_flushes_original_confirmed_output(acp_runtime, tmp_path):
    env = acp_runtime
    remaining = iter(
        [
            ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        "partial", ToolFunction("send_message", '{"text":"Original partial"}')
                    ),
                ),
            ),
            "",
        ]
    )
    env.provider._responder = lambda _: next(remaining)
    state, identity = tmp_path / "failed-stdio", str(uuid4())
    async with live(env.http_app) as url:
        for attempt in range(2):
            peer = Peer()
            async with process(peer, url, state, token=env.device["token"]) as (connection, _):
                await connection.initialize(protocol_version=1)
                if attempt == 0:
                    session = await connection.new_session(cwd=str(tmp_path), mcp_servers=[])
                with pytest.raises(RequestError, match="did not complete"):
                    await connection.prompt(
                        session_id=session.session_id,
                        prompt=[text("Fail after a partial result")],
                        **{"yuki.requestId": identity},
                    )
                assert [update.content.text for _, update in peer.updates] == ["Original partial"]
                assert len(env.provider.requests) == 2
