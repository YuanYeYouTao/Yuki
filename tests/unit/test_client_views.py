"""Personal snapshots converge after subscription gaps, rollbacks, and final lost hints."""

import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import update
from tests.support.event_seed import seed_scoped_event
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_app_messages import drain
from tests.unit.test_client_api import bearer
from tests.unit.test_client_works import owner, seeded

from yuki.client_api.errors import ClientError
from yuki.client_api.views import ClientViews
from yuki.client_api.works import ClientWorks
from yuki.domain.conversations import ConversationScope
from yuki.execution_trace.recorder import TraceRecorder, trace_span
from yuki.identity.canonical_repository import find_identity_binding
from yuki.runtime.work_schema_v1 import work

admin = admin_fixture
app_env = app_fixture


def decode(frame):
    fields = dict(line.split(": ", 1) for line in frame.strip().splitlines())
    return fields["event"], json.loads(fields["data"])


async def setup_views(env, database):
    device, person = await owner(env, database)
    principal = await env.identity.authenticate(device["token"])
    traces = TraceRecorder(database)
    return (
        device,
        person,
        principal,
        ClientViews(env.identity, ClientWorks(env.identity, env.gateway), traces),
    )


async def test_snapshot_and_keysets_read_only_own_private_entries(app_env, database):
    env = app_env
    device, person, principal, views = await setup_views(env, database)
    original = await seeded(env, database)
    group = await seeded(env, database, group=True)
    event_ids = [original.event.id]
    for index in range(5):
        event_ids.append(
            (
                await seed_scoped_event(
                    env.harness.scoped_events,
                    scope=ConversationScope.private("8000", "1001"),
                    origin_key=str(uuid4()),
                    sender_user_id="1001",
                    direction="inbound",
                    content=f"entry {index}",
                    segments=({"type": "yuki_context", "data": {"secret": "hidden prompt"}},),
                )
            ).event.id
        )
    other = await seeded(env, database, account="1002")
    home = await env.client.get("/client/v1/home", headers=bearer(device["token"]))
    assert home.status_code == 200, home.text
    data = home.json()["data"]
    assert data["main"]["conversation_id"] == principal.conversation_id
    assert [entry["conversation_id"] for entry in data["entries"]] == [
        original.event.canonical_conversation_id
    ]
    assert {item["work_id"] for item in data["works"]} == {original.row["id"], group.row["id"]}
    first = await views.events(principal, original.row["conversation_id"], limit=2)
    second = await views.events(
        principal, original.row["conversation_id"], after=first["next_after"], limit=2
    )
    last = await views.events(
        principal, original.row["conversation_id"], before=event_ids[-1], limit=2
    )
    assert [item["event_id"] for item in first["items"] + second["items"]] == event_ids[:4]
    assert [item["event_id"] for item in last["items"]] == event_ids[-3:-1]
    assert all(item["platform"] == "qq" and item["binding_id"] for item in first["items"])
    assert "hidden prompt" not in json.dumps(first)
    for identity in (group.row["conversation_id"], other.row["conversation_id"]):
        response = await env.client.get(
            f"/client/v1/conversations/{identity}/events", headers=bearer(device["token"])
        )
        assert response.status_code == 404
        response = await env.client.get(
            f"/client/v1/activity?conversation_id={identity}", headers=bearer(device["token"])
        )
        assert response.status_code == 404
    expanded = await env.client.get(
        f"/client/v1/conversations/{original.row['conversation_id']}/events?limit=51",
        headers=bearer(device["token"]),
    )
    assert expanded.status_code == 200, expanded.text
    assert [item["event_id"] for item in expanded.json()["data"]["items"]] == event_ids
    assert expanded.json()["data"]["has_more"] is False
    assert await original.repository.get(original.row["id"]) == original.row
    async with database.read_session() as session:
        assert (await find_identity_binding(session, "1001")).person_id == person


