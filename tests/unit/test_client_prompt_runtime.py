"""Original local request receipts survive cancellation and missing live owners."""

import asyncio
import json
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import SimpleNamespace
from uuid import uuid4

import pytest
from tests.unit.test_app_messages import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_app_messages import drain
from tests.unit.test_client_api import bearer, pair, person_and_code

from yuki.client_api import prompts as prompts_module
from yuki.client_api.errors import ClientError
from yuki.client_api.prompts import ClientPrompts
from yuki.client_api.requests import ClientRequests, personal_command
from yuki.gateway.providers.app.ingress import AppGateway
from yuki.persistence.models import ChatEventModel

app_env = app_fixture
admin = admin_fixture


async def client_prompt(env):
    _, code = await person_and_code(env.client)
    device = await pair(env.client, code)
    principal = await env.identity.authenticate(device["token"])
    return device, principal, str(uuid4())


async def submit(env, device, request_id):
    response = await env.client.post(
        "/client/v1/messages",
        json={"request_id": request_id, "text": "original local prompt"},
        headers=bearer(device["token"]),
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.parametrize("changed", [False, True])
async def test_terminal_receipt_prepares_original_payload_before_writer(
    app_env, database, monkeypatch, changed
):
    env = app_env
    device, principal, request_id = await client_prompt(env)
    env.gateway.on_finished = None
    original = await submit(env, device, request_id)
    await drain(env)
    calls_before = len(env.calls)
    prompts = ClientPrompts(
        env.identity,
        {"app": env.gateway},
        env.harness.processor._chat.runtime.bindings,
        concurrency=env.harness.processor._concurrency,
    )
    active = ContextVar("terminal_receipt_writer", default=False)
    writer = database.immediate_session
    payload = prompts_module.message_payload
    preparations = 0
    intervened = False

    def prepared_payload(*args):
        nonlocal preparations
        assert not active.get(), "terminal receipt decoded its source inside writer"
        preparations += 1
        return payload(*args)

    @asynccontextmanager
    async def checked_writer():
        nonlocal intervened
        if changed and not intervened:
            intervened = True
            async with writer() as session:
                row = await session.get(ChatEventModel, original["event_id"])
                parts = json.loads(row.segments_json)
                parts[0]["data"]["text"] = "updated original request"
                row.segments_json = json.dumps(parts)
                row.content = "updated original request"
        async with writer() as session:
            token = active.set(True)
            try:
                yield session
            finally:
                active.reset(token)

    monkeypatch.setattr(prompts_module, "message_payload", prepared_payload)
    monkeypatch.setattr(database, "immediate_session", checked_writer)
    await prompts.finish(
        principal,
        request_id,
        original["event_id"],
        SimpleNamespace(reason="chat", handled=True, sent_messages=1),
        None,
    )
    assert preparations == (2 if changed else 1)
    assert len(env.calls) == calls_before
    facts = await ClientRequests(env.identity, prompts=prompts).get(principal, request_id)
    assert facts["turn"] == {"state": "ended", "outcome": "chat"}


async def test_missing_terminal_receipt_remains_unknown_without_replaying_main(app_env):
    env = app_env
    device, principal, request_id = await client_prompt(env)
    # Model a lost terminal write after the real Main/Social execution. The
    # shared fixture now installs the real writer, so absence must be explicit.
    env.gateway.on_finished = None
    original = await submit(env, device, request_id)
    await drain(env)
    assert len(env.calls) == 2
    async with env.identity.database.read_session() as reader:
        assert await personal_command(reader, principal.request_person_id, request_id) is None

    # A cold reader has no original in-process owner or durable terminal fact.
    observer = AppGateway(
        env.identity.database, env.gateway.registry, env.harness.processor, env.social
    )
    prompts = ClientPrompts(
        env.identity,
        {"app": observer},
        env.harness.processor._chat.runtime.bindings,
        concurrency=env.harness.processor._concurrency,
    )
    requests = ClientRequests(env.identity, prompts=prompts)
    facts = await requests.get(principal, request_id)
    assert facts["event_id"] == original["event_id"]
    assert len(facts["result_event_ids"]) == 1
    assert facts["turn"] == {"state": "unknown", "outcome": None}
    cancelled = await prompts.cancel(principal, request_id)
    assert cancelled["cancelled"] is False
    assert cancelled["turn"] == facts["turn"]
    replay = await submit(env, device, request_id)
    assert replay["replay"] and replay["event_id"] == original["event_id"]
    assert len(env.calls) == 2


async def test_late_cancel_joins_blocked_terminal_writer_without_relabelling_main(
    app_env, database
):
    env = app_env
    device, principal, request_id = await client_prompt(env)
    prompts = ClientPrompts(
        env.identity,
        {"app": env.gateway},
        env.harness.processor._chat.runtime.bindings,
        concurrency=env.harness.processor._concurrency,
    )
    main_finished, begin_write, writing = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def finish(*args):
        assert args[-2].reason == "chat" and args[-1] is None
        main_finished.set()
        await begin_write.wait()
        writing.set()
        await prompts.finish(*args)

    env.gateway.on_finished = finish
    await submit(env, device, request_id)
    async with asyncio.timeout(10):
        await main_finished.wait()
        async with database.immediate_session():
            begin_write.set()
            await writing.wait()
            cancellation = asyncio.create_task(prompts.cancel(principal, request_id))
            await asyncio.sleep(0)
            assert not cancellation.done()
        facts = await cancellation
    assert facts["turn"] == {"state": "ended", "outcome": "chat"}
    assert not facts["cancelled"]
    assert len(facts["result_event_ids"]) == 1 and len(env.calls) == 2


async def test_original_cancel_receipt_is_idempotent_after_owner_is_gone(app_env, monkeypatch):
    env = app_env
    device, principal, request_id = await client_prompt(env)
    prompts = ClientPrompts(
        env.identity,
        {"app": env.gateway},
        env.harness.processor._chat.runtime.bindings,
        concurrency=env.harness.processor._concurrency,
    )
    entered = asyncio.Event()

    async def waiting_model(request):
        env.calls.append(request)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(env.provider, "complete", waiting_model)
    await submit(env, device, request_id)
    async with asyncio.timeout(10):
        await entered.wait()
        first = await prompts.cancel(principal, request_id)
        assert not env.gateway.tasks
        repeated = await prompts.cancel(principal, request_id)
    assert first == repeated
    assert first["cancelled"]
    assert first["turn"]["outcome"] == "cancelled"
    assert len(env.calls) == 1 and first["result_event_ids"] == []


async def test_revoked_device_cannot_cancel_an_accepted_prompt(app_env, monkeypatch):
    env = app_env
    device, principal, request_id = await client_prompt(env)
    prompts = ClientPrompts(
        env.identity,
        {"app": env.gateway},
        env.harness.processor._chat.runtime.bindings,
        concurrency=env.harness.processor._concurrency,
    )
    entered, release = asyncio.Event(), asyncio.Event()
    complete = env.provider.complete

    async def waiting_model(request):
        entered.set()
        await release.wait()
        return await complete(request)

    monkeypatch.setattr(env.provider, "complete", waiting_model)
    await submit(env, device, request_id)
    async with asyncio.timeout(10):
        await entered.wait()
        await env.identity.revoke_for_operator(principal.device_id, str(uuid4()), str(uuid4()))
        with pytest.raises(ClientError, match="unauthenticated"):
            await prompts.cancel(principal, request_id)
        assert env.gateway.tasks
        release.set()
        await drain(env)

    # Revocation blocks new API actions; the already finished execution keeps its receipt.
    async with env.identity.database.read_session() as reader:
        from yuki.client_api.requests import personal_command

        receipt = await personal_command(reader, principal.request_person_id, request_id)
        assert receipt is not None and receipt.operation_kind == "client.message"
