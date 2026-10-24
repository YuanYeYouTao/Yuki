"""Actual personal management HTTP, domain changes and original recovery boundaries."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, event, func, insert, select, update
from tests.conftest import make_settings
from tests.support.event_seed import seed_scoped_event
from tests.support.workspace_snapshots import snapshot_bytes
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_app_messages import drain
from tests.unit.test_client_api import bearer, pair, person_and_code
from tests.unit.test_client_files import app_context
from tests.unit.test_client_works import owner, seeded
from tests.unit.test_memory_mutation import _event, _service

from yuki.automation.repository import AutomationRepository
from yuki.automation.service import AutomationService
from yuki.character.repository import CharacterRepository
from yuki.client_api.automations import ClientAutomations
from yuki.client_api.capabilities import ClientCapabilities
from yuki.client_api.computer import ClientComputer
from yuki.client_api.http import ClientApi, attach_client_api
from yuki.client_api.identity import fingerprint
from yuki.client_api.memory import ClientMemory
from yuki.client_api.openapi import client_openapi
from yuki.client_api.views import ClientViews
from yuki.client_api.work_projection import public_step
from yuki.client_api.works import ClientWorks
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.domain.conversations import ConversationScope
from yuki.domain.tool_actor import ToolActor
from yuki.identity.db_models import PresenceModel
from yuki.memory.enums import MemoryScopeType, MemorySourceType, SelfMemoryVisibility
from yuki.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from yuki.persistence.models import ChatEventModel, MemoryMutationReceiptModel
from yuki.runtime.origin import TurnOrigin
from yuki.runtime.work_recovery_schema import deliveries
from yuki.runtime.work_schema_v1 import effects
from yuki.social.db_models import SocialOperationModel
from yuki.time.service import TimeContextService

admin = admin_fixture
app_env = app_fixture


@pytest.fixture
async def management(app_env, database):
    env = app_env
    mutations, facts, ledger, _ = _service(database, self_memory_enabled=True)
    memory = ClientMemory(env.identity, facts, mutations, env.gateway)
    repository = AutomationRepository(database)
    settings = make_settings(database.url, automation_enabled=True)
    service = AutomationService(
        settings=settings, repository=repository, time_service=TimeContextService()
    )
    automations = ClientAutomations(env.identity, repository, service)
    character = CharacterRepository(env.identity, settings)
    await character.initialize("Management character")
    works = ClientWorks(env.identity, env.gateway)
    views = ClientViews(env.identity, works, character=character)
    app = FastAPI()
    attach_client_api(
        app,
        lambda: ClientApi(
            env.identity,
            works=works,
            views=views,
            files=env.files,
            character=character,
            memory=memory,
            automations=automations,
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://management"
    ) as client:
        yield SimpleNamespace(
            app=app,
            client=client,
            env=env,
            memory=memory,
            mutations=mutations,
            facts=facts,
            ledger=ledger,
            automations=automations,
            automation_service=service,
            character=character,
            views=views,
            works=works,
        )


async def fact_for(case, person, account="1001", *, self_for=None):
    source = await _event(
        case.ledger, message_id=str(uuid4()), sender_user_id=account, content="original preference"
    )
    fact = await case.facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.SELF if self_for else MemoryScopeType.PERSON,
            canonical_subject_person_id=None if self_for else person,
            visibility_type=SelfMemoryVisibility.PRIVATE if self_for else None,
            canonical_visibility_person_id=self_for,
            memory_key="desktop-preference",
            category="preference",
            content="original preference",
            source_type=MemorySourceType.EXPLICIT,
        ),
        evidence=MemoryEvidenceCreate(
            event_id=source.id,
            source_speaker_user_id=account,
            excerpt=source.content,
            relation="explicit_command",
        ),
    )
    return fact


async def detail(case, device, fact):
    response = await case.client.get(
        f"/client/v1/memory/facts/{fact.id}", headers=bearer(device["token"])
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def correction(case, device, fact_id, body):
    return await case.client.post(
        f"/client/v1/memory/facts/{fact_id}/corrections", json=body, headers=bearer(device["token"])
    )


async def test_memory_own_subject_evidence_and_atomic_original_correction(management, database):
    case = management
    first, person = await owner(case.env, database)
    principal = await case.env.identity.authenticate(first["token"])
    code = await case.env.identity.issue_for_device(principal, str(uuid4()))
    second = await pair(case.env.client, code["code"])
    fact = await fact_for(case, person)
    original = await detail(case, first, fact)
    assert original["scope"] == "person" and original["writable"]
    assert original["evidence"] and original["evidence"][0]["event_id"] > 0
    body = {
        "request_id": str(uuid4()),
        "expected_revision": original["revision"],
        "action": "correct",
        "text": "updated explicit preference",
    }
    response = await correction(case, first, fact.id, body)
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["applied_operation"] == "correct" and result["new_fact_id"] != fact.id
    replay = await correction(case, second, fact.id, body)
    assert replay.status_code == 200 and replay.json()["data"] == {**result, "replay": True}
    assert not case.env.calls
    async with database.read_session() as reader:
        event = await reader.get(ChatEventModel, result["event_id"])
        assert (
            event.origin == "memory_correction"
            and event.author_person_id == principal.source_person_id
        )
        assert event.canonical_conversation_id == principal.conversation_id
        assert (
            await reader.scalar(select(func.count()).select_from(MemoryMutationReceiptModel)) == 1
        )
        receipt = await reader.scalar(
            select(ControlCommandReceiptModel).where(
                ControlCommandReceiptModel.request_id == body["request_id"]
            )
        )
        assert (
            receipt.operation_kind == "client.memory_correction"
            and receipt.operation_ref == str(fact.id)
        )
        assert receipt.request_person_id == principal.request_person_id
        assert receipt.payload_hash == fingerprint(
            "client.memory_correction",
            str(fact.id),
            {key: body[key] for key in ("expected_revision", "action", "text")},
        )
    assert (await case.facts.get_fact(fact.id)).status.value == "superseded"
    assert (await case.facts.get_fact(result["new_fact_id"])).content == body["text"]
    conflict = await correction(case, first, fact.id, {**body, "text": "different payload"})
    assert conflict.status_code == 409 and conflict.json()["problem"]["code"] == "request_conflict"
    found = await case.client.get(
        f"/client/v1/requests/{body['request_id']}", headers=bearer(second["token"])
    )
    assert found.json()["data"]["operation_kind"] == "client.memory_correction"


async def test_memory_interrupted_preparation_resumes_one_original_user_action(
    management, database, monkeypatch
):
    case = management
    device, person = await owner(case.env, database)
    fact = await fact_for(case, person)
    original = await detail(case, device, fact)
    body = {
        "request_id": str(uuid4()),
        "expected_revision": original["revision"],
        "action": "correct",
        "text": "resume original action",
    }

    async def interrupted(*args, **kwargs):
        raise RuntimeError("lost before domain preparation")

    with monkeypatch.context() as patch:
        patch.setattr(case.mutations, "prepare_mutation", interrupted)
        with pytest.raises(RuntimeError, match="lost before"):
            await correction(case, device, fact.id, body)
    pending = await case.client.get(
        f"/client/v1/requests/{body['request_id']}", headers=bearer(device["token"])
    )
    assert pending.json()["data"] == {"found": False}
    async with database.read_session() as reader:
        source_id = await reader.scalar(
            select(ChatEventModel.id).where(ChatEventModel.origin == "memory_correction")
        )
        assert source_id is not None
        assert (
            await reader.scalar(select(func.count()).select_from(MemoryMutationReceiptModel)) == 0
        )
    response = await correction(case, device, fact.id, body)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["event_id"] == source_id
    async with database.read_session() as reader:
        assert (
            await reader.scalar(
                select(func.count())
                .select_from(ChatEventModel)
                .where(ChatEventModel.origin == "memory_correction")
            )
            == 1
        )
    assert not case.env.calls


async def test_memory_self_is_read_only_and_owner_does_not_read_other_private_facts(
    management, database
):
    case = management
    first, person = await owner(case.env, database)
    other, other_person = await owner(case.env, database, "1002")
    await case.character.set_owner(str(uuid4()), person, str(uuid4()), granted=True)
    private = await fact_for(case, other_person, "1002")
    private_self = await fact_for(case, other_person, "1002", self_for=other_person)
    own_self = await fact_for(case, person, self_for=person)
    own = await detail(case, first, own_self)
    assert own["scope"] == "self" and not own["writable"]
    page = await case.client.get(
        "/client/v1/memory/facts?about=self", headers=bearer(first["token"])
    )
    assert [item["fact_id"] for item in page.json()["data"]["items"]] == [own_self.id]
    for fact in (private, private_self):
        assert (
            await case.client.get(
                f"/client/v1/memory/facts/{fact.id}", headers=bearer(first["token"])
            )
        ).status_code == 404
    body = {"request_id": str(uuid4()), "expected_revision": own["revision"], "action": "retract"}
    assert (await correction(case, first, own_self.id, body)).status_code == 404
    assert (await detail(case, other, private))["fact_id"] == private.id


async def test_memory_writer_rechecks_device_and_rolls_back_both_receipts(
    management, database, monkeypatch
):
    case = management
    device, person = await owner(case.env, database)
    principal = await case.env.identity.authenticate(device["token"])
    fact = await fact_for(case, person)
    original = await detail(case, device, fact)
    prepare = case.mutations.prepare_mutation

    async def revoked(*args, **kwargs):
        plan = await prepare(*args, **kwargs)
        await case.env.identity.revoke(principal, device["device_id"], str(uuid4()))
        return plan

    monkeypatch.setattr(case.mutations, "prepare_mutation", revoked)
    body = {
        "request_id": str(uuid4()),
        "expected_revision": original["revision"],
        "action": "retract",
    }
    response = await correction(case, device, fact.id, body)
    assert response.status_code == 401, response.text
    assert (await case.facts.get_fact(fact.id)).status.value == "active"
    async with database.read_session() as reader:
        assert (
            await reader.scalar(select(func.count()).select_from(MemoryMutationReceiptModel)) == 0
        )
        assert (
            await reader.scalar(
                select(ControlCommandReceiptModel.request_id).where(
                    ControlCommandReceiptModel.request_id == body["request_id"]
                )
            )
            is None
        )


async def test_work_keysets_steps_and_status_stay_personal(management, database):
    case = management
    device, _ = await owner(case.env, database)
    first = await seeded(case.env, database)
    second = await seeded(case.env, database, state="failed")
    stranger, _ = await owner(case.env, database, "1002")
    private = await seeded(case.env, database, account="1002")
    async with database.immediate_session() as writer:
        for index in range(3):
            await writer.execute(
                insert(effects).values(
                    effect_key=f"private-internal-key-{index}",
                    work_id=first.row["id"],
                    kind="tool",
                    state="accepted",
                    receipt_json=json.dumps(
                        {
                            "invocation": {
                                "tool_id": "web_search",
                                "arguments": {"query": "SECRET_TOOL_INPUT"},
                            },
                            "outcome": {"ok": True},
                        }
                    ),
                    created=float(index + 1),
                    updated=float(index + 1),
                )
            )
    headers = bearer(device["token"])
    page = await case.client.get("/client/v1/works?limit=1", headers=headers)
    assert page.status_code == 200, page.text
    assert [item["work_id"] for item in page.json()["data"]["works"]] == [first.row["id"]]
    ended = await case.client.get("/client/v1/works?group=ended&limit=1", headers=headers)
    assert ended.json()["data"]["works"][0]["work_id"] == second.row["id"]
    step_page = await case.client.get(
        f"/client/v1/works/{first.row['id']}/steps?limit=1", headers=headers
    )
    assert step_page.status_code == 200, step_page.text
    assert (
        "SECRET_TOOL_INPUT" not in step_page.text and "private-internal-key" not in step_page.text
    )
    after = step_page.json()["data"]["next_after"]
    following = await case.client.get(
        f"/client/v1/works/{first.row['id']}/steps",
        params={"after": after, "limit": 1},
        headers=headers,
    )
    assert (
        following.json()["data"]["steps"][0]["step_id"]
        != step_page.json()["data"]["steps"][0]["step_id"]
    )
    assert (
        await case.client.get(f"/client/v1/works/{private.row['id']}/steps", headers=headers)
    ).status_code == 404
    assert (
        await case.client.get(
            f"/client/v1/works/{first.row['id']}/steps", headers=bearer(stranger["token"])
        )
    ).status_code == 404
    status = await case.client.get("/client/v1/character/status", headers=headers)
    assert status.status_code == 200 and status.json()["data"]["mine"]["works"]["running"] == 1
    assert (
        await case.client.get("/client/v1/works?before=malformed", headers=headers)
    ).status_code == 422


async def test_event_pages_retain_completed_work_links_from_original_delivery_and_receipt(
    management, database
):
    case = management
    device, _ = await owner(case.env, database)
    completed = await seeded(case.env, database, state="completed")
    await owner(case.env, database, "1002")
    private = await seeded(case.env, database, account="1002", state="completed")
    result_ids = []
    for target, status, receipt_kind in (
        (completed, "succeeded", "delivery"),
        (completed, "succeeded", "effect"),
        (private, "succeeded", "effect"),
        (completed, "uncertain", "effect"),
    ):
        result = (
            await seed_scoped_event(
                case.env.harness.scoped_events,
                scope=ConversationScope.private("8000", "1001"),
                platform_message_id=str(uuid4()),
                sender_user_id="8000",
                direction="outbound",
                content=f"actual result {len(result_ids)}",
            )
        ).event
        result_ids.append(result.id)
        operation = str(uuid4())
        async with database.immediate_session() as writer:
            writer.add(
                SocialOperationModel(
                    id=operation,
                    source_turn_id=f"{target.row['conversation_id']}:event:{target.event.id}",
                    tool_call_id=str(uuid4()),
                    source_conversation_id=target.row["conversation_id"],
                    action="send",
                    payload_hash="a" * 64,
                    target_kind="person",
                    target_id=completed.event.author_person_id,
                    presence_id=completed.event.ingress_presence_id,
                    status=status,
                    event_id=result.id,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
            if receipt_kind == "delivery":
                await writer.execute(
                    insert(deliveries).values(
                        id=operation,
                        work_id=target.row["id"],
                        kind="result",
                        state="delivered",
                        created=1.0,
                        updated=1.0,
                    )
                )
            else:
                await writer.execute(
                    insert(effects).values(
                        effect_key=f"effect:{operation}",
                        work_id=target.row["id"],
                        kind="social",
                        state="accepted",
                        receipt_json=json.dumps(
                            {"invocation": {"original_domain_ref": f"social:{operation}"}}
                        ),
                        created=1.0,
                        updated=1.0,
                    )
                )
    headers = bearer(device["token"])
    home = await case.client.get("/client/v1/home?works=active", headers=headers)
    assert home.status_code == 200 and home.json()["data"]["works"] == []
    queries = []

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().startswith("SELECT") and "page_work_links" in statement:
            queries.append((statement, parameters))

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        response = await case.client.get(
            f"/client/v1/conversations/{completed.row['conversation_id']}/events", headers=headers
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    assert queries
    async with database.read_session() as reader:
        connection = await reader.connection()
        plan = await connection.exec_driver_sql(
            "EXPLAIN QUERY PLAN " + queries[0][0], queries[0][1]
        )
        assert "ix_runtime_work_trigger_event" in str(plan.all())
    page = {item["event_id"]: item["work_ids"] for item in response.json()["data"]["items"]}
    assert page[completed.event.id] == [completed.row["id"]]
    assert page[result_ids[0]] == page[result_ids[1]] == [completed.row["id"]]
    assert page[result_ids[2]] == page[result_ids[3]] == []
    assert private.row["id"] not in response.text
    isolated = await case.client.get(
        f"/client/v1/conversations/{completed.row['conversation_id']}/events",
        params={"after": result_ids[0] - 1, "limit": 1},
        headers=headers,
    )
    assert [item["event_id"] for item in isolated.json()["data"]["items"]] == [result_ids[0]]
    assert isolated.json()["data"]["items"][0]["work_ids"] == [completed.row["id"]]


async def test_artifact_directory_uses_original_confirmed_versions_and_revocation(
    management, database
):
    case = management
    device, _ = await owner(case.env, database)
    context = await app_context(case.env, device)
    for name, data in (("first.txt", b"first"), ("source.py", b"print('source')")):
        snapshot = snapshot_bytes(case.env.store, name, data)
        sent = await case.env.social.execute(
            "send",
            {"artifact_id": snapshot["artifact_id"], "attachment_kind": "file"},
            replace(context, call_id=str(uuid4())),
        )
        assert sent["status"] == "succeeded"
    response = await case.client.get(
        "/client/v1/artifacts?limit=1", headers=bearer(device["token"])
    )
    assert response.status_code == 200, response.text
    page = response.json()["data"]
    assert len(page["items"]) == 1 and page["next_cursor"]
    following = await case.client.get(
        "/client/v1/artifacts",
        params={"cursor": page["next_cursor"], "limit": 1},
        headers=bearer(device["token"]),
    )
    assert (
        following.status_code == 200
        and following.json()["data"]["items"][0]["artifact_id"] != page["items"][0]["artifact_id"]
    )
    code = await case.client.get("/client/v1/artifacts?kind=code", headers=bearer(device["token"]))
    assert [item["name"] for item in code.json()["data"]["items"]] == ["source.py"]
    _, other_code = await person_and_code(case.env.client)
    stranger = await pair(case.env.client, other_code)
    assert (
        await case.client.get("/client/v1/artifacts", headers=bearer(stranger["token"]))
    ).json()["data"]["items"] == []
    item = page["items"][0]
    async with database.immediate_session() as writer:
        await writer.execute(
            delete(ChatEventModel).where(ChatEventModel.id == item["ref"]["event_id"])
        )
    remaining = await case.client.get("/client/v1/artifacts", headers=bearer(device["token"]))
    assert item["artifact_id"] not in remaining.text


async def test_automation_transitions_keep_current_definition_and_original_receipts(
    management, database
):
    case = management
    device, person = await owner(case.env, database)
    principal = await case.env.identity.authenticate(device["token"])
    async with database.read_session() as reader:
        presence = await reader.get(PresenceModel, principal.presence_id)
    actor = ToolActor(
        user_id=principal.external_account_id,
        bot_user_id=presence.external_account_id,
        group_id=None,
        origin=TurnOrigin.USER_MESSAGE,
        instruction="create real fixture schedule",
        execution_id=str(uuid4()),
        person_id=person,
        conversation_id=principal.conversation_id,
        presence_id=principal.presence_id,
        platform="app",
    )
    row = await case.automation_service.create_task(
        {
            "goal": "keep the original complete request",
            "name": "fixture schedule",
            "trigger": {"type": "after", "seconds": 60},
        },
        actor=actor,
        conversation_key=f"conversation:{principal.conversation_id}",
    )
    response = await case.client.get("/client/v1/automations", headers=bearer(device["token"]))
    assert response.status_code == 200, response.text
    current = response.json()["data"]["items"][0]
    body = {"request_id": str(uuid4()), "expected_revision": current["revision"]}
    url = f"/client/v1/automations/{row.id}/actions/pause"
    paused = await case.client.post(url, json=body, headers=bearer(device["token"]))
    assert paused.status_code == 200 and paused.json()["data"]["state"] == "paused", paused.text
    replay = await case.client.post(url, json=body, headers=bearer(device["token"]))
    assert replay.status_code == 200 and replay.json()["data"]["replay"]
    request = await case.client.get(
        f"/client/v1/requests/{body['request_id']}", headers=bearer(device["token"])
    )
    assert request.json()["data"]["target"] == str(row.id)
    stale = await case.client.post(
        f"/client/v1/automations/{row.id}/actions/resume",
        json={**body, "request_id": str(uuid4())},
        headers=bearer(device["token"]),
    )
    assert stale.status_code == 409
    other, _ = await owner(case.env, database, "1002")
    denied = await case.client.post(
        url,
        json={"request_id": str(uuid4()), "expected_revision": paused.json()["data"]["revision"]},
        headers=bearer(other["token"]),
    )
    assert denied.status_code == 404
    original = await case.automations.repository.get(row.id)
    assert original.version_id == row.version_id and original.script == row.script


def test_management_openapi_comes_from_real_response_models():
    document = client_openapi()
    assert document["info"]["x-yuki-api-revision"] == 6
    paths = document["paths"]
    for path in (
        "/client/v1/memory/facts",
        "/client/v1/automations",
        "/client/v1/character/status",
        "/client/v1/character/capabilities",
        "/client/v1/works",
        "/client/v1/works/{identity}/steps",
        "/client/v1/artifacts",
        "/client/v1/me/proactive-route",
    ):
        assert path in paths
    schemas = document["components"]["schemas"]
    assert schemas["MemoryFactView"]["properties"]["writable"]["type"] == "boolean"
    assert "wait" in schemas["Work"]["properties"]
    assert "discriminator" in schemas["WorkWait"]["properties"]["conditions"]["items"]


@pytest.mark.parametrize("action", ["correct", "retract"])
async def test_memory_personal_receipt_failure_rolls_back_mutation_and_resumes_original_source(
    management, database, monkeypatch, action
):
    case = management
    device, person = await owner(case.env, database)
    fact = await fact_for(case, person)
    original = await detail(case, device, fact)
    body = {
        "request_id": str(uuid4()),
        "expected_revision": original["revision"],
        "action": action,
        "text": "corrected once" if action == "correct" else None,
    }
    record = case.env.identity.receipts.record_personal_command

    async def fail(session, **kwargs):
        await record(session, **kwargs)
        if kwargs["operation"] == "client.memory_correction":
            raise RuntimeError("failed after personal receipt")

    with monkeypatch.context() as patch:
        patch.setattr(case.env.identity.receipts, "record_personal_command", fail)
        with pytest.raises(RuntimeError, match="failed after"):
            await correction(case, device, fact.id, body)
    assert (await case.facts.get_fact(fact.id)).status.value == "active"
    async with database.read_session() as reader:
        assert (
            await reader.scalar(select(func.count()).select_from(MemoryMutationReceiptModel)) == 0
        )
        assert (
            await reader.scalar(
                select(ControlCommandReceiptModel.request_id).where(
                    ControlCommandReceiptModel.request_id == body["request_id"]
                )
            )
            is None
        )
        source = await reader.scalar(
            select(ChatEventModel.id).where(ChatEventModel.origin == "memory_correction")
        )
    response = await correction(case, device, fact.id, body)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["event_id"] == source
    assert (await case.facts.get_fact(fact.id)).status.value == (
        "superseded" if action == "correct" else "invalidated"
    )


async def test_memory_prepared_evidence_changes_do_not_commit_a_correction(
    management, database, monkeypatch
):
    case = management
    device, person = await owner(case.env, database)
    fact = await fact_for(case, person)
    original = await detail(case, device, fact)
    source = original["evidence"][0]["event_id"]
    prepare = case.mutations.prepare_mutation

    async def hidden(*args, **kwargs):
        plan = await prepare(*args, **kwargs)
        async with database.immediate_session() as writer:
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == source)
                .values(suppression_status="duplicate", utterance_fingerprint="f" * 64)
            )
        return plan

    monkeypatch.setattr(case.mutations, "prepare_mutation", hidden)
    body = {
        "request_id": str(uuid4()),
        "expected_revision": original["revision"],
        "action": "correct",
        "text": "must retain original evidence",
    }
    response = await correction(case, device, fact.id, body)
    assert (
        response.status_code == 409 and response.json()["problem"]["code"] == "version_conflict"
    ), response.text
    assert (await case.facts.get_fact(fact.id)).status.value == "active"
    async with database.read_session() as reader:
        assert (
            await reader.scalar(select(func.count()).select_from(MemoryMutationReceiptModel)) == 0
        )


async def test_proactive_route_uses_original_person_target_and_does_not_grant_foreign_binding(
    management, database
):
    case = management
    device, _ = await owner(case.env, database)
    principal = await case.env.identity.authenticate(device["token"])
    other, _ = await owner(case.env, database, "1002")
    foreign = await case.env.identity.authenticate(other["token"])
    headers = bearer(device["token"])
    current = (await case.client.get("/client/v1/me/proactive-route", headers=headers)).json()[
        "data"
    ]
    body = {
        "request_id": str(uuid4()),
        "expected_revision": current["revision"],
        "binding_id": principal.binding_id,
    }
    changed = await case.client.put("/client/v1/me/proactive-route", headers=headers, json=body)
    assert changed.status_code == 200, changed.text
    result = changed.json()["data"]
    assert result["binding_id"] == principal.binding_id and not result["paused"]
    replay = await case.client.put("/client/v1/me/proactive-route", headers=headers, json=body)
    assert replay.status_code == 200 and replay.json()["data"]["revision"] == result["revision"]
    receipt = await case.client.get(f"/client/v1/requests/{body['request_id']}", headers=headers)
    assert receipt.json()["data"]["target"] == principal.request_person_id
    assert receipt.json()["data"]["payload_hash"] == fingerprint(
        "client.proactive_route",
        principal.request_person_id,
        {"expected_revision": body["expected_revision"], "binding_id": principal.binding_id},
    )
    denied = await case.client.put(
        "/client/v1/me/proactive-route",
        headers=headers,
        json={
            "request_id": str(uuid4()),
            "expected_revision": result["revision"],
            "binding_id": foreign.binding_id,
        },
    )
    assert denied.status_code == 404


async def test_character_fixture_seeds_real_domains_and_loses_only_the_completed_ack(
    management, database
):
    from tests.support.desktop_character_fixture import attach_character_fixture

    case = management
    device, person = await owner(case.env, database)
    source = await case.env.client.post(
        "/client/v1/messages",
        headers=bearer(device["token"]),
        json={"request_id": str(uuid4()), "text": "這是記憶的原始依據。"},
    )
    assert source.status_code == 200
    await drain(case.env)
    await attach_character_fixture(
        case.app,
        SimpleNamespace(
            database=database,
            character=case.character,
            ledger=case.ledger,
            memory_mutations=case.mutations,
            automation=case.automation_service,
        ),
        person,
    )
    response = await case.client.post("/__test/character-data")
    assert response.status_code == 200, response.text
    seed = response.json()
    assert (
        len(seed["fact_ids"]) == 52 and seed["source_event_id"] == source.json()["data"]["event_id"]
    )
    page = await case.client.get(
        "/client/v1/memory/facts?limit=50", headers=bearer(device["token"])
    )
    assert len(page.json()["data"]["items"]) == 50 and page.json()["data"]["next_cursor"]
    fact_id = seed["fact_ids"][-1]
    detail_response = await case.client.get(
        f"/client/v1/memory/facts/{fact_id}", headers=bearer(device["token"])
    )
    body = {
        "request_id": str(uuid4()),
        "expected_revision": detail_response.json()["data"]["revision"],
        "action": "correct",
        "text": "real committed correction",
    }
    await case.client.post("/__test/lose-next-memory-response")
    lost = await correction(case, device, fact_id, body)
    assert lost.status_code == 503
    await case.client.post("/__test/release-original-queries")
    original = await case.client.get(
        f"/client/v1/requests/{body['request_id']}", headers=bearer(device["token"])
    )
    receipt = original.json()["data"]
    assert (
        receipt["operation_kind"] == "client.memory_correction" and receipt["status"] == "succeeded"
    )
    assert (await case.facts.get_fact(receipt["result"]["new_fact_id"])).content == body["text"]
    arrangements = await case.client.get("/client/v1/automations", headers=bearer(device["token"]))
    assert [row["automation_id"] for row in arrangements.json()["data"]["items"]] == [
        seed["automation_id"]
    ]


async def test_commit_refresh_and_computer_hint_use_existing_sse_lifecycle(management, database):
    case = management
    device, _ = await owner(case.env, database)
    principal = await case.env.identity.authenticate(device["token"])
    changes = []
    computer = SimpleNamespace(
        revision=7, device_connection=lambda identity, delta: changes.append((identity, delta))
    )
    case.views.computer = computer
    stream = case.views.stream(principal)
    try:
        first = await anext(stream)
        assert '"computer_revision": 7' in first
        assert changes == [(principal.device_id, 1)]
        pending = asyncio.create_task(anext(stream))
        database.commit_hints.publish()
        refresh = await asyncio.wait_for(pending, 2)
        assert "event: refresh\n" in refresh and "data: {}" in refresh
        pending = asyncio.create_task(anext(stream))
        computer.revision += 1
        database.commit_hints.publish()
        event = await asyncio.wait_for(pending, 2)
        assert "event: computer\n" in event and '"revision": 8' in event
    finally:
        await stream.aclose()
    assert changes[-1] == (principal.device_id, -1)


async def test_active_stream_aggregates_only_current_roots_and_refreshes_late_history(
    management, database
):
    case = management
    device, _ = await owner(case.env, database)
    principal = await case.env.identity.authenticate(device["token"])
    current = await seeded(case.env, database)
    ended = await seeded(case.env, database, state="completed")
    all_rows = (await case.views.sync(principal))["works"]
    assert {row["work_id"] for row in all_rows} == {current.row["id"], ended.row["id"]}

    aggregates = []

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        if "GROUP BY runtime_work_effects.work_id" in statement:
            aggregates.append(parameters)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    stream = case.views.stream(principal, active=True)
    try:
        first = await anext(stream)
        snapshot = json.loads(first.split("data: ", 1)[1])
        assert [row["work_id"] for row in snapshot["works"]] == [current.row["id"]]
        assert aggregates and current.row["id"] in str(aggregates)
        assert ended.row["id"] not in str(aggregates)

        pending = asyncio.create_task(anext(stream))
        async with database.immediate_session() as writer:
            await writer.execute(
                insert(effects).values(
                    effect_key="late-history-send",
                    work_id=ended.row["id"],
                    kind="tool",
                    state="accepted",
                    receipt_json=json.dumps(
                        {"invocation": {"tool_id": "send"}, "outcome": {"ok": True}}
                    ),
                    created=1.0,
                    updated=1.0,
                )
            )
        refreshed = await asyncio.wait_for(pending, 2)
        assert "event: refresh\n" in refreshed
        detail = await case.works.steps(principal, ended.row["id"], None, 50)
        assert detail["steps"][0]["category"] == "message"
        assert ended.row["id"] not in str(aggregates)
    finally:
        await stream.aclose()
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    invalid = await case.client.get(
        "/client/v1/stream?works=ended", headers=bearer(device["token"])
    )
    assert invalid.status_code == 422
    parameter = next(
        item
        for item in client_openapi()["paths"]["/client/v1/stream"]["get"]["parameters"]
        if item["name"] == "works"
    )
    assert parameter["in"] == "query"


@pytest.mark.parametrize(
    ("tool", "category", "label"),
    [
        ("send", "message", "傳送訊息"),
        ("send_message", "message", "傳送訊息"),
        ("read_webpage", "web", "讀網頁"),
        ("workspace_inspect", "file", "讀取檔案"),
        ("inspect_conversation_attachment", "file", "讀取檔案"),
        ("save_conversation_attachment_to_workspace", "file", "保存檔案"),
        ("workspace_publish", "file", "發布檔案"),
    ],
)
def test_work_steps_classify_current_tools_and_original_historical_receipts(tool, category, label):
    step = public_step(
        {
            "receipt_json": json.dumps({"invocation": {"tool_id": tool}, "outcome": {"ok": True}}),
            "state": "accepted",
            "kind": "tool",
            "effect_key": "original-effect",
            "created": 1.0,
            "updated": 1.0,
        },
        b"work-namespace",
    )
    assert (step["category"], step["label"], step["status"]) == (category, label, "succeeded")


async def test_capability_and_status_project_actual_manager_root_manifest(
    management, database, tmp_path
):
    from tests.unit.test_computer_control import manager_at

    case = management
    device, _ = await owner(case.env, database)
    principal = await case.env.identity.authenticate(device["token"])
    manager = manager_at(tmp_path)
    # /proc is a native Linux memory probe; manifest/status projection remains real here.
    manager.available_memory = lambda: 0
    manager.desktop_manifest = {
        "desktop": True,
        "tools": [{"name": "yuki-computer", "version": "test-build"}],
    }

    class ManagerTransport:
        async def execute(self, name, _args, *, request_id):
            assert request_id
            if name == "environment_status":
                return manager.environment_status()
            if name == "display_status":
                return manager.desktop.display_status()
            assert name == "control_get"
            return manager.computer.snapshot()

    class ApprovedPlugins:
        async def public_capabilities(self):
            return ()

    try:
        transport = ManagerTransport()
        capabilities = await ClientCapabilities(
            case.env.identity, ApprovedPlugins(), transport
        ).get(principal)
        assert capabilities["environment"] == manager.desktop_manifest
        computer = ClientComputer(case.env.identity, transport)
        environment = await computer.environment(principal)
        assert environment["state"] == "running" and environment["display"] == "stopped"
        assert environment["control"] == {"holder": "none"}
    finally:
        await manager.close()
