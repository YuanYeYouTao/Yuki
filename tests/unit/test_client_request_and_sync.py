"""Personal request ownership and snapshot convergence use original server facts."""

import asyncio
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from tests.integration.test_client_continuation import live
from tests.support.event_seed import seed_scoped_event
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_app_messages import drain
from tests.unit.test_client_api import bearer, pair
from tests.unit.test_client_files import upload
from tests.unit.test_client_sdk import DropAcknowledgement
from tests.unit.test_client_works import owner, seeded

from yuki.client_sdk import Outbox, PersonalClient
from yuki.domain.conversations import ConversationScope
from yuki.execution_trace.recorder import TraceRecorder, trace_span
from yuki.identity.db_models import IdentityBindingModel
from yuki.persistence.models import ChatEventModel

admin = admin_fixture
app_env = app_fixture


@pytest.mark.parametrize(
    ("other_device", "operation", "first_operation"),
    [
        (False, "upload", "message"),
        (True, "cancel", "message"),
        (False, "cancel", "command"),
        (True, "upload", "command"),
    ],
)
async def test_message_and_command_share_one_atomic_person_request(
    app_env, database, monkeypatch, other_device, operation, first_operation
):
    env = app_env
    first, _ = await owner(env, database)
    principal = await env.identity.authenticate(first["token"])
    second = first
    if other_device:
        code = await env.identity.issue_for_device(principal, str(uuid4()))
        second = await pair(env.client, code["code"])
    original_work = await seeded(env, database, state="waiting_user")
    identity = str(uuid4())

    async def command():
        if operation == "upload":
            return await upload(env, second, b"original uploaded bytes", request_id=identity)
        return await env.client.post(
            f"/client/v1/works/{original_work.row['id']}/cancel",
            json={"request_id": identity, "expected_revision": original_work.row["revision"]},
            headers=bearer(second["token"]),
        )

    entered, release = asyncio.Event(), asyncio.Event()
    receive = env.gateway.receive

    async def pause_message(*args, **kwargs):
        entered.set()
        await release.wait()
        return await receive(*args, **kwargs)

    monkeypatch.setattr(env.gateway, "receive", pause_message)
    message = asyncio.create_task(
        env.client.post(
            "/client/v1/messages",
            json={"request_id": identity, "text": "original message"},
            headers=bearer(first["token"]),
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    if first_operation == "command":
        commanded = await command()
        release.set()
        sent = await asyncio.wait_for(message, 10)
    else:
        release.set()
        sent = await asyncio.wait_for(message, 10)
        commanded = await command()
    await drain(env)
    accepted, rejected = (sent, commanded) if first_operation == "message" else (commanded, sent)
    assert accepted.status_code == 200, accepted.text
    assert rejected.status_code == 409 and rejected.json()["problem"]["code"] == "request_conflict"
    found = await env.client.get(f"/client/v1/requests/{identity}", headers=bearer(first["token"]))
    expected = (
        "message"
        if first_operation == "message"
        else f"client.{'work_cancel' if operation == 'cancel' else 'upload'}"
    )
    assert found.json()["data"]["operation_kind"] == expected
    async with database.read_session() as session:
        count = await session.scalar(
            select(func.count(ChatEventModel.id)).where(
                ChatEventModel.canonical_conversation_id == principal.conversation_id,
                ChatEventModel.direction == "inbound",
            )
        )
    assert count == (1 if first_operation == "message" else 0)
    assert bool(env.calls) == (first_operation == "message")


async def test_message_lost_ack_is_not_shadowed_by_later_command(app_env, database, tmp_path):
    env = app_env
    device, _ = await owner(env, database)
    transport = DropAcknowledgement(env.app)
    async with httpx.AsyncClient(transport=transport) as http:
        box = Outbox(tmp_path / "outbox.sqlite")
        client = PersonalClient("http://test", device["token"], box, http=http)
        identity = await client.prepare_message("accepted before acknowledgement was lost")
        with pytest.raises(httpx.ReadTimeout):
            await client.submit(identity)
        rejected = await upload(env, device, b"different operation", request_id=identity)
        assert rejected.status_code == 409
        await drain(env)
        restored = PersonalClient("http://test", device["token"], Outbox(box.path), http=http)
        result = await restored.submit(identity)
        assert result["state"] == "result" and result["result"]["operation_kind"] == "message"
        assert transport.posts == 1


async def test_unreadable_old_request_does_not_block_home_other_requests_or_watch(
    app_env, database, tmp_path
):
    env = app_env
    device, _ = await owner(env, database)
    original = await seeded(env, database, group=True, state="waiting_user")
    await seed_scoped_event(
        env.harness.scoped_events,
        scope=ConversationScope.group("8000", "2001"),
        origin_key=str(uuid4()),
        sender_user_id="1002",
        direction="inbound",
        content="another group member",
    )
    async with live(env.app) as url:
        client = PersonalClient(url, device["token"], Outbox(tmp_path / "outbox.sqlite"))
        try:
            old = await client.prepare_input(original.row["id"], "original accepted input")
            assert (await client.submit(old))["state"] == "accepted"
            assert await env.harness.profiles.delete_person("1002")
            assert await original.repository.get(original.row["id"]) is None
            current = await client.prepare_message("a new independent request")
            await client.submit(current)
            await drain(env)
            home = await client.refresh()
            assert home["main"]["conversation_id"]
            assert client.outbox.get(old)["state"] == "accepted"
            assert client.outbox.get(old)["problem"] == "not_found"
            assert client.outbox.get(current)["state"] == "result"
            watch = client.watch()
            try:
                assert (await asyncio.wait_for(anext(watch), 5))["main"] == home["main"]
            finally:
                await watch.aclose()
            assert len(env.calls) == 2
        finally:
            await client.close()


async def append_history(env, database, principal, texts):
    async with database.read_session() as session:
        binding = await session.get(IdentityBindingModel, principal.binding_id)
    rows = []
    for text in texts:
        message = env.gateway.message(
            binding,
            str(uuid4()),
            text,
            ({"type": "text", "data": {"text": text}},),
            conversation_id=principal.conversation_id,
        )
        admitted = await env.gateway.processor._canonical_ingress.pre_admit(
            env.gateway.handle, message
        )
        rows.append(
            await env.gateway.processor._canonical_uow.append_inbound(admitted.message, admitted)
        )
    return rows


async def test_activity_skips_unchanged_history_and_new_events_use_original_cursor(
    app_env, database, tmp_path
):
    env = app_env
    device, _ = await owner(env, database)
    principal = await env.identity.authenticate(device["token"])
    rows = await append_history(
        env, database, principal, (f"history {index}" for index in range(53))
    )
    pages = []

    async def record(request):
        if request.url.path.endswith("/events"):
            pages.append(int(request.url.params["after"]))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), event_hooks={"request": [record]}
    ) as http:
        client = PersonalClient(
            "http://test", device["token"], Outbox(tmp_path / "sync.sqlite"), http=http
        )
        await client.refresh()
        assert pages == [0, rows[49].event.id]
        pages.clear()
        recorder = TraceRecorder(database)
        async with trace_span(
            "turn", {}, recorder=recorder, conversation_id=principal.conversation_id
        ):
            await client.refresh()
        assert pages == []
        added = await append_history(env, database, principal, ("new event",))
        await client.refresh()
        assert pages == [rows[-1].event.id]
        assert client.events[principal.conversation_id][-1]["event_id"] == added[0].event.id
        assert not env.calls


async def test_history_source_change_reloads_and_removes_stale_content(app_env, database, tmp_path):
    env = app_env
    device, _ = await owner(env, database)
    principal = await env.identity.authenticate(device["token"])
    rows = await append_history(env, database, principal, ("original", "to delete"))
    client = PersonalClient(
        "http://test", device["token"], Outbox(tmp_path / "sync.sqlite"), http=env.client
    )
    await client.refresh()
    async with database.immediate_session() as session:
        first = await session.get(ChatEventModel, rows[0].event.id)
        first.content = "corrected"
        second = await session.get(ChatEventModel, rows[1].event.id)
        await session.delete(second)
    await client.refresh()
    assert [(row["event_id"], row["text"]) for row in client.events[principal.conversation_id]] == [
        (rows[0].event.id, "corrected")
    ]
    assert not env.calls
