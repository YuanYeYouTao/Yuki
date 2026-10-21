"""Merge and names do not cancel or repurchase an already dispatched Work request."""

import asyncio
import copy
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.integration.test_telegram_runtime import drain as drain_telegram
from tests.integration.test_telegram_runtime import telegram_env as telegram_env
from tests.integration.test_telegram_runtime import update
from tests.support.client_identity import client_identity_service
from tests.support.runtime_execution import make_child_executor
from tests.unit.test_app_messages import app_env as app_env
from tests.unit.test_app_messages import drain
from tests.unit.test_client_api import bearer
from tests.unit.test_client_works import owner
from tests.unit.test_personal_entry_contract import admin as admin

from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.identity.canonical_repository import find_identity_binding
from yuki.identity.db_models import CanonicalPersonModel
from yuki.identity.person_identity import merge_persons, rename_person
from yuki.persistence.models import ChatEventModel
from yuki.runtime.subagent_repository import SubagentRepository
from yuki.runtime.work_budget import charge
from yuki.runtime.work_budget_schema import budgets
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import journal, work


@pytest.mark.parametrize("change", ["merge", "rename", "merge_disabled"])
async def test_warm_work_retains_paid_request_source_and_budget(
    app_env, database, monkeypatch, change
):
    env = app_env
    chat = env.harness.processor._chat
    chat._settings = chat._settings.model_copy(update={"runtime_work_enabled": True})
    device, person_id = await owner(env, database)
    entered, release = asyncio.Event(), asyncio.Event()
    original_complete = env.provider.complete

    def respond(_request):
        count = len(env.provider.requests)
        action = "accept" if count == 1 else "update" if change == "merge_disabled" else "complete"
        arguments = {"action": action}
        arguments["result" if action == "complete" else "goal"] = "original internal task"
        return ChatResponse(
            "",
            0,
            tool_calls=(
                ToolCall(f"original-{count}", ToolFunction("task_control", json.dumps(arguments))),
            ),
        )

    env.provider._responder = respond

    async def complete(request):
        result = await original_complete(request)
        if len(env.provider.requests) == 2:
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(env.provider, "complete", complete)
    response = await env.client.post(
        "/client/v1/messages",
        headers=bearer(device["token"]),
        json={"request_id": str(uuid4()), "text": "complete this internal task"},
    )
    assert response.status_code == 200
    await asyncio.wait_for(entered.wait(), 5)
    original_requests = copy.deepcopy(env.provider.requests)
    async with database.sessions() as reader:
        original = (await reader.execute(select(work))).mappings().one()
        target = await find_identity_binding(reader, "1002")
        original_source = original["source_json"]
    try:
        async with database.immediate_session() as writer:
            root_id = person_id
            if change.startswith("merge"):
                root_id = await merge_persons(writer, person_id, target.person_id)
            else:
                await rename_person(
                    writer, person_id, "new independent name", now=datetime.now(UTC)
                )
            if change == "merge_disabled":
                root = await writer.get(CanonicalPersonModel, root_id)
                root.enabled = False
        release.set()
        await drain(env)
    finally:
        release.set()
    assert env.provider.requests == original_requests
    assert len(env.provider.requests) == 2
    async with database.sessions() as reader:
        current = (
            (await reader.execute(select(work).where(work.c.id == original["id"]))).mappings().one()
        )
        budget = (
            (await reader.execute(select(budgets).where(budgets.c.root_id == original["id"])))
            .mappings()
            .one()
        )
        saved = (
            (await reader.execute(select(journal).where(journal.c.work_id == original["id"])))
            .mappings()
            .one()
        )
    assert current["source_json"] == original_source
    assert json.loads(current["source_json"])["actor_person_id"] == person_id
    assert current["parent_work_id"] is None
    assert budget["models"] == 2
    assert saved["chain_id"] == original_requests[1].request_chain_id
    assert current["state"] == ("failed" if change == "merge_disabled" else "completed")


@pytest.mark.parametrize("change", ["transfer", "merge", "source_disabled", "root_disabled"])
async def test_queued_child_keeps_original_owner_and_budget_without_an_active_account(
    telegram_env, database, change
):
    env = telegram_env
    await env.gateway.receive(update(text="original accepted internal task"))
    await drain_telegram(env)
    async with database.sessions() as reader:
        event = await reader.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "inbound")
        )
        original_binding = await find_identity_binding(
            reader, event.sender_user_id, platform="telegram"
        )
        target = await find_identity_binding(reader, "1002")
    repository = WorkRepository(database)
    source = {
        "origin": "user_message",
        "principal_kind": "person",
        "actor_person_id": event.author_person_id,
        "actor_user_id": event.sender_user_id,
        "trigger_event_id": event.id,
        "presence_id": event.ingress_presence_id,
        "bot_user_id": event.bot_user_id,
        "conversation_id": event.canonical_conversation_id,
        "generation": 1,
    }
    lease = await repository.acquire(event.canonical_conversation_id, 1)
    try:
        parent = await repository.accept(
            lease, source_key="original-parent", source=source, goal="internal task"
        )
        database.subagents_enabled = True
        child_id = await SubagentRepository(repository).start(
            lease, parent["id"], "original-spawn", {"goal": "internal calculation"}
        )
        async with database.immediate_session() as writer:
            await charge(writer, parent["id"], models=3, tools=2)
    finally:
        await repository.release(lease)
    original = await repository.get(child_id)
    if change == "transfer":
        await client_identity_service(database).transfer_for_operator(
            original_binding.id, target.person_id, str(uuid4()), str(uuid4())
        )
    else:
        async with database.immediate_session() as writer:
            if change == "source_disabled":
                person = await writer.get(CanonicalPersonModel, event.author_person_id)
                person.enabled = False
            root_id = await merge_persons(writer, event.author_person_id, target.person_id)
            if change == "root_disabled":
                root = await writer.get(CanonicalPersonModel, root_id)
                root.enabled = False
    before = len(env.model.requests)
    env.model._responder = lambda _: ChatResponse(
        "",
        0,
        tool_calls=(
            ToolCall(
                "original-complete",
                ToolFunction("task_control", '{"action":"complete","result":"internal result"}'),
            ),
        ),
    )
    chat = env.harness.processor._chat
    executor = make_child_executor(
        repository,
        chat=chat,
        config=chat._runtime_config,
        runner=chat.runtime.runner,
        load_tools=chat.runtime.runner.main_contract.definitions,
    )
    await executor.run(child_id)
    child = await repository.get(child_id)
    assert child["source_json"] == original["source_json"]
    assert child["parent_work_id"] == parent["id"]
    allowed = change in {"merge", "transfer"}
    assert child["state"] == ("completed" if allowed else "failed")
    assert len(env.model.requests) - before == int(allowed)
    async with database.sessions() as reader:
        budget = (
            (await reader.execute(select(budgets).where(budgets.c.root_id == parent["id"])))
            .mappings()
            .one()
        )
    assert budget["models"] == 3 + int(allowed)
    if not allowed:
        assert budget["tools"] == 2
