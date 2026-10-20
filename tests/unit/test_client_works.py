"""Personal inputs retain original Work identity and commit all three facts together."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select, update
from tests.support.event_seed import seed_scoped_event
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_client_api import bearer, pair

from yuki.client_api.works import ClientWorks
from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ControlCommandReceiptModel,
)
from yuki.domain.conversations import ConversationScope
from yuki.identity.canonical_repository import find_identity_binding
from yuki.persistence.event_repository import ConversationReadVersion
from yuki.persistence.models import ChatEventModel
from yuki.runtime.work_budget_schema import budgets
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import effects, inputs, journal, work
from yuki.runtime.work_source_guard import WorkSourceGuard
from yuki.social.db_models import SocialOperationModel

admin = admin_fixture
app_env = app_fixture


async def owner(env, database, account="1001"):
    async with database.sessions() as session:
        binding = await find_identity_binding(session, account)
    issued = await env.identity.issue_for_operator(binding.person_id, str(uuid4()), str(uuid4()))
    device = await pair(env.client, issued["code"])
    return device, binding.person_id


async def seeded(
    env, database, *, account="1001", group=False, state="running", retain_lease=False
):
    event = (
        await seed_scoped_event(
            env.harness.scoped_events,
            scope=ConversationScope.group("8000", "2001")
            if group
            else ConversationScope.private("8000", account),
            platform_message_id=str(uuid4()),
            sender_user_id=account,
            direction="inbound",
            content="original request",
        )
    ).event
    repository = WorkRepository(database)
    lease = await repository.acquire(event.canonical_conversation_id, 1)
    source = {
        "actor_person_id": event.author_person_id,
        "actor_user_id": account,
        "principal_kind": "person",
        "origin": "user_message",
        "trigger_event_id": event.id,
        "bot_user_id": "8000",
        "presence_id": event.ingress_presence_id,
        "generation": 1,
        "conversation_id": event.canonical_conversation_id,
    }
    row = await repository.accept(
        lease, source_key=f"original:{uuid4()}", source=source, goal="original goal"
    )
    async with database.immediate_session() as session:
        await session.execute(
            update(work)
            .where(work.c.id == row["id"])
            .values(state=state, model_requests=3, tool_calls=2)
        )
        await session.execute(
            insert(budgets).values(
                root_id=row["id"], models=3, tools=2, model_limit=20, tool_limit=40
            )
        )
        await session.execute(
            insert(journal).values(
                work_id=row["id"],
                chain_id=str(uuid4()),
                contract="original-contract",
                source_revision=1,
                phase="tools",
                payload_json='{"session":"original-session"}',
                updated=1.0,
            )
        )
    if not retain_lease:
        await repository.release(lease)
    return SimpleNamespace(
        event=event,
        repository=repository,
        lease=lease,
        row=await repository.get(row["id"]),
        source=source,
    )


@pytest.mark.parametrize("group", [False, True])
async def test_input_is_one_app_event_with_atomic_ready_payload_and_no_main(
    app_env, database, group
):
    env = app_env
    device, person = await owner(env, database)
    original = await seeded(env, database, group=group)
    request = {"request_id": str(uuid4()), "text": "use this extra requirement"}
    response = await env.client.post(
        f"/client/v1/works/{original.row['id']}/inputs",
        json=request,
        headers=bearer(device["token"]),
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    async with database.sessions() as session:
        event = await session.get(ChatEventModel, result["event_id"])
        item = (
            (await session.execute(select(inputs).where(inputs.c.id == result["input_id"])))
            .mappings()
            .one()
        )
        assert (
            item["ready"]
            and item["work_id"] == original.row["id"]
            and item["conversation_id"] == original.row["conversation_id"]
        )
        assert event.author_person_id == person and event.origin == "work_input"
        assert event.canonical_conversation_id != original.row["conversation_id"]
        assert json.loads(item["payload_json"]) == {
            "text": request["text"],
            "origin": "client_input",
            "source_conversation_id": event.canonical_conversation_id,
            "device_id": device["device_id"],
        }
        assert (
            await session.scalar(
                select(func.count(ControlCommandReceiptModel.principal_id)).where(
                    ControlCommandReceiptModel.request_id == request["request_id"]
                )
            )
            == 1
        )
        assert (
            await session.execute(
                select(journal.c.payload_json).where(journal.c.work_id == original.row["id"])
            )
        ).scalar_one() == '{"session":"original-session"}'
    assert await original.repository.get(original.row["id"]) == original.row
    assert not env.calls and not env.gateway.tasks
    queried = await env.client.get(
        f"/client/v1/requests/{request['request_id']}", headers=bearer(device["token"])
    )
    assert (
        queried.status_code == 200
        and queried.json()["data"]["operation_kind"] == "client.work_input"
    )
    assert queried.json()["data"]["result"] == {
        "event_id": result["event_id"],
        "input_id": result["input_id"],
    }


async def test_waiting_resume_retains_session_budget_and_honors_busy_original_lease(
    app_env, database
):
    env = app_env
    device, _ = await owner(env, database)
    original = await seeded(env, database, state="waiting_user", retain_lease=True)
    request = {"request_id": str(uuid4()), "text": "answer"}
    busy = await env.client.post(
        f"/client/v1/works/{original.row['id']}/inputs",
        json=request,
        headers=bearer(device["token"]),
    )
    assert busy.status_code == 503
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(inputs.c.id))) == 0
        assert await session.scalar(select(func.count(ChatEventModel.id))) == 1
    await original.repository.release(original.lease)
    response = await env.client.post(
        f"/client/v1/works/{original.row['id']}/inputs",
        json=request,
        headers=bearer(device["token"]),
    )
    assert response.status_code == 200, response.text
    current = await original.repository.get(original.row["id"])
    assert current["state"] == "queued"
    for key in (
        "id",
        "conversation_id",
        "generation",
        "source_json",
        "goal",
        "model_requests",
        "tool_calls",
    ):
        assert current[key] == original.row[key]
    async with database.sessions() as session:
        assert (
            await session.execute(
                select(budgets.c.models, budgets.c.model_limit).where(
                    budgets.c.root_id == current["id"]
                )
            )
        ).one() == (3, 20)
        assert (
            await session.execute(
                select(journal.c.payload_json).where(journal.c.work_id == current["id"])
            )
        ).scalar_one() == '{"session":"original-session"}'


async def test_other_person_cannot_read_input_or_cancel_group_work(app_env, database):
    env = app_env
    other, _ = await owner(env, database, "1002")
    original = await seeded(env, database, group=True)
    for path, body in (
        (f"/client/v1/works/{original.row['id']}", None),
        (
            f"/client/v1/works/{original.row['id']}/inputs",
            {"request_id": str(uuid4()), "text": "forged"},
        ),
        (
            f"/client/v1/works/{original.row['id']}/cancel",
            {"request_id": str(uuid4()), "expected_revision": original.row["revision"]},
        ),
    ):
        response = (
            await env.client.get(path, headers=bearer(other["token"]))
            if body is None
            else await env.client.post(path, json=body, headers=bearer(other["token"]))
        )
        assert response.status_code == 404, response.text
    assert await original.repository.get(original.row["id"]) == original.row


async def test_input_request_identity_spans_work_message_and_devices(app_env, database):
    env = app_env
    device, _ = await owner(env, database)
    original = await seeded(env, database)
    other = await seeded(env, database)
    request = {"request_id": str(uuid4()), "text": "same body"}
    issued = await env.identity.issue_for_device(
        await env.identity.authenticate(device["token"]), str(uuid4())
    )
    second = await pair(env.client, issued["code"])
    first = await env.client.post(
        f"/client/v1/works/{original.row['id']}/inputs",
        json=request,
        headers=bearer(device["token"]),
    )
    assert first.status_code == 200, first.text
    replay = await env.client.post(
        f"/client/v1/works/{original.row['id']}/inputs",
        json=request,
        headers=bearer(second["token"]),
    )
    assert replay.status_code == 200 and replay.json()["data"] == {
        **first.json()["data"],
        "replay": True,
    }
    different = await env.client.post(
        f"/client/v1/works/{other.row['id']}/inputs", json=request, headers=bearer(second["token"])
    )
    assert different.status_code == 409
    message = await env.client.post(
        "/client/v1/messages", json=request, headers=bearer(device["token"])
    )
    assert message.status_code == 409
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(inputs.c.id))) == 1
    assert not env.calls


async def test_failure_after_mailbox_insert_rolls_back_all_facts(app_env, database, monkeypatch):
    env = app_env
    device, _ = await owner(env, database)
    original = await seeded(env, database, state="waiting_user")
    record = env.identity.receipts.record_personal_command

    async def fail(session, **kwargs):
        await record(session, **kwargs)
        raise RuntimeError("test failure after receipt")

    monkeypatch.setattr(env.identity.receipts, "record_personal_command", fail)
    principal = await env.identity.authenticate(device["token"])
    with pytest.raises(RuntimeError, match="test failure"):
        await ClientWorks(env.identity, env.gateway).submit(
            principal, original.row["id"], str(uuid4()), "rollback this"
        )
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(inputs.c.id))) == 0
        assert await session.scalar(select(func.count(ChatEventModel.id))) == 1
        assert (
            await session.scalar(
                select(func.count(ControlCommandReceiptModel.principal_id)).where(
                    ControlCommandReceiptModel.operation_kind == "client.work_input"
                )
            )
            == 0
        )
    assert await original.repository.get(original.row["id"]) == original.row


@pytest.mark.parametrize("state", ["completed", "failed", "cancelled"])
async def test_terminal_work_preserves_failure_receipt_and_does_not_create_input(
    app_env, database, state
):
    env = app_env
    device, _ = await owner(env, database)
    original = await seeded(env, database, state=state)
    request = {"request_id": str(uuid4()), "text": "too late"}
    for _ in range(2):
        response = await env.client.post(
            f"/client/v1/works/{original.row['id']}/inputs",
            json=request,
            headers=bearer(device["token"]),
        )
        assert response.status_code == 409 and response.json()["problem"] == {
            "code": "precondition_failed",
            "state": state,
        }
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(inputs.c.id))) == 0
        assert await session.scalar(select(func.count(ChatEventModel.id))) == 1
        assert (
            await session.scalar(
                select(func.count(ControlCommandReceiptModel.principal_id)).where(
                    ControlCommandReceiptModel.request_id == request["request_id"]
                )
            )
            == 1
        )


async def test_cancel_preserves_original_receipt_and_revision_conflict(app_env, database):
    env = app_env
    device, _ = await owner(env, database)
    original = await seeded(env, database)
    stale = await env.client.post(
        f"/client/v1/works/{original.row['id']}/cancel",
        json={"request_id": str(uuid4()), "expected_revision": original.row["revision"] + 1},
        headers=bearer(device["token"]),
    )
    assert stale.status_code == 409 and stale.json()["problem"]["code"] == "version_conflict"
    request = {"request_id": str(uuid4()), "expected_revision": original.row["revision"]}
    response = await env.client.post(
        f"/client/v1/works/{original.row['id']}/cancel",
        json=request,
        headers=bearer(device["token"]),
    )
    assert response.status_code == 200, response.text
    replay = await env.client.post(
        f"/client/v1/works/{original.row['id']}/cancel",
        json=request,
        headers=bearer(device["token"]),
    )
    assert replay.status_code == 200 and replay.json()["data"]["replay"]
    assert (await original.repository.get(original.row["id"]))["state"] == "cancelled"


async def test_home_and_group_work_show_only_this_works_confirmed_question(app_env, database):
    env = app_env
    device, person = await owner(env, database)
    original = await seeded(env, database, group=True, state="waiting_user")
    other = await seeded(env, database, account="1002", group=True)
    for target, body in ((original, "what city?"), (other, "other person's question")):
        event = (
            await seed_scoped_event(
                env.harness.scoped_events,
                scope=ConversationScope.group("8000", "2001"),
                platform_message_id=str(uuid4()),
                sender_user_id="8000",
                direction="outbound",
                content=body,
            )
        ).event
        operation = str(uuid4())
        async with database.immediate_session() as session:
            session.add(
                SocialOperationModel(
                    id=operation,
                    source_turn_id=f"{target.row['conversation_id']}:event:{target.event.id}",
                    tool_call_id=str(uuid4()),
                    source_conversation_id=target.row["conversation_id"],
                    action="send_message",
                    payload_hash="a" * 64,
                    target_kind="space",
                    target_id=target.event.canonical_conversation_id,
                    presence_id=target.event.ingress_presence_id,
                    status="succeeded",
                    event_id=event.id,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
            await session.execute(
                insert(effects).values(
                    effect_key=str(uuid4()),
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
    await seed_scoped_event(
        env.harness.scoped_events,
        scope=ConversationScope.group("8000", "2001"),
        platform_message_id=str(uuid4()),
        sender_user_id="1002",
        direction="inbound",
        content="hidden group message",
    )
    response = await env.client.get(
        f"/client/v1/works/{original.row['id']}", headers=bearer(device["token"])
    )
    assert response.status_code == 200, response.text
    public = response.json()["data"]
    assert public["waiting_question"]["text"] == "what city?"
    assert public["budget"]["models"] == 3 and public["budget"]["model_limit"] == 20
    assert (
        public["source"]["kind"] == "space"
        and public["result_delivery"]["conversation_id"] == original.row["conversation_id"]
    )
    assert all(
        value not in response.text
        for value in (
            "other person's question",
            "hidden group message",
            "checkpoint_json",
            "source_json",
            "original-session",
        )
    )
    home = await env.client.get("/client/v1/home", headers=bearer(device["token"]))
    assert home.status_code == 200 and [
        item["work_id"] for item in home.json()["data"]["works"]
    ] == [original.row["id"]]
    assert person == original.source["actor_person_id"]


async def guard_for(original, database):
    async with database.sessions() as session:
        conversation = await session.get(
            CanonicalConversationModel, original.row["conversation_id"]
        )
        scope = (
            ConversationScope.group("8000", "2001")
            if conversation.kind == "space"
            else ConversationScope.private("8000", "1001")
        )
        version = ConversationReadVersion(
            scope,
            conversation.id,
            conversation.generation,
            conversation.starts_after_event_id,
            conversation.prompt_source_revision,
            (original.event.id,),
        )
    control = SimpleNamespace(
        repository=original.repository,
        lease=original.lease,
        current=original.row,
        source=original.source,
        session=SimpleNamespace(event_ids=[], source_revision=0),
    )
    guard = WorkSourceGuard(version)
    assert await guard.check(control)
    return guard, control


async def test_guard_accepts_only_mailbox_proof_and_retains_original_work_after_other_entry_reset(
    app_env, database
):
    from yuki.conversation.hydrate import bump_canonical_generation

    env = app_env
    device, _ = await owner(env, database)
    original = await seeded(env, database, group=True, retain_lease=True)
    guard, control = await guard_for(original, database)
    request = {"request_id": str(uuid4()), "text": "new authenticated input"}
    response = await env.client.post(
        f"/client/v1/works/{original.row['id']}/inputs",
        json=request,
        headers=bearer(device["token"]),
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert not await guard.check(None, database=database, event_ids=frozenset({result["event_id"]}))
    control.session.event_ids.append(result["event_id"])
    assert await guard.check(control)
    restored = WorkSourceGuard.restore(guard.snapshot())
    assert await restored.check(control)
    principal = await env.identity.authenticate(device["token"])
    async with database.immediate_session() as session:
        await bump_canonical_generation(
            session, principal.conversation_id, event_id=result["event_id"], force=True
        )
    assert await restored.check(control)
    async with database.immediate_session() as session:
        await session.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == result["event_id"])
            .values(content="tampered input")
        )
    assert not await restored.check(control)
    assert await original.repository.get(original.row["id"]) == original.row
    await original.repository.release(original.lease)


async def test_same_person_event_without_mailbox_proof_is_not_a_work_input(app_env, database):
    env = app_env
    device, _ = await owner(env, database)
    original = await seeded(env, database, group=True, retain_lease=True)
    guard, control = await guard_for(original, database)
    principal = await env.identity.authenticate(device["token"])
    async with database.sessions() as session:
        from yuki.identity.db_models import IdentityBindingModel

        binding = await session.get(IdentityBindingModel, principal.binding_id)
    message = env.gateway.message(
        binding, str(uuid4()), "ordinary App message", (), conversation_id=principal.conversation_id
    )
    admitted = await env.gateway.processor._canonical_ingress.pre_admit(env.gateway.handle, message)
    event = (
        await env.gateway.processor._canonical_uow.append_inbound(admitted.message, admitted)
    ).event
    control.session.event_ids.append(event.id)
    assert not await guard.check(control)
    await original.repository.release(original.lease)


async def test_message_request_cannot_be_reused_for_work_input(app_env, database):
    env = app_env
    device, _ = await owner(env, database)
    original = await seeded(env, database)
    request = {"request_id": str(uuid4()), "text": "ordinary message"}
    response = await env.client.post(
        "/client/v1/messages", json=request, headers=bearer(device["token"])
    )
    assert response.status_code == 200, response.text
    response = await env.client.post(
        f"/client/v1/works/{original.row['id']}/inputs",
        json=request,
        headers=bearer(device["token"]),
    )
    assert response.status_code == 409 and response.json()["problem"]["code"] == "request_conflict"
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(inputs.c.id))) == 0


async def test_input_committed_before_completion_keeps_original_work_runnable(app_env, database):
    env = app_env
    device, _ = await owner(env, database)
    original = await seeded(env, database, group=True, retain_lease=True)
    response = await env.client.post(
        f"/client/v1/works/{original.row['id']}/inputs",
        json={"request_id": str(uuid4()), "text": "arrived before completion"},
        headers=bearer(device["token"]),
    )
    assert response.status_code == 200, response.text
    async with database.immediate_session() as session:
        row = await original.repository.commit_state(
            session,
            original.lease,
            original.row["id"],
            "completed",
            revision=original.row["revision"],
        )
    assert row["state"] == "queued" and row["reason"] == "work_input_arrived"
    assert (
        row["conversation_id"] == original.row["conversation_id"]
        and row["generation"] == original.row["generation"]
    )
    assert [
        item["id"] for item in await original.repository.pending(original.lease, work_id=row["id"])
    ] == [response.json()["data"]["input_id"]]
    await original.repository.release(original.lease)


async def test_completion_between_preparation_and_writer_creates_no_input(
    app_env, database, monkeypatch
):
    env = app_env
    device, _ = await owner(env, database)
    original = await seeded(env, database)
    pre_admit = env.gateway.processor._canonical_ingress.pre_admit

    async def completing(*args, **kwargs):
        admitted = await pre_admit(*args, **kwargs)
        async with database.immediate_session() as session:
            await session.execute(
                update(work)
                .where(work.c.id == original.row["id"])
                .values(state="completed", revision=work.c.revision + 1)
            )
        return admitted

    monkeypatch.setattr(env.gateway.processor._canonical_ingress, "pre_admit", completing)
    response = await env.client.post(
        f"/client/v1/works/{original.row['id']}/inputs",
        json={"request_id": str(uuid4()), "text": "arrived too late"},
        headers=bearer(device["token"]),
    )
    assert response.status_code == 409 and response.json()["problem"] == {
        "code": "precondition_failed",
        "state": "completed",
    }
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(inputs.c.id))) == 0
        assert await session.scalar(select(func.count(ChatEventModel.id))) == 1


@pytest.mark.parametrize("completion_first", [False, True])
async def test_group_cancel_and_completion_preserve_original_winner(
    app_env, database, completion_first
):
    from yuki.runtime.work_repository import WorkConflict

    env = app_env
    device, _ = await owner(env, database)
    original = await seeded(env, database, group=True, retain_lease=True)
    try:
        if completion_first:
            async with database.immediate_session() as session:
                await original.repository.commit_state(
                    session,
                    original.lease,
                    original.row["id"],
                    "completed",
                    revision=original.row["revision"],
                )
        cancelled = await env.client.post(
            f"/client/v1/works/{original.row['id']}/cancel",
            json={"request_id": str(uuid4()), "expected_revision": original.row["revision"]},
            headers=bearer(device["token"]),
        )
        if completion_first:
            assert (
                cancelled.status_code == 409
                and cancelled.json()["problem"]["code"] == "version_conflict"
            )
            assert (await original.repository.get(original.row["id"]))["state"] == "completed"
        else:
            assert cancelled.status_code == 200
            with pytest.raises(WorkConflict):
                async with database.immediate_session() as session:
                    await original.repository.commit_state(
                        session,
                        original.lease,
                        original.row["id"],
                        "completed",
                        revision=original.row["revision"],
                    )
            assert (await original.repository.get(original.row["id"]))["state"] == "cancelled"
    finally:
        await original.repository.release(original.lease)


async def test_plain_app_reply_matches_own_waiting_entry_without_borrowing_qq_wait(
    app_env, database
):
    from tests.unit.test_app_messages import drain

    env = app_env
    device, person = await owner(env, database)
    qq = await seeded(env, database, state="waiting_user")
    started = await env.client.post(
        "/client/v1/messages",
        json={"request_id": str(uuid4()), "text": "start the App task"},
        headers=bearer(device["token"]),
    )
    await drain(env)
    app_conversation = started.json()["data"]["conversation_id"]
    lease = await qq.repository.acquire(app_conversation, 1)
    app_work = await qq.repository.accept(
        lease,
        source_key="original-app-wait",
        source={
            "actor_person_id": person,
            "origin": "user_message",
            "trigger_event_id": started.json()["data"]["event_id"],
        },
        goal="continue here",
    )
    await qq.repository.transition(lease, app_work["id"], app_work["revision"], "waiting_user")
    await qq.repository.release(lease)
    before = len(env.calls)
    answered = await env.client.post(
        "/client/v1/messages",
        json={"request_id": str(uuid4()), "text": "the actual App answer"},
        headers=bearer(device["token"]),
    )
    assert answered.status_code == 200, answered.text
    await drain(env)
    assert len(env.calls) == before
    assert (await qq.repository.get(app_work["id"]))["state"] == "queued"
    assert await qq.repository.get(qq.row["id"]) == qq.row
    async with database.sessions() as session:
        item = (
            (await session.execute(select(inputs).where(inputs.c.work_id == app_work["id"])))
            .mappings()
            .one()
        )
        assert item["ready"] and item["event_id"] == answered.json()["data"]["event_id"]
        assert "the actual App answer" in json.loads(item["payload_json"])["text"]