async def test_subscribe_before_sync_covers_snapshot_gap_and_hint_follows_commit(
    app_env, database, monkeypatch
):
    env = app_env
    device, _, principal, views = await setup_views(env, database)
    old = await views.home(principal)
    await env.client.post(
        "/client/v1/messages",
        json={"request_id": str(uuid4()), "text": "between snapshot and subscription"},
        headers=bearer(device["token"]),
    )
    await drain(env)
    sync = views.sync

    async def checked(actor, *, active=False):
        assert database.commit_hints.subscribers
        return await sync(actor, active=active)

    monkeypatch.setattr(views, "sync", checked)
    stream = views.stream(principal)
    kind, initial = decode(await anext(stream))
    assert (
        kind == "sync"
        and initial["conversations"][0]["latest_event_id"] > old["main"]["latest_event_id"]
    )
    # Revision-only changes also wake clients; the writer has not committed yet.
    original = await seeded(env, database, group=True)
    # Consume the creation hint to establish the group's Work projection.
    while True:
        kind, data = decode(await anext(stream))
        if kind == "work" and data["work_id"] == original.row["id"]:
            break
    pending = asyncio.create_task(anext(stream))
    async with database.immediate_session() as session:
        await session.execute(
            update(work)
            .where(work.c.id == original.row["id"])
            .values(revision=work.c.revision + 1, state="waiting_user")
        )
        await asyncio.sleep(0.02)
        assert not pending.done()
    kind, changed = decode(await asyncio.wait_for(pending, 2))
    assert kind == "work" and changed["revision"] == original.row["revision"] + 1
    await stream.aclose()
    assert not database.commit_hints.subscribers


async def test_lost_final_hint_disconnects_and_reconnect_sync_reads_durable_result(
    app_env, database
):
    env = app_env
    device, _, principal, views = await setup_views(env, database)
    stream = views.stream(principal)
    await anext(stream)
    await env.client.post(
        "/client/v1/messages",
        json={"request_id": str(uuid4()), "text": "last durable result"},
        headers=bearer(device["token"]),
    )
    await drain(env)
    for _ in range(70):
        database.commit_hints.publish()
    assert not database.commit_hints.subscribers
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    restored = ClientViews(env.identity, ClientWorks(env.identity, env.gateway))
    reconnect = restored.stream(principal)
    kind, snapshot = decode(await anext(reconnect))
    assert kind == "sync"
    entry = next(
        item
        for item in snapshot["conversations"]
        if item["conversation_id"] == principal.conversation_id
    )
    messages = await restored.events(principal, principal.conversation_id)
    assert entry["latest_event_id"] == messages["items"][-1]["event_id"]
    assert messages["items"][-1]["text"] == "App reply"
    await reconnect.aclose()


async def test_activity_uses_actual_live_spans_and_never_payloads(app_env, database):
    env = app_env
    _, _, principal, views = await setup_views(env, database)
    before = await views.activity(principal, principal.conversation_id)
    assert before["evidence"] == "insufficient"
    async with trace_span(
        "turn",
        {"prompt": "secret prompt", "tool_arguments": "secret args"},
        recorder=views.traces,
        conversation_id=principal.conversation_id,
        origin="user_message",
    ):
        active = await views.activity(principal, principal.conversation_id)
        assert (
            active["evidence"] == "executing" and active["sources"][0]["origin"] == "user_message"
        )
        assert "secret" not in json.dumps(active)
    after = await views.activity(principal, principal.conversation_id)
    assert after["evidence"] == "insufficient" and not after["sources"]


async def test_revoked_device_stream_ends_without_exposing_new_facts(app_env, database):
    env = app_env
    device, _, principal, views = await setup_views(env, database)
    stream = views.stream(principal)
    await anext(stream)
    await env.identity.revoke(principal, device["device_id"], str(uuid4()))
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    with pytest.raises(ClientError, match="unauthenticated"):
        await views.events(principal, principal.conversation_id)
