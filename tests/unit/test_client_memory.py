"""Real personal HTTP, canonical App events and original Memory mutations."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event as sql_event
from sqlalchemy import func, select
from tests.conftest import make_settings
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_client_api import bearer, pair
from tests.unit.test_client_works import owner
from tests.unit.test_memory_mutation import _context, _event, _service

from yuki.client_api.http import ClientApi, attach_client_api
from yuki.client_api.memory import ClientMemory
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.memory.enums import MemoryScopeType, MemoryStatus
from yuki.memory.mutation.models import (
    MemoryMutationRequest,
    MemoryMutationTarget,
)
from yuki.memory.signature import fact_signature
from yuki.persistence.models import ChatEventModel, MemoryFactModel, MemoryMutationReceiptModel
from yuki.services.agent_tools import AgentToolService, ToolRuntime

admin = admin_fixture
app_env = app_fixture


@pytest.fixture
async def memory_env(app_env, database):
    mutations, facts, ledger, _ = _service(database, self_memory_enabled=True)
    service = ClientMemory(app_env.identity, {"app": app_env.gateway}, facts, mutations)
    app = FastAPI()
    attach_client_api(app, lambda: ClientApi(app_env.identity, memory=service))
    device, person = await owner(app_env, database)
    issued = await app_env.identity.issue_for_device(
        await app_env.identity.authenticate(device["token"]), str(uuid4())
    )
    second = await pair(app_env.client, issued["code"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield SimpleNamespace(
            client=client,
            service=service,
            facts=facts,
            ledger=ledger,
            mutations=mutations,
            device=device,
            second=second,
            person=person,
            app_env=app_env,
        )


async def seed(env, account="1001", *, self_fact=False):
    source = await _event(
        env.ledger, message_id=str(uuid4()), sender_user_id=account, content="記住：我住在台北"
    )
    result = await env.mutations.mutate(
        MemoryMutationRequest(
            operation="create",
            target=MemoryMutationTarget(
                subject_ref="self" if self_fact else "current_speaker",
                scope_type=MemoryScopeType.SELF if self_fact else MemoryScopeType.PERSON,
            ),
            new_content="我喜歡先核對資料" if self_fact else "我住在台北",
            memory_key=f"preference:{uuid4()}",
            category="self_preference" if self_fact else "location",
        ),
        _context(source),
    )
    assert result.ok
    return await env.facts.get_fact(result.new_fact_id), source


async def items(env, about="me", **params):
    response = await env.client.get(
        "/client/v1/memory/facts",
        params={"about": about, **params},
        headers=bearer(env.device["token"]),
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def correction(fact, action="correct", **updates):
    return {
        "request_id": str(uuid4()),
        "expected_revision": fact_signature(fact),
        "action": action,
        **({"text": "我現在住在台南"} if action == "correct" else {}),
        **updates,
    }


async def post(env, fact, body, device=None):
    return await env.client.post(
        f"/client/v1/memory/facts/{fact.id}/corrections",
        json=body,
        headers=bearer((device or env.device)["token"]),
    )


async def test_correction_versions_real_fact_and_replay_keeps_original_event(memory_env, database):
    env = memory_env
    old, source = await seed(env)
    await seed(env, "1002")
    assert [item["fact_id"] for item in (await items(env))["items"]] == [old.id]
    body = correction(old)
    applied = await post(env, old, body)
    assert applied.status_code == 200, applied.text
    result = applied.json()["data"]
    assert result["old_fact_id"] == old.id and result["new_fact_id"] != old.id
    new = await env.facts.get_fact(result["new_fact_id"])
    assert new.supersedes_id == old.id and new.content == body["text"]
    assert (await env.facts.get_fact(old.id)).status is MemoryStatus.SUPERSEDED
    assert (await items(env))["items"][0]["fact_id"] == new.id
    retry = await post(env, old, body, env.second)
    assert retry.status_code == 200 and retry.json()["data"] == {**result, "replay": True}
    conflict = await post(env, old, correction(old), env.second)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["problem"]["replacement_fact_id"] == new.id
    changed = await post(env, old, {**body, "text": "另一個內容"})
    assert changed.status_code == 409 and changed.json()["problem"]["code"] == "request_conflict"
    detail = await env.client.get(
        f"/client/v1/memory/facts/{new.id}", headers=bearer(env.device["token"])
    )
    assert {item["event_id"] for item in detail.json()["data"]["evidence"]} == {
        source.id,
        result["event_id"],
    }
    async with database.sessions() as session:
        event = await session.get(ChatEventModel, result["event_id"])
        principal = await env.app_env.identity.authenticate(env.device["token"])
        assert event.origin == "memory_correction" and event.direction == "inbound"
        assert event.author_person_id == env.person
        assert event.canonical_conversation_id == principal.conversation_id
    assert not env.app_env.calls  # No Main turn, model request or notification.


async def test_concurrent_devices_cas_one_real_version(memory_env):
    env = memory_env
    old, _ = await seed(env)
    results = await asyncio.gather(
        post(env, old, correction(old)), post(env, old, correction(old), env.second)
    )
    assert sorted(result.status_code for result in results) == [200, 409]
    assert len((await items(env))["items"]) == 1


async def test_retraction_removes_active_search_and_rollback_is_atomic(
    memory_env, database, monkeypatch
):
    env = memory_env
    fact, source = await seed(env)
    tools = AgentToolService(
        settings=make_settings(database.url), ledger=env.ledger, memories=env.facts
    )
    runtime = ToolRuntime(
        inbound=InboundMessage(
            "search",
            "message:private:normal",
            ScopeType.PRIVATE,
            SenderIdentity("1001"),
            "台北",
            bot_user_id="8000",
            person_id=source.author_person_id,
            conversation_id=source.canonical_conversation_id,
            presence_id=source.ingress_presence_id,
        ),
        trigger_event_id=source.id,
    )
    before_search = (
        await tools.execute("search_memory", '{"query":"台北"}', runtime)
    ).model_payload()
    assert before_search["ok"] and before_search["data"]["memories"]
    body = correction(fact, "retract")
    original = env.app_env.identity.receipts.record_personal_command

    async def fail(*args, **kwargs):
        raise RuntimeError("crash after event and mutation before personal receipt")

    async with database.sessions() as session:
        before = [
            await session.scalar(select(func.count()).select_from(model))
            for model in (
                ChatEventModel,
                MemoryFactModel,
                MemoryMutationReceiptModel,
                ControlCommandReceiptModel,
            )
        ]
    monkeypatch.setattr(env.app_env.identity.receipts, "record_personal_command", fail)
    with pytest.raises(RuntimeError, match="crash after"):
        await post(env, fact, body)
    async with database.sessions() as session:
        after = [
            await session.scalar(select(func.count()).select_from(model))
            for model in (
                ChatEventModel,
                MemoryFactModel,
                MemoryMutationReceiptModel,
                ControlCommandReceiptModel,
            )
        ]
    assert after == before
    assert (await env.facts.get_fact(fact.id)).status is MemoryStatus.ACTIVE
    monkeypatch.setattr(env.app_env.identity.receipts, "record_personal_command", original)
    done = await post(env, fact, body)
    assert done.status_code == 200, done.text
    assert done.json()["data"]["applied_operation"] == "invalidate"
    assert not (await items(env))["items"]
    after_search = (
        await tools.execute("search_memory", '{"query":"台北"}', runtime)
    ).model_payload()
    assert after_search["ok"] and not after_search["data"]["memories"]
    scope = await env.service.scopes.authorized(env.person, private_scene=True)
    assert not await env.facts.repository.get_active_authorized(scope, (fact.id,))
    assert (await post(env, fact, body, env.second)).json()["data"]["replay"]


async def test_self_visibility_does_not_grant_foreign_evidence_or_write(memory_env, database):
    env = memory_env
    fact, _source = await seed(env, "1002", self_fact=True)
    # A legitimate private visibility owner is independent of evidence conversation.
    async with database.immediate_session() as session:
        row = await session.get(MemoryFactModel, fact.id)
        row.canonical_visibility_person_id = env.person
    listed = (await items(env, "self"))["items"]
    assert [item["fact_id"] for item in listed] == [fact.id]
    detail = await env.client.get(
        f"/client/v1/memory/facts/{fact.id}", headers=bearer(env.device["token"])
    )
    assert detail.status_code == 200 and detail.json()["data"]["evidence"] == []
    denied = await post(env, fact, correction(fact))
    assert denied.status_code == 404
    foreign, _ = await seed(env, "1002")
    assert not (await items(env))["items"]
    assert (await post(env, foreign, correction(foreign))).status_code == 404


async def test_keyset_and_no_history_scan_after_first_dml(memory_env, database):
    env = memory_env
    first, _ = await seed(env)
    second, _ = await seed(env)
    page = await items(env, limit=1)
    assert page["items"][0]["fact_id"] == second.id and page["next_cursor"]
    assert (await items(env, limit=1, cursor=page["next_cursor"]))["items"][0][
        "fact_id"
    ] == first.id
    writes = set()
    statements = []

    def capture(_connection, _cursor, statement, *_):
        upper = statement.lstrip().upper()
        if upper.startswith(("INSERT", "UPDATE", "DELETE")):
            writes.add(id(_connection))
        if (
            id(_connection) in writes
            and upper.startswith(("SELECT", "WITH"))
            and "memory_evidence" in statement
        ):
            statements.append(statement)

    def ended(connection):
        writes.discard(id(connection))

    sql_event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    sql_event.listen(database.engine.sync_engine, "commit", ended)
    sql_event.listen(database.engine.sync_engine, "rollback", ended)
    try:
        result = await post(env, first, correction(first))
        assert result.status_code == 200, result.text
    finally:
        sql_event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
        sql_event.remove(database.engine.sync_engine, "commit", ended)
        sql_event.remove(database.engine.sync_engine, "rollback", ended)
    # Post-DML evidence access must use prepared caches or exact source identities,
    # never scan the old fact's complete source history again.
    assert not [
        sql
        for sql in statements
        if "from memory_evidence" in sql.lower()
        and ("memory_evidence.fact_id in" in sql.lower() or "count(" in sql.lower())
    ]


async def test_global_evidence_count_without_private_source_leak(memory_env, database):
    env = memory_env
    fact, source = await seed(env, "1002", self_fact=True)
    async with database.immediate_session() as session:
        row = await session.get(MemoryFactModel, fact.id)
        row.visibility_type = "global"
        row.canonical_visibility_person_id = None
    listed = (await items(env, "self"))["items"]
    assert listed[0]["evidence_count"] == 1
    response = await env.client.get(
        f"/client/v1/memory/facts/{fact.id}", headers=bearer(env.device["token"])
    )
    assert response.json()["data"]["evidence"] == []
    assert str(source.id) not in str(response.json()["data"]["evidence"])


async def test_completed_correction_can_be_queried_after_response_loss(memory_env):
    env = memory_env
    fact, _ = await seed(env)
    body = correction(fact)
    original = await post(env, fact, body)
    query = await env.client.get(
        f"/client/v1/requests/{body['request_id']}", headers=bearer(env.second["token"])
    )
    assert query.status_code == 200, query.text
    data = query.json()["data"]
    assert data["status"] == "succeeded"
    assert data["result"] == {
        key: value for key, value in original.json()["data"].items() if key != "replay"
    }


async def test_merged_person_keeps_selected_app_entry_and_original_owners(memory_env, database):
    from yuki.identity.person_identity import merge_persons

    env = memory_env
    fact, _ = await seed(env)
    other_device, other_person = await owner(env.app_env, database, "1002")
    other = await env.app_env.identity.authenticate(other_device["token"])
    async with database.immediate_session() as session:
        await merge_persons(session, env.person, other_person)
    response = await env.client.post(
        f"/client/v1/memory/facts/{fact.id}/corrections",
        json=correction(fact),
        headers={**bearer(env.device["token"]), "X-Yuki-Entry": other.binding_id},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    async with database.sessions() as session:
        source = await session.get(ChatEventModel, data["event_id"])
        assert source.canonical_conversation_id == other.conversation_id
        assert source.author_person_id == other_person
        original = await session.get(MemoryFactModel, fact.id)
        assert original.canonical_subject_person_id == env.person
