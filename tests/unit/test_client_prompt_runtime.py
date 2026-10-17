"""Original local request receipts survive cancellation and missing live owners."""

import asyncio
from uuid import uuid4

import pytest
from tests.unit.test_app_messages import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_app_messages import drain
from tests.unit.test_client_api import bearer, pair, person_and_code

from qq_ai_bot.client_api.errors import ClientError
from qq_ai_bot.client_api.prompts import ClientPrompts
from qq_ai_bot.client_api.requests import ClientRequests

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


async def test_missing_terminal_receipt_remains_unknown_without_replaying_main(app_env):
    env = app_env
    device, principal, request_id = await client_prompt(env)
    original = await submit(env, device, request_id)
    await drain(env)
    assert len(env.calls) == 2

    # A cold reader has no original in-process owner or durable terminal fact.
    prompts = ClientPrompts(env.identity, {"app": env.gateway})
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
    prompts = ClientPrompts(env.identity, {"app": env.gateway})
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
    prompts = ClientPrompts(env.identity, {"app": env.gateway})
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
    prompts = ClientPrompts(env.identity, {"app": env.gateway})
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
    async with env.identity.database.sessions() as reader:
        from qq_ai_bot.client_api.requests import personal_command

        receipt = await personal_command(reader, principal.request_person_id, request_id)
        assert receipt is not None and receipt.operation_kind == "client.message"
